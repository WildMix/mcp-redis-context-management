"""Stdio MCP server for the project memory: what agents changed, decided and found, shared by every
agent (Claude Code, Codex, ...) and every worktree of a project, in a Redis Stack vector index.

Tools:
- search_project_memory(query): the 3 entries of this project most similar to the query (KNN over
  local sentence-transformers embeddings): notes saved by agents and imported docs.
- save_to_project_memory(title, content): stores a note, embedded with the same model. The Stop hook
  in hooks/ asks agents to call it at the end of a turn that changed files.

A call is about the project of the directory the agent works in (see memory_store.project_of): the
workdir argument if given, else the client's MCP roots, else this server's working directory, which
Claude Code and Codex set to the session's.

    python mcp_server.py                  # serve MCP over stdin/stdout (what Claude Code and Codex launch)
    python mcp_server.py --query "..."    # print one search to the terminal, to try it without a client
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import anyio
from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from redis.commands.search.query import Query

from memory_store import (INDEX_NAME, Chunk, Namespace, check_model, connect, embedding_text, ensure_index,
                          encode, fit_chunks, git, hold_wsl_open, index_exists, load_model, model_dimension,
                          mutate, namespace_of, project_of, tag)

TOP_K = 3
# Every worktree holds its own copy of the shared docs, so the nearest neighbours are often one
# paragraph three times over. Fetch more candidates and fold identical chunks into one result.
CANDIDATES = 30
MAX_NOTE_CHARS = 6000


_model = None
_model_lock = threading.Lock()


def embedding_model():
    """The model, loaded once; main() starts loading it in the background as the server starts (5-30 s,
    mostly importing torch), and the first search or save waits for it."""
    global _model
    with _model_lock:
        if _model is None:
            _model = load_model()
        return _model


def _feed_stdin_without_pending_reads() -> None:
    """Windows only: give the MCP transport a private pipe, filled by a thread that reads stdin only when
    data is already waiting, so no read ever stays pending on stdin.

    A pending read on a pipe blocks every other operation on it, even a type query. scipy's Fortran
    runtime (pulled in by sentence-transformers through scikit-learn) queries stdin while it loads, so
    loading the model while the transport waited on stdin would deadlock; the handshake would then have
    to wait for the model, and a cold start (30 s and more) overruns the clients' connection timeouts."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.PeekNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
                                       ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    stdin_fd = sys.stdin.fileno()
    handle = msvcrt.get_osfhandle(stdin_fd)
    if kernel32.GetFileType(handle) != 3:  # FILE_TYPE_PIPE; a console or a file is never left blocked
        return
    read_end, write_end = os.pipe()

    def pump() -> None:
        waiting = wintypes.DWORD()
        try:  # PeekNamedPipe fails once the client closes its end
            while kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(waiting), None):
                if not waiting.value:
                    time.sleep(0.01)
                    continue
                data = os.read(stdin_fd, waiting.value)  # returns at once: the bytes are there
                while data:
                    data = data[os.write(write_end, data):]
        finally:
            os.close(write_end)  # end of input for the transport, which then shuts the server down

    threading.Thread(target=pump, name="stdin-pump", daemon=True).start()
    sys.stdin = open(read_end, encoding="utf-8")  # FastMCP's stdio transport reads sys.stdin.buffer


mcp = FastMCP(
    "project-memory",
    log_level="WARNING",  # the root logger, to stderr: keeps per-request and model-loading INFO lines out
    # Clients show these instructions even when they defer the tools, so they carry the rule on their own.
    instructions="CRITICAL: this server is the long-term memory of the project you are working in, shared by every "
                 "agent and worktree: notes earlier sessions saved about what they changed and decided, plus the "
                 "project's docs and handoffs. Call search_project_memory WHENEVER the user asks about the "
                 "project's architecture, design, past sessions, decisions or an implementation that is not "
                 "visible in the files you have open, before answering, planning or editing. Do not guess and do "
                 "not say you lack context: search first (if the tool is deferred, load it by name). Save with "
                 "save_to_project_memory only new, lasting knowledge about this project (decisions and their "
                 "reasons, constraints, gotchas) that the code, commits and docs don't already show.",
)


# --- Search ------------------------------------------------------------------------------------

def _field(doc, name: str) -> str:
    return getattr(doc, name, "") or ""


def search(query: str, directory: str) -> str:
    query = query.strip()
    if not query:
        return "Empty query: ask a question or name a topic."
    project = project_of(directory)
    r = connect()
    for _ in range(3):
        namespace = namespace_of(r)
        try:
            if not index_exists(r, namespace.index):
                result = "The project memory is empty: nothing has been saved or imported yet."
            else:
                check_model(r)
                result = _search(query, project, r, namespace)
        except Exception:
            if namespace_of(r) != namespace:
                continue
            raise
        if namespace_of(r) == namespace:
            return result
    raise RuntimeError("The memory was rebuilt repeatedly during this search; retry it.")


def _search(query: str, project: str, r, namespace: Namespace) -> str:
    vector = encode(embedding_model(), [query])[0]
    candidates = CANDIDATES
    while True:
        knn = (Query(f"(@project:{{{tag(project)}}})=>[KNN {candidates} @embedding $vector AS distance]")
               .sort_by("distance")
               .return_fields("kind", "content", "heading", "source", "start_line", "end_line", "content_sha",
                              "note_id", "parts", "join_mode", "created", "agent", "branch", "worktree", "distance")
               .paging(0, candidates)
               .dialect(2))
        docs = r.ft(namespace.index).search(knn, query_params={"vector": vector.tobytes()}).docs
        groups: dict[str, list] = {}
        for doc in docs:
            key = _field(doc, "note_id") or _field(doc, "content_sha") or doc.id
            if key in groups or len(groups) < TOP_K:
                groups.setdefault(key, []).append(doc)
        if len(groups) >= TOP_K or len(docs) < candidates:
            break
        candidates *= 2
    if not groups:
        return f'Nothing in the memory of project "{project}" yet.'

    parts = []
    for i, (best, *copies) in enumerate(groups.values(), 1):
        similarity = f"similarity {1 - float(best.distance):.2f}"
        if _field(best, "kind") == "note":
            parts.append(f"[{i}] {_describe_note(best)}  {similarity}\n{_note_text(r, best, namespace)}")
            continue
        lines = [f"[{i}] doc {best.source}:{best.start_line}-{best.end_line}  {similarity}"]
        if best.heading:
            lines.append(f"section: {best.heading}")
        if copies:
            lines.append("same text in: " + ", ".join(f"{c.source}:{c.start_line}-{c.end_line}" for c in copies))
        parts.append("\n".join(lines) + "\n\n" + best.content)
    count = f"{len(groups)} closest entr{'y' if len(groups) == 1 else 'ies'}"
    header = f'{count} of project "{project}" for "{query}" (cosine similarity, 1.0 = same meaning):'
    return header + "\n\n" + "\n\n---\n\n".join(parts)


def _describe_note(doc) -> str:
    when = datetime.fromtimestamp(int(_field(doc, "created") or 0)).strftime("%Y-%m-%d %H:%M")
    by = f" by {doc.agent}" if _field(doc, "agent") else ""
    on = f" on branch {doc.branch}" if _field(doc, "branch") else ""
    where = f" in {doc.worktree}" if _field(doc, "worktree") else ""
    return f"note of {when}{by}{on}{where}"


def _note_text(r, doc, namespace: Namespace = Namespace()) -> str:
    """The whole note, whichever of its chunks matched."""
    keys = [f"{namespace.notes}{doc.note_id}:{n}" for n in range(int(_field(doc, "parts") or 1))]
    pipe = r.pipeline(transaction=False)
    for key in keys:
        pipe.hget(key, "content")
    parts = pipe.execute()
    if any(part is None for part in parts):
        raise RuntimeError(f"Note {doc.note_id} has missing chunks; retry after checking the stored records.")
    separator = "" if _field(doc, "join_mode") == "exact" else "\n\n"
    return f"title: {doc.heading}\n\n" + separator.join(parts)


# --- Save --------------------------------------------------------------------------------------

def note_chunks(content: str, model, title: str) -> list[str]:
    """Exact slices of a note, including title tokens in each piece's budget."""
    return [chunk.text for chunk in fit_chunks(model, "note", "", [Chunk(title, content, 1, 1)])]


def save(title: str, content: str, directory: str, agent: str) -> str:
    title, content = " ".join(title.split()), content.strip()
    if not title or not content:
        raise ValueError("A note needs both a title and content.")
    if len(content) > MAX_NOTE_CHARS:
        raise ValueError(f"The note has {len(content)} characters; condense it to what another agent needs "
                         f"to know, under {MAX_NOTE_CHARS}.")
    project = project_of(directory)
    r = connect()
    model = embedding_model()
    namespace = ensure_index(r, model_dimension(model))

    pieces = note_chunks(content, model, title)
    vectors = encode(model, [embedding_text("note", "", title, piece) for piece in pieces])
    now = time.time()
    note_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(8)
    worktree = git(directory, "rev-parse", "--show-toplevel")
    branch = git(directory, "branch", "--show-current")
    records = []
    for n, (piece, vector) in enumerate(zip(pieces, vectors)):
        records.append((f"{namespace.notes}{note_id}:{n}", {
            "kind": "note",
            "project": project,
            "note_id": note_id,
            "parts": len(pieces),
            "join_mode": "exact",
            "heading": title,
            "content": piece,
            "source": f"note {note_id}",
            "created": int(now),
            "agent": agent,
            "branch": branch,
            "worktree": str(Path(worktree)) if worktree else directory,
            "embedding": vector.tobytes(),
        }))
    def queue(pipe):
        for key, fields in records:
            pipe.hset(key, mapping=fields)
    mutate(r, namespace, queue)
    return f'Saved note {note_id} to the memory of project "{project}".'


# --- Tools -------------------------------------------------------------------------------------

async def _directory(ctx: Context, workdir: str) -> str:
    """The directory whose project a call is about: workdir if given, else the client's first file root,
    else this server's working directory (Claude Code and Codex start the server in the session's)."""
    if workdir.strip():
        return workdir.strip()
    if ctx.session.check_client_capability(types.ClientCapabilities(roots=types.RootsCapability())):
        try:
            with anyio.fail_after(3):
                roots = (await ctx.session.list_roots()).roots
            for root in roots:
                if str(root.uri).startswith("file:"):
                    return url2pathname(urlparse(str(root.uri)).path)
        except Exception:  # a client that declares roots but does not answer: fall back
            pass
    return os.getcwd()


@mcp.tool()
async def search_project_memory(query: str, ctx: Context, workdir: str = "") -> str:
    """CRITICAL: Use this tool WHENEVER the user asks about the project's architecture, design, past
    sessions, decisions or specific implementations that are not immediately visible in your current file
    context. DO NOT guess or apologize for lacking context: you MUST call this tool first to retrieve the
    project history before answering.

    It searches the long-term memory of the project you are working in: notes other agents saved about
    what they changed and decided, and the project's docs (design notes, handoffs, earlier memory files).
    Every worktree of a repository shares one memory, and results come from this project only.

    Ask in natural language ("how is authentication wired?", "why did the build move to clang?"). If a
    search misses, rephrase it (other words, the feature's or file's name) before concluding that nothing
    is recorded. Returns the 3 most similar entries and where each comes from: for a doc, open the file at
    the given lines for the surrounding context; a note is history, so check it against the current code.

    Args:
        query: the question or topic to look up
        workdir: your current working directory; needed only if this server runs elsewhere
    """
    directory = await _directory(ctx, workdir)
    return await anyio.to_thread.run_sync(search, query, directory)


@mcp.tool()
async def save_to_project_memory(title: str, content: str, ctx: Context, workdir: str = "") -> str:
    """Save a note to the long-term memory of the project you are working in, where every other agent and
    worktree on the project finds it with search_project_memory.

    Save only knowledge that is all of:
    - about this project itself (its code, design, architecture, assets, build or workflow), not about
      other repositories, your tools, the agent setup or this memory system;
    - new: not already in the memory, and not plain from the code, a diff, a commit message or the docs;
    - lasting: a decision and its reasons, a rejected alternative, a constraint, a gotcha, a non-obvious
      cause, a known limitation or unfinished piece. A log of edits or of this session is not.
    When in doubt, don't save. Write the note to stand on its own for a reader who has not seen this
    conversation, and leave out secrets.

    Args:
        title: one line naming what the note is about
        content: the note, in plain text or Markdown (at most 6000 characters)
        workdir: your current working directory; needed only if this server runs elsewhere
    """
    directory = await _directory(ctx, workdir)
    client = ctx.session.client_params.clientInfo if ctx.session.client_params else None
    return await anyio.to_thread.run_sync(save, title, content, directory, client.name if client else "")


def main() -> None:
    parser = argparse.ArgumentParser(description="Project-memory MCP server (stdio).")
    parser.add_argument("--query", help="print one search and exit instead of serving MCP")
    parser.add_argument("--workdir", default=os.getcwd(), help="directory whose project --query searches")
    args = parser.parse_args()
    hold_wsl_open()  # WSL and Redis start up while the model loads
    if args.query is None:
        if sys.platform == "win32":
            _feed_stdin_without_pending_reads()
        threading.Thread(target=embedding_model, name="model-loader", daemon=True).start()
        mcp.run()  # stdio transport: stdout carries the protocol, so nothing else may print there
        return
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        print(search(args.query, args.workdir))
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
