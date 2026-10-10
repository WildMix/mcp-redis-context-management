"""Stop hook for Claude Code and Codex: when a turn changed the project's files in a directory listed in
hook_directories.txt, ask the agent whether it learned anything worth saving to the project memory,
and to save it with save_to_project_memory if so. Only new, lasting knowledge about the project
qualifies (see REASON); a turn that teaches nothing ends with "Nothing to save to project memory."

Both clients run it at the end of every turn with a JSON event on stdin (session_id, transcript_path,
cwd, stop_hook_active, ...). It reads the part of the session transcript written since its previous
run. When that part edited files inside the project (any worktree of the session's repository), or
ran a command that changes them there, without saving to the memory, it answers
{"decision": "block", "reason": ...}: both clients then keep the turn going with the reason as the
agent's next instruction. Changes to other repositories or to configuration elsewhere do not count.
That continuation ends with stop_hook_active set, which lets the turn finish. Anything unexpected lets
the turn finish too: a hook must never trap an agent.

Standard library only, so it starts in milliseconds.

    python stop_hook.py --check [DIR]   # is the hook active in DIR (default: here), and what would it say?
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

DIRECTORIES_FILE = Path(__file__).resolve().parent.parent / "hook_directories.txt"
STATE_DIR = Path(tempfile.gettempdir()) / "project-memory-hook"  # per session: transcript offset of the last run

SAVE_TOOL = "save_to_project_memory"
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}  # Claude Code; Codex logs edits as FileChange items
SHELL_TOOLS = {"Bash", "PowerShell"}

# Shell commands that change files. A miss skips one reminder; a false alarm costs one short
# "Nothing to save" reply.
CHANGING_COMMAND = re.compile(r"""
      \bgit\s+(?:-C\s+\S+\s+)?(?:commit|merge|rebase|cherry-pick|revert|am|apply|mv|rm|restore|reset
                                |stash\s+(?:pop|apply))(?![\w-])
    | \bsed\s+(?:-\w+\s+)*-i
    | (?<![\w-])(?:rm|mv|cp|mkdir|rmdir|touch|tee|truncate|patch)\s
    | \b(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item|Move-Item|Copy-Item|Rename-Item|Expand-Archive)\b
    | \b(?:pip|npm|pnpm|yarn|cargo|uv)\s+(?:install|add|remove|uninstall)\b
    | (?<![\d&<>=-])>>?(?!\s*(?:&|/dev/null|\$null|nul\b))   # redirection into a file
""", re.VERBOSE | re.IGNORECASE)

# Where a command works: a command that moves to a directory first (cd, pushd, Set-Location, git -C)
# works there; one that names absolute paths works on those; otherwise it works in its own directory.
WORKDIR_ARG = re.compile(r"""
      (?:^|[\s;&|(])(?i:cd|pushd|Set-Location|sl)\s+("[^"]+"|'[^']+'|[^\s;&|)]+)
    | \bgit\s+-C\s+("[^"]+"|'[^']+'|\S+)
""", re.VERBOSE)
ABSOLUTE_PATH = re.compile(r"""(?:(?<=^)|(?<=[\s"'=(]))
    (~[\\/][^\s"';&|)]*|[A-Za-z]:[\\/][^\s"';&|)]*|/(?:mnt/)?[a-zA-Z]/[^\s"';&|)]*"""
                           + (r")" if os.name == "nt" else r"|/(?!dev/)[^\s\"';&|)]+)"), re.VERBOSE)
ASSIGNMENT = re.compile(r"""(?:^|[\s;&|(])(?:export\s+)?([A-Za-z_]\w*)=("[^"]*"|'[^']*'|[^\s;&|)]*)""")

REASON = """\
This turn changed files in this project. Before you finish, decide whether it taught anything that \
belongs in the project memory. Every agent working on this project (other sessions, other \
worktrees, Claude or Codex) reads that memory, and it is only useful if it holds what they cannot \
find anywhere else.

Save only if all of these hold:
- It is about this project itself: its code, design, architecture, assets, build or workflow. Work \
on other repositories, on your tools, on the agent setup or on this memory system does not belong \
here.
- It is new: not already in the project memory (search it if unsure), and not already plain from \
the code, the diff, the commit message or the docs.
- It will still matter later: a decision and its reasons, a rejected alternative, a constraint, a \
gotcha, a non-obvious cause, a known limitation or unfinished piece. A list of edits, or the state \
of this session, is not.

When in doubt, don't save: reply only "Nothing to save to project memory." and end your turn.

Otherwise call the save_to_project_memory tool of the project-memory MCP server (in Claude Code: \
mcp__project-memory__save_to_project_memory) once, with workdir: {cwd}, a one-line title, and \
content holding only that knowledge, written for an agent who has not seen this conversation. No \
secrets. If the tool is not loaded, search for it by name: the server takes a few seconds to \
connect. Then end your turn."""


def _native(path: str, base: str) -> str:
    """path made absolute against base and normalised for comparisons. On Windows, Git Bash's /c/...
    and WSL's /mnt/c/... become C:/..."""
    path = os.path.expandvars(os.path.expanduser(path.strip("'\"")))
    if os.name == "nt":
        if m := re.match(r"^/(?:mnt/)?([a-zA-Z])(/.*)?$", path):
            path = f"{m.group(1)}:{m.group(2) or '/'}"
    return os.path.normcase(os.path.abspath(os.path.join(base, path)))


def _inside(path: str, roots: list[str]) -> bool:
    return any(path == root or path.startswith(root.rstrip("\\/") + os.sep) for root in roots)


def listed(cwd: str) -> bool:
    """Whether cwd is one of the directories in hook_directories.txt, or inside one."""
    try:
        lines = DIRECTORIES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    roots = [_native(line.strip(), cwd) for line in lines if line.strip() and not line.strip().startswith("#")]
    return _inside(_native(cwd, cwd), roots)


def project_dirs(cwd: str) -> list[str]:
    """The project's directories: every worktree of cwd's repository (they share one memory), or cwd
    itself outside git."""
    try:
        listing = subprocess.run(["git", "-C", cwd, "worktree", "list", "--porcelain"], capture_output=True,
                                 encoding="utf-8", stdin=subprocess.DEVNULL, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        listing = ""
    worktrees = [line[len("worktree "):] for line in listing.splitlines() if line.startswith("worktree ")]
    return [_native(d, cwd) for d in worktrees] or [_native(cwd, cwd)]


def shell_changes_project(command: str, base: str, roots: list[str]) -> bool:
    """Whether a shell command run in base changes files in the project. A heuristic: a miss skips one
    reminder, a false alarm costs one short "Nothing to save" reply."""
    for name, value in ASSIGNMENT.findall(command):  # S="C:/x"; cd "$S/y" works in C:/x/y
        command = re.sub(r"\$(?:\{%s\}|%s\b)" % (name, name), lambda _: value.strip("'\""), command)
    if not CHANGING_COMMAND.search(command):
        return False

    def in_project(path: str) -> bool:  # a path still holding a variable (a loop's, say) may be anywhere
        return "$" in path or _inside(_native(path, base), roots)

    targets = [cd_dir or git_dir for cd_dir, git_dir in WORKDIR_ARG.findall(command)]
    paths = targets or ABSOLUTE_PATH.findall(command)
    return any(in_project(p) for p in paths) if paths else _inside(_native(base, base), roots)


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


def turn_activity(entries: list[dict], cwd: str, roots: list[str]) -> tuple[bool, bool]:
    """(changed the project's files, saved to the memory) in these entries of a Claude Code or Codex
    transcript; cwd is the session's directory, roots the project's directories."""
    changed = saved = False

    def edits_project(paths: list[str], base: str) -> bool:  # an edit with no known path counts
        return not paths or any(_inside(_native(p, base), roots) for p in paths)

    for entry in entries:
        # Claude Code: assistant messages carry tool_use blocks
        message = entry.get("message") if entry.get("type") == "assistant" else None
        for block in (message or {}).get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name, tool_input = block.get("name", ""), block.get("input") or {}
            saved |= name.endswith(SAVE_TOOL)
            if name in EDIT_TOOLS:
                path = tool_input.get("file_path") or tool_input.get("notebook_path")
                changed |= edits_project([path] if path else [], cwd)
            elif name in SHELL_TOOLS:
                changed |= shell_changes_project(str(tool_input.get("command", "")), cwd, roots)

        # Codex: each finished step of a turn is an item_completed event
        payload = entry.get("payload") or {}
        item = payload.get("item") if payload.get("type") == "item_completed" else None
        if isinstance(item, dict):
            kind = item.get("type")
            if kind == "FileChange":
                changed |= edits_project(list(item.get("changes") or {}), cwd)
            elif kind == "CommandExecution":
                command, where = item.get("command"), str(item.get("cwd") or "")
                base = url2pathname(urlparse(where).path) if where.startswith("file:") else (where or cwd)
                changed |= shell_changes_project(" ".join(command) if isinstance(command, list)
                                                 else str(command or ""), base, roots)
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
    changed, saved = turn_activity(entries, cwd, project_dirs(cwd))
    return {"decision": "block", "reason": REASON.format(cwd=cwd)} if changed and not saved else None


def check(directory: str) -> None:
    """--check [DIR]: whether the hook is active in DIR, and its answer to a turn that edited a file there."""
    directory = os.path.abspath(directory)
    print(f"Directory list: {DIRECTORIES_FILE} ({'found' if DIRECTORIES_FILE.is_file() else 'missing'})")
    if not listed(directory):
        print(f"{directory} is not listed: the hook lets every turn there end without asking anything.")
        return
    print("Project directories (edits elsewhere are ignored):\n  " + "\n  ".join(project_dirs(directory)))
    session = f"check-{os.getpid()}"
    with tempfile.TemporaryDirectory() as tmp:  # a one-entry transcript: an agent that edited a file
        transcript = Path(tmp) / "transcript.jsonl"
        transcript.write_text(json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "example.txt"}}]}}) + "\n", encoding="utf-8")
        answer = decide({"session_id": session, "transcript_path": str(transcript), "cwd": directory})
    (STATE_DIR / f"{session}.json").unlink(missing_ok=True)
    if answer:
        print(f"{directory} is listed. After a turn that edits a file of the project, the hook keeps the turn "
              f"going with this instruction:\n\n{answer['reason']}")
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
