"""Extract project-scoped agent context into Markdown for the existing importer.

Only readable memories, summaries and (optionally) conversation text are sources.
Configuration, credentials, tool output and encrypted state are not source formats.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from memory_store import git


SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "cache", "plugins",
             "vendor_imports", ".sandbox", "tmp", "tool-results"}


def exists(path: Path) -> bool:
    """Missing sources are allowed; an unreadable source must not look like a deletion."""
    try:
        path.stat()
        return True
    except FileNotFoundError:
        return False


def normalized(path: str | Path) -> str:
    value = str(path).strip().strip('`"\'').replace("\\", "/")
    if value.startswith("//?/UNC/"):
        value = "//" + value[8:]
    elif value.startswith("//?/"):
        value = value[4:]
    if os.name == "nt":
        value = re.sub(r"^/(?:mnt/)?([A-Za-z])(?=/)", r"\1:", value)
        value = value.lower()
    return value.rstrip("/")


def belongs(cwd: str, roots: list[Path]) -> bool:
    value = normalized(cwd)
    return bool(value) and any(value == normalized(root) or value.startswith(normalized(root) + "/")
                               for root in roots)


def project_roots(roots: list[Path]) -> list[Path]:
    """Include linked worktrees without matching unrelated repositories by folder name."""
    found = set(roots)
    for root in roots:
        has_git = any((parent / ".git").exists() for parent in (root, *root.parents))
        if git(root, "rev-parse", "--is-inside-work-tree", strict=has_git) != "true":
            continue
        listing = git(root, "worktree", "list", "--porcelain", "-z", strict=True)
        found.update(Path(field[9:]).resolve() for field in listing.split("\0")
                     if field.startswith("worktree "))
    return sorted(found)


def files_under(root: Path, suffix: str) -> list[Path]:
    if not exists(root):
        return []
    def failed(error):
        raise error
    paths = []
    for directory, dirs, files in os.walk(root, onerror=failed, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(directory) / d).is_symlink()]
        paths.extend(Path(directory) / name for name in files
                     if name.lower().endswith(suffix) and not (Path(directory) / name).is_symlink())
    return sorted(paths)


@dataclass(frozen=True)
class Extract:
    source: Path
    category: str
    text: str

    @property
    def filename(self) -> Path:
        digest = hashlib.sha256(str(self.source).encode("utf-8")).hexdigest()[:20]
        # Keep unique cache IDs out of the filename used in embedding text.
        return Path(f"agent-{digest}") / "context.md"

    def render(self) -> str:
        return f"Original source: `{self.source}`\n\n{self.text.strip()}\n"


def sections(text: str, pattern: str) -> list[tuple[int, str]]:
    starts = [match.start() for match in re.finditer(pattern, text, re.MULTILINE)]
    return [(text.count("\n", 0, start) + 1, text[start:end])
            for start, end in zip(starts, starts[1:] + [len(text)])]


def cwds(text: str) -> list[str]:
    return re.findall(r"^cwd:\s*(.+)$", text, re.MULTILINE)


def scoped_markdown(path: Path, roots: list[Path]) -> str:
    text = path.read_text(encoding="utf-8-sig")
    directories = cwds(text)
    return text if directories and all(belongs(cwd, roots) for cwd in directories) else ""


def message_text(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(block["text"] for block in content if isinstance(block, dict)
                       and block.get("type") in {"text", "input_text", "output_text"}
                       and isinstance(block.get("text"), str))


def session_context(path: Path, agent: str, roots: list[Path], transcripts: bool,
                    seen_cwds: set[str] | None = None) -> list[Extract]:
    """Read each session once. Mixed-project sessions are deliberately left unassigned."""
    summaries, messages = [], []
    matched = False
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                # An active agent can still be writing its final record.
                if not line.endswith("\n"):
                    break
                raise RuntimeError(f"Invalid session JSON at {path}:{number}") from error
            if not isinstance(row, dict):
                continue
            kind = row.get("type")
            payload = row.get("payload") or {}
            if not isinstance(payload, dict):
                payload = {}
            cwd = row.get("cwd") if agent == "Claude" else (
                payload.get("cwd") if kind in {"session_meta", "turn_context"} else None)
            if cwd:
                if seen_cwds is not None:
                    seen_cwds.add(cwd)
                if not belongs(cwd, roots):
                    return []
                matched = True

            summary, message = "", {}
            if agent == "Claude":
                if kind in {"user", "assistant"} and isinstance(row.get("message"), dict):
                    message = row["message"]
                    if row.get("isCompactSummary"):
                        summary = message_text(message)
                        message = {}
            else:
                if kind == "compacted" and isinstance(payload.get("message"), str):
                    summary = payload["message"]
                elif kind == "response_item" and payload.get("type") == "message":
                    message = payload
                    if message.get("channel") == "summary":
                        summary = message_text(message)
                        message = {}
            if summary.strip():
                summaries.append(f"## {agent} summary, source line {number}\n\n{summary.strip()}")
            if (transcripts and message.get("role") in {"user", "assistant"}
                    and message.get("channel") not in {"analysis", "justify", "confidence"}):
                text = message_text(message).strip()
                if text:
                    messages.append(f"## {agent} {message['role']}, source line {number}\n\n{text}")
    if not matched:
        return []
    return [Extract(path, category, "\n\n".join(parts)) for category, parts in
            (("summaries", summaries), ("transcripts", messages)) if parts]


def discover(roots: list[Path], claude_home: Path, codex_home: Path,
             transcripts: bool = False) -> list[Extract]:
    extracts: dict[tuple[Path, str], Extract] = {}

    def add(path: Path, text: str, category: str = "memories"):
        if text.strip():
            extracts[path, category] = Extract(path, category, text)

    # Project-local knowledge is often intentionally gitignored.
    for root in roots:
        for name in (".claude", ".codex"):
            for path in files_under(root / name, ".md"):
                add(path, path.read_text(encoding="utf-8-sig"))

    claude_projects = claude_home / "projects"
    if exists(claude_projects):
        for directory in sorted(claude_projects.iterdir()):
            if not directory.is_dir() or directory.is_symlink():
                continue
            # Claude's project directories encode the absolute checkout path.
            encoded_match = any(directory.name == re.sub(r"[^a-zA-Z0-9]", "-", str(root)) for root in roots)
            session_extracts = []
            directories = set()
            for path in files_under(directory, ".jsonl"):
                session_extracts.extend(session_context(path, "Claude", roots, transcripts, directories))
            for item in session_extracts:
                add(item.source, item.text, item.category)
            matched = all(belongs(cwd, roots) for cwd in directories) if directories else encoded_match
            if matched:
                for path in files_under(directory / "memory", ".md"):
                    add(path, path.read_text(encoding="utf-8-sig"))

    memories = codex_home / "memories"
    selected_summaries = set()
    for path in files_under(memories / "rollout_summaries", ".md"):
        text = scoped_markdown(path, roots)
        if text:
            selected_summaries.add(path.name)
            add(path, text)

    # These files combine many projects; importing them whole would pollute every project.
    registry = memories / "MEMORY.md"
    if exists(registry):
        parts = []
        for line, text in sections(registry.read_text(encoding="utf-8-sig"), r"^# Task Group:"):
            references = set(re.findall(r"rollout_summaries/([^\s)]+\.md)", text))
            if references and references <= selected_summaries:
                parts.append(f"Original section starts at line {line}.\n\n{text.strip()}")
        add(registry, "\n\n".join(parts))
    raw = memories / "raw_memories.md"
    if exists(raw):
        parts = []
        for line, text in sections(raw.read_text(encoding="utf-8-sig"), r"^## Thread "):
            directories = cwds(text)
            if directories and all(belongs(cwd, roots) for cwd in directories):
                parts.append(f"Original section starts at line {line}.\n\n{text.strip()}")
        add(raw, "\n\n".join(parts))

    for folder in ("sessions", "archived_sessions"):
        for path in files_under(codex_home / folder, ".jsonl"):
            for item in session_context(path, "Codex", roots, transcripts):
                add(item.source, item.text, item.category)
    return sorted(extracts.values(), key=lambda item: (item.category, str(item.source)))


def export(extracts: list[Extract], destination: Path, transcripts: bool = False) -> list[Path]:
    """Maintain only our generated files, after discovery has completed successfully.

    Separate directories keep an import without transcripts from pruning a prior transcript import.
    """
    roots = []
    for category in ("memories", "summaries", "transcripts") if transcripts else ("memories", "summaries"):
        directory = destination / category
        directory.mkdir(parents=True, exist_ok=True)
        roots.append(directory)
        current = set()
        for item in extracts:
            if item.category != category:
                continue
            path = directory / item.filename
            path.parent.mkdir(parents=True, exist_ok=True)
            current.add(path)
            rendered = item.render()
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                temporary = path.with_suffix(".tmp")
                temporary.write_text(rendered, encoding="utf-8")
                temporary.replace(path)
        for path in directory.glob("agent-*/context.md"):
            if re.fullmatch(r"agent-[0-9a-f]{20}", path.parent.name) and path not in current:
                path.unlink()
    return roots
