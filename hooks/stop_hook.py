"""Stop hook for Claude Code and Codex: when a turn changed files in a directory listed in
hook_directories.txt, ask the agent to record what it changed with save_to_project_memory before it
finishes.

Both clients run it at the end of every turn with a JSON event on stdin (session_id, transcript_path,
cwd, stop_hook_active, ...). It reads the part of the session transcript written since its previous
run, and when that part edited files (or ran a command that changes them) without saving to the
memory, it answers {"decision": "block", "reason": ...}: both clients then keep the turn going with
the reason as the agent's next instruction. That continuation ends with stop_hook_active set, which
lets the turn finish. Anything unexpected lets the turn finish too: a hook must never trap an agent.

Standard library only, so it starts in milliseconds.

    python stop_hook.py --check [DIR]   # is the hook active in DIR (default: here), and what would it say?
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

DIRECTORIES_FILE = Path(__file__).resolve().parent.parent / "hook_directories.txt"
STATE_DIR = Path(tempfile.gettempdir()) / "project-memory-hook"  # per session: transcript offset of the last run

SAVE_TOOL = "save_to_project_memory"
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}  # Claude Code; Codex logs edits as FileChange items
SHELL_TOOLS = {"Bash", "PowerShell"}

# Shell commands that change files. A miss skips one reminder; a false alarm costs one short
# "Nothing to save" reply.
CHANGING_COMMAND = re.compile(r"""
      \bgit\s+(?:-C\s+\S+\s+)?(?:commit|merge|rebase|cherry-pick|revert|am|apply|mv|rm|restore|reset
                                |stash\s+(?:pop|apply))\b
    | \bsed\s+(?:-\w+\s+)*-i
    | (?<![\w-])(?:rm|mv|cp|mkdir|rmdir|touch|tee|truncate|patch)\s
    | \b(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item|Expand-Archive)\b
    | \b(?:pip|npm|pnpm|yarn|cargo|uv)\s+(?:install|add|remove|uninstall)\b
    | (?<![\d&>=-])>>?(?!\s*(?:&|/dev/null|\$null|nul\b))   # redirection into a file
""", re.VERBOSE | re.IGNORECASE)

REASON = """\
Before you finish: this turn changed files, and the other agents working on this project (other \
sessions, other worktrees, Claude or Codex) can only learn about it from the shared project memory.

Call the save_to_project_memory tool of the project-memory MCP server (in Claude Code: \
mcp__project-memory__save_to_project_memory) once, with:
- workdir: {cwd}
- title: one line naming what changed
- content: what you changed (files, components, behaviour) and why (the request, the decisions \
taken, the alternatives rejected), plus what the next agent must know: constraints, gotchas, \
anything unfinished or unverified. A few short paragraphs or bullets that stand on their own for \
someone who has not seen this conversation. No secrets.

If the tool is not loaded, search for it by name: the server takes a few seconds to connect. Skip \
the save only when the turn changed nothing that lasts (scratch files only, or edits you reverted); \
then reply only "Nothing to save to project memory." Then end your turn."""


def listed(cwd: str) -> bool:
    """Whether cwd is one of the directories in hook_directories.txt, or inside one."""
    try:
        lines = DIRECTORIES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    here = os.path.normcase(os.path.abspath(cwd))
    for line in lines:
        line = line.strip()
        if line and not line.startswith("#"):
            root = os.path.normcase(os.path.abspath(os.path.expandvars(os.path.expanduser(line))))
            if here == root or here.startswith(root.rstrip("\\/") + os.sep):
                return True
    return False


def new_entries(transcript: Path, session_id: str) -> list[dict]:
    """Transcript entries written since this hook last ran in the session."""
    state = STATE_DIR / (re.sub(r"[^\w-]", "_", session_id) + ".json")
    try:
        offset = int(json.loads(state.read_text(encoding="utf-8"))["offset"])
    except (OSError, ValueError, KeyError, TypeError):
        offset = 0
    if offset > transcript.stat().st_size:  # rewritten, not appended to: start over
        offset = 0
    with transcript.open("rb") as f:
        f.seek(offset)
        data = f.read()
    data = data[:data.rfind(b"\n") + 1]  # a line still being written belongs to the next run
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"offset": offset + len(data)}), encoding="utf-8")

    entries = []
    for line in data.splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            pass
    return entries


def turn_activity(entries: list[dict]) -> tuple[bool, bool]:
    """(changed files, saved to the memory) in these entries of a Claude Code or Codex transcript."""
    changed = saved = False
    for entry in entries:
        # Claude Code: assistant messages carry tool_use blocks
        message = entry.get("message") if entry.get("type") == "assistant" else None
        for block in (message or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, tool_input = block.get("name", ""), block.get("input") or {}
            saved |= name.endswith(SAVE_TOOL)
            changed |= name in EDIT_TOOLS or (
                name in SHELL_TOOLS and bool(CHANGING_COMMAND.search(str(tool_input.get("command", "")))))

        # Codex: each finished step of a turn is an item_completed event
        payload = entry.get("payload") or {}
        item = payload.get("item") if payload.get("type") == "item_completed" else None
        if isinstance(item, dict):
            kind = item.get("type")
            if kind == "FileChange":
                changed = True
            elif kind == "CommandExecution":
                command = item.get("command")
                changed |= bool(CHANGING_COMMAND.search(" ".join(command) if isinstance(command, list)
                                                        else str(command or "")))
            elif kind == "McpToolCall":
                saved |= item.get("tool") == SAVE_TOOL
        # Codex code mode calls tools from JavaScript inside one custom tool call
        if payload.get("type") == "custom_tool_call" and SAVE_TOOL in str(payload.get("input", "")):
            saved = True
    return changed, saved


def decide(event: dict) -> dict | None:
    """The hook's answer to a Stop event: a block with the save instruction, or None to let the turn end."""
    cwd = event.get("cwd") or os.getcwd()
    transcript = event.get("transcript_path")
    if not transcript or not listed(cwd):
        return None
    entries = new_entries(Path(transcript), str(event.get("session_id") or "unknown"))
    if event.get("stop_hook_active"):  # the continuation this hook asked for: let the turn end
        return None
    changed, saved = turn_activity(entries)
    return {"decision": "block", "reason": REASON.format(cwd=cwd)} if changed and not saved else None


def check(directory: str) -> None:
    """--check [DIR]: whether the hook is active in DIR, and its answer to a turn that edited a file there."""
    directory = os.path.abspath(directory)
    print(f"Directory list: {DIRECTORIES_FILE} ({'found' if DIRECTORIES_FILE.is_file() else 'missing'})")
    if not listed(directory):
        print(f"{directory} is not listed: the hook lets every turn there end without asking anything.")
        return
    session = f"check-{os.getpid()}"
    with tempfile.TemporaryDirectory() as tmp:  # a one-entry transcript: an agent that edited a file
        transcript = Path(tmp) / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "example.txt"}}]}}) + "\n", encoding="utf-8")
        answer = decide({"session_id": session, "transcript_path": str(transcript), "cwd": directory})
    (STATE_DIR / f"{session}.json").unlink(missing_ok=True)
    if answer:
        print(f"{directory} is listed. After a turn that edits a file, the hook blocks the stop and tells "
              f"the agent:\n\n{answer['reason']}")
    else:
        print(f"{directory} is listed, but the hook did not react to a simulated edit: this is a bug.")


def main() -> None:
    if sys.argv[1:2] == ["--check"]:
        check(sys.argv[2] if len(sys.argv) > 2 else os.getcwd())
        return
    try:
        answer = decide(json.loads(sys.stdin.buffer.read().decode("utf-8")))
        if answer:
            print(json.dumps(answer))
    except Exception:
        return


if __name__ == "__main__":
    main()
