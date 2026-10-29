"""MCP server for the project memory: what agents changed, decided and found, shared by every
agent (Claude Code, Codex, ...) and every worktree of a project, in a Redis Stack vector index.

Tools:
- list_memory_contexts(): discover configured contexts and their project IDs from anywhere.
- search_project_memory(query): the 3 entries of this project most similar to the query (KNN over
  local sentence-transformers embeddings): notes saved by agents and imported docs.
- save_to_project_memory(title, content): optionally stores a note, embedded with the same model.

A call can explicitly select context/project, independently of the filesystem. Otherwise it uses
workdir or the client's MCP roots. Only stdio falls back to the server's cwd.

    python mcp_server.py                  # serve MCP over stdin/stdout (what Claude Code and Codex launch)
    python mcp_server.py --transport streamable-http  # one warm server on 127.0.0.1:8765/mcp
    python mcp_server.py --query "..."    # print one search to the terminal, to try it without a client
"""
from __future__ import annotations

import argparse
import json
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
from starlette.responses import JSONResponse
from memory_routing import configured_router

from memory_store import (INDEX_NAME, PROJECT_OVERRIDE, Chunk, Namespace, check_model, connect, embedding_text, ensure_index,
                          encode, fit_chunks, git, hold_wsl_open, index_exists, load_model, model_dimension,
                          mutate, namespace_of, project_of, tag, utc_timestamp, sync_status_key)

TOP_K = 3
# Every worktree holds its own copy of the shared docs, so the nearest neighbours are often one
# paragraph three times over. Fetch more candidates and fold identical chunks into one result.
CANDIDATES = 30
MAX_NOTE_CHARS = 6000

SEARCH_GUIDANCE = (
    "For questions spanning multiple topics or projects, first use list_memory_contexts and its "
    "resources to map each topic to the relevant context/project. A question spanning contexts or projects "
    "requires separate search_project_memory calls with focused subqueries in EVERY relevant context/project; "
    "do not send the whole mixed question to just one project or repeat it unchanged in every scope. "
    "Comparisons need evidence for each side, with additional focused searches if the top three "
    "results omit a part. Combine the findings, identifying each context/project and its sources. "
    "A miss in one scope says nothing about another. Multiple clearly requested scopes are not "
    "ambiguous intent: search each relevant scope without asking the user to choose only one. "
    "Do not search unrelated scopes for a single-topic question."
)


_model = None
_model_lock = threading.Lock()
# SentenceTransformer/tokenizers may change internal state during encode/tokenization. Share the
# model across clients, but serialize inference (including note chunk tokenization).
_inference_lock = threading.Lock()
_shared_http = False
_router = None


def destination(directory: str, *, writing: bool = False, context: str = "", project: str = ""):
    if _router is None:
        if context or project:
            raise ValueError("Explicit contexts require MEMORY_ROUTES. Use workdir with this unrouted server.")
        project, client = project_of(directory), connect()
    else:
        target, project = _router.select(context, project) if context or project else _router.resolve(directory)
        if writing and target.read_only:
            raise ValueError("This corpus is read-only through MCP. Edit and publish its curated Markdown instead.")
        client = _router.connect(target)
    return project, client


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


def _exit_with_parent(parent_pid: int) -> None:
    """Task Scheduler can kill PowerShell without its hidden Python children. Watch the original
    Windows process handle (not a reusable PID) and exit with it, even while the model is loading."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    parent = kernel32.OpenProcess(0x00100000, False, parent_pid)  # SYNCHRONIZE
    if not parent:
        raise RuntimeError("The scheduled MCP worker has already exited or cannot be monitored.")

    def watch():
        result = kernel32.WaitForSingleObject(parent, 0xFFFFFFFF)  # INFINITE
        kernel32.CloseHandle(parent)
        os._exit(0 if result == 0 else 1)

    threading.Thread(target=watch, name="worker-watch", daemon=True).start()


mcp = FastMCP(
    "project-memory",
    log_level="WARNING",  # the root logger, to stderr: keeps per-request and model-loading INFO lines out
    # Clients show these instructions even when they defer the tools, so they carry the rule on their own.
    instructions="Use list_memory_contexts to discover available memory. To access it from any directory, "
                 "pass context and, when required by that context, project. "
                 "Explicit selection takes precedence over workdir and client roots; no checkout is required. "
                 "Otherwise pass your absolute current project directory as workdir. Never guess an ambiguous "
                 "context or project. " + SEARCH_GUIDANCE + " Each context defines its own read-only status. "
                 "CRITICAL: this server is the long-term memory of the project you are working in, shared by every "
                 "agent and worktree: notes earlier sessions saved about what they changed and decided, plus the "
                 "project's docs and handoffs. Call search_project_memory WHENEVER the user asks about the "
                 "project's architecture, design, past sessions, decisions or an implementation that is not "
                 "visible in the files you have open, before answering, planning or editing. Do not guess and do "
                 "not say you lack context: search first (if the tool is deferred, load it by name). Save with "
                 "save_to_project_memory only new, lasting knowledge about this project (decisions and their "
                 "reasons, constraints, gotchas) that the code, commits and docs don't already show.",
)


@mcp.custom_route("/health", methods=["GET"])
async def health(request):
    """Process/model readiness only; normal tool calls also check Redis availability."""
    ready = _model is not None
    return JSONResponse({"status": "ready" if ready else "loading", "model_loaded": ready,
                         "pid": os.getpid()}, status_code=200 if ready else 503)


# --- Search ------------------------------------------------------------------------------------

def _field(doc, name: str) -> str:
    return getattr(doc, name, "") or ""


def search(query: str, directory: str, context: str = "", project: str = "") -> str:
    query = query.strip()
    if not query:
        return "Empty query: ask a question or name a topic."
    project, r = destination(directory, context=context, project=project)
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
    with _inference_lock:
        vector = encode(embedding_model(), [query])[0]
    candidates = CANDIDATES
    while True:
        knn = (Query(f"(@project:{{{tag(project)}}})=>[KNN {candidates} @embedding $vector AS distance]")
               .sort_by("distance")
               .return_fields("kind", "content", "heading", "source", "start_line", "end_line", "content_sha",
                              "note_id", "parts", "join_mode", "created", "updated_at", "source_modified_at", "agent", "branch", "worktree", "distance")
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
        if _field(best, "updated_at"):
            lines.append(f"updated in Redis: {best.updated_at}; source modified: {_field(best, 'source_modified_at') or 'unknown'}")
        if best.heading:
            lines.append(f"section: {best.heading}")
        if copies:
            lines.append("same text in: " + ", ".join(f"{c.source}:{c.start_line}-{c.end_line}" for c in copies))
        parts.append("\n".join(lines) + "\n\n" + best.content)
    count = f"{len(groups)} closest entr{'y' if len(groups) == 1 else 'ies'}"
    header = f'{count} of project "{project}" for "{query}" (cosine similarity, 1.0 = same meaning):'
    status = r.hgetall(sync_status_key(project))
    if status.get("last_success_completed_at"):
        header += f"\nLast successful document sync: {status['last_success_completed_at']} (latest attempt: {status.get('status', 'unknown')})."
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


def save(title: str, content: str, directory: str, agent: str, context: str = "", project: str = "") -> str:
    title, content = " ".join(title.split()), content.strip()
    if not title or not content:
        raise ValueError("A note needs both a title and content.")
    if len(content) > MAX_NOTE_CHARS:
        raise ValueError(f"The note has {len(content)} characters; condense it to what another agent needs "
                         f"to know, under {MAX_NOTE_CHARS}.")
    project, r = destination(directory, writing=True, context=context, project=project)
    if context:
        # The caller may be in an unrelated repo; do not attach its branch or the server's cwd.
        directory = ""
    model = embedding_model()
    namespace = ensure_index(r, model_dimension(model))

    with _inference_lock:
        pieces = note_chunks(content, model, title)
        vectors = encode(model, [embedding_text("note", "", title, piece) for piece in pieces])
    now = time.time()
    note_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(8)
    worktree = git(directory, "rev-parse", "--show-toplevel") if directory else ""
    branch = git(directory, "branch", "--show-current") if directory else ""
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
            "updated_at": utc_timestamp(now),
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
    """Resolve each call independently; a shared server must never guess the client's project."""
    if workdir.strip():
        if _shared_http and (not Path(workdir.strip()).is_absolute() or not Path(workdir.strip()).is_dir()):
            raise ValueError("workdir must be an existing absolute project directory on this machine.")
        return workdir.strip()
    directories = []
    if ctx.session.check_client_capability(types.ClientCapabilities(roots=types.RootsCapability())):
        try:
            with anyio.fail_after(3):
                roots = (await ctx.session.list_roots()).roots
            for root in roots:
                uri = urlparse(str(root.uri))
                if uri.scheme == "file" and uri.netloc in ("", "localhost"):
                    directory = url2pathname(uri.path)
                    if not _shared_http:
                        return directory
                    if Path(directory).is_absolute() and Path(directory).is_dir():
                        directories.append(str(Path(directory).resolve()))
        except Exception:  # a client that declares roots but does not answer: fall back
            pass
    if _shared_http:
        if len(set(directories)) == 1:
            return directories[0]
        raise ValueError("Pass workdir with your absolute current project directory, or select context "
                         "and project using list_memory_contexts. Without explicit selection, shared HTTP "
                         "needs workdir or exactly one valid local MCP root.")
    return os.getcwd()


@mcp.tool()
async def list_memory_contexts() -> str:
    """Discover contexts, project IDs, source document names and read-only status from any directory.

    No workdir or local checkout is needed. Use the returned context and project IDs with
    search_project_memory or save_to_project_memory. A fixed-project context needs only context;
    other contexts require an explicit project. The resources map shows which documents belong to
    each project, including components grouped under one shared project. Match the question's
    topics to those resources, allowing common acronym/spelling variants. For questions spanning contexts or projects,
    make separate focused search_project_memory calls in each relevant context/project
    and combine the evidence. Do not choose only one scope for a clearly mixed question.
    Writable contexts can create a new project using a valid project ID.
    """
    if _router is None:
        return json.dumps({"contexts": [], "message": "This server uses directory selection. "
                           "Configure MEMORY_ROUTES to enable explicit contexts, or pass workdir."})
    return json.dumps({"search_guidance": SEARCH_GUIDANCE,
                       "contexts": await anyio.to_thread.run_sync(_router.contexts)})


@mcp.tool()
async def search_project_memory(query: str, ctx: Context, workdir: str = "",
                                context: str = "", project: str = "") -> str:
    """CRITICAL: Use this tool WHENEVER the user asks about the project's architecture, design, past
    sessions, decisions or specific implementations that are not immediately visible in your current file
    context. DO NOT guess or apologize for lacking context: you MUST call this tool first to retrieve the
    project history before answering.

    It searches the long-term memory of the project you are working in: notes other agents saved about
    what they changed and decided, and the project's docs (design notes, handoffs, earlier memory files).
    Worktree routes share a memory. A configured shared corpus can include related repositories;
    configured Redis contexts are isolated. Select context/project to search from anywhere without
    a local checkout. Otherwise unmapped directories are rejected in routed mode.

    Each call searches ONLY ONE context/project. For a question spanning contexts/projects, first call
    list_memory_contexts to map topics using its resources, then split the question and search each
    relevant context/project separately with a focused subquery. For comparisons, retrieve evidence
    for each side. Do not send the full mixed question to just one project or treat its results as
    covering other scopes. Combine the findings with their context/project and source attribution.

    Ask in natural language ("how is authentication wired?", "why did the build move to clang?"). If a
    search misses, rephrase it (other words, the feature's or file's name) before concluding that nothing
    is recorded. Returns the 3 most similar entries and where each comes from: for a doc, open the file at
    the given lines for the surrounding context; curated sources are portable paths relative to
    the corpus bundle. A note is history, so check it against the current code.

    Args:
        query: a focused question about this context/project; split mixed-topic requests into separate calls
        workdir: absolute project directory for automatic routing; ignored when context/project is supplied
        context: context name from list_memory_contexts; overrides directory routing
        project: project ID in that context; required unless the context has a fixed project
    """
    directory = "" if context or project else await _directory(ctx, workdir)
    return await anyio.to_thread.run_sync(search, query, directory, context, project)


@mcp.tool()
async def save_to_project_memory(title: str, content: str, ctx: Context, workdir: str = "",
                                 context: str = "", project: str = "") -> str:
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
    A read-only corpus rejects saves; propose an edit to its curated Markdown instead.

    Args:
        title: one line naming what the note is about
        content: the note, in plain text or Markdown (at most 6000 characters)
        workdir: absolute project directory for automatic routing; ignored when context/project is supplied
        context: context name from list_memory_contexts; saves require read_only=false
        project: explicit project ID in the selected context; writable contexts allow new projects
    """
    directory = "" if context or project else await _directory(ctx, workdir)
    client = ctx.session.client_params.clientInfo if ctx.session.client_params else None
    return await anyio.to_thread.run_sync(save, title, content, directory, client.name if client else "", context, project)


def main() -> None:
    global _shared_http, _router
    parser = argparse.ArgumentParser(description="Project-memory MCP server (stdio or shared local HTTP).")
    parser.add_argument("--query", help="print one search and exit instead of serving MCP")
    parser.add_argument("--workdir", default=os.getcwd(), help="directory whose project --query searches")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--port", type=int, default=8765, help="local HTTP port (default: 8765)")
    parser.add_argument("--parent-pid", type=int, help="Windows HTTP worker PID; exit when that process ends")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    _shared_http = args.transport == "streamable-http"
    if _shared_http and (args.query is not None or PROJECT_OVERRIDE):
        parser.error("shared HTTP cannot use --query or MEMORY_PROJECT; use per-call workdir and repository IDs")
    _router = configured_router()
    if _router and PROJECT_OVERRIDE:
        parser.error("MEMORY_ROUTES cannot be combined with MEMORY_PROJECT")
    if args.parent_pid is not None:
        if sys.platform != "win32" or not _shared_http or args.parent_pid <= 0:
            parser.error("--parent-pid requires Windows HTTP mode and a positive worker PID")
        _exit_with_parent(args.parent_pid)
    hold_wsl_open()  # WSL and Redis start up while the model loads
    if args.query is None:
        if _shared_http:
            # Warm both the model and its first inference before accepting clients. Failure exits
            # the process so the sign-in worker can retry; never advertise a broken warm server.
            started = time.monotonic()
            with _inference_lock:
                encode(embedding_model(), ["Project memory startup"])
            if _router:
                for target in _router.targets.values():
                    _router.connect(target)
            else:
                check_model(connect())
            mcp.settings.host = "127.0.0.1"
            mcp.settings.port = args.port
            print(f"Model ready in {time.monotonic() - started:.1f}s; serving "
                  f"http://127.0.0.1:{args.port}/mcp (pid {os.getpid()})", flush=True)
            mcp.run(transport="streamable-http")
            return
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
