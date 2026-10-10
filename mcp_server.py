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
import functools
import os
import re
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import anyio
from mcp import types
from mcp.server.fastmcp import Context, FastMCP
from redis.commands.search.query import Query

from memory_store import (INDEX_NAME, MAX_CHARS, NOTE_PREFIX, check_model, connect, create_index, embedding_text,
                          encode, git, hold_wsl_open, index_exists, load_model, model_dimension, project_of,
                          split_long, tag)

TOP_K = 3
# Every worktree holds its own copy of the shared docs, so the nearest neighbours are often one
# paragraph three times over. Fetch more candidates and fold identical chunks into one result.
CANDIDATES = 30
MAX_NOTE_CHARS = 6000


@functools.cache
def embedding_model():
    # main() calls this before the server reads stdin, never alongside it. On Windows, scipy's Fortran
    # runtime (which sentence-transformers pulls in through scikit-learn) probes stdin while it loads,
    # and that probe blocks for as long as the MCP transport has a read pending on the same pipe.
    return load_model()


mcp = FastMCP(
    "project-memory",
    log_level="WARNING",  # the root logger, to stderr: keeps per-request and model-loading INFO lines out
    instructions="Long-term memory of the project you work in, shared by every agent and worktree on it: notes "
                 "agents saved about what they changed and decided, and imported docs. Search it before working "
                 "on a topic to recover earlier decisions and constraints instead of re-deriving them; save a note "
                 "when you change something other agents should know about.",
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
    if not index_exists(r):
        return "The project memory is empty: nothing has been saved or imported yet."
    check_model(r)

    vector = encode(embedding_model(), [query])[0]
    knn = (Query(f"(@project:{{{tag(project)}}})=>[KNN {CANDIDATES} @embedding $vector AS distance]")
           .sort_by("distance")
           .return_fields("kind", "content", "heading", "source", "start_line", "end_line", "content_sha",
                          "note_id", "parts", "created", "agent", "branch", "worktree", "distance")
           .paging(0, CANDIDATES)
           .dialect(2))
    docs = r.ft(INDEX_NAME).search(knn, query_params={"vector": vector.tobytes()}).docs

    groups: dict[str, list] = {}  # one result per note, and per doc text that several worktrees share
    for doc in docs:
        key = _field(doc, "note_id") or _field(doc, "content_sha")
        if key in groups or len(groups) < TOP_K:
            groups.setdefault(key, []).append(doc)
    if not groups:
        return f'Nothing in the memory of project "{project}" yet.'

    parts = []
    for i, (best, *copies) in enumerate(groups.values(), 1):
        similarity = f"similarity {1 - float(best.distance):.2f}"
        if _field(best, "kind") == "note":
            parts.append(f"[{i}] {_describe_note(best)}  {similarity}\n{_note_text(r, best)}")
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


def _note_text(r, doc) -> str:
    """The whole note, whichever of its chunks matched."""
    keys = [f"{NOTE_PREFIX}{doc.note_id}:{n}" for n in range(int(_field(doc, "parts") or 1))]
    pipe = r.pipeline(transaction=False)
    for key in keys:
        pipe.hget(key, "content")
    return f"title: {doc.heading}\n\n" + "\n\n".join(part for part in pipe.execute() if part)


# --- Save --------------------------------------------------------------------------------------

def note_chunks(content: str) -> list[str]:
    """A note in pieces of at most MAX_CHARS, cut between paragraphs; headings stay in the text."""
    pieces: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", content):
        for _, _, piece in split_long(list(enumerate(para.splitlines(), 1))):
            if buf and len(buf) + 2 + len(piece) > MAX_CHARS:
                pieces.append(buf)
                buf = ""
            buf = f"{buf}\n\n{piece}" if buf else piece
    if buf:
        pieces.append(buf)
    return pieces


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
    if not index_exists(r):
        create_index(r, model_dimension(model))
    check_model(r)

    pieces = note_chunks(content)
    vectors = encode(model, [embedding_text("note", "", title, piece) for piece in pieces])
    now = time.time()
    note_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(3)
    worktree = git(directory, "rev-parse", "--show-toplevel")
    branch = git(directory, "branch", "--show-current")
    pipe = r.pipeline(transaction=True)
    for n, (piece, vector) in enumerate(zip(pieces, vectors)):
        pipe.hset(f"{NOTE_PREFIX}{note_id}:{n}", mapping={
            "kind": "note",
            "project": project,
            "note_id": note_id,
            "parts": len(pieces),
            "heading": title,
            "content": piece,
            "source": f"note {note_id}",
            "created": int(now),
            "agent": agent,
            "branch": branch,
            "worktree": str(Path(worktree)) if worktree else directory,
            "embedding": vector.tobytes(),
        })
    pipe.execute()
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
    """Search the long-term memory of the project you are working in: notes other agents saved about what
    they changed and decided, and imported docs (design notes, handoffs, earlier memory files). Every
    worktree of a repository shares one memory.

    Ask in natural language ("how is the fight stance animated?", "why did the build move to clang?").
    Returns the 3 most similar entries and where each comes from; for a doc, open the file at the given
    lines for the surrounding context.

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

    Record what another agent would need and cannot see from the code alone: what you changed and why,
    the decisions taken and the alternatives rejected, constraints and gotchas found, and what is
    unfinished or unverified. Write it to stand on its own for a reader who has not seen this
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
    embedding_model()  # 5-15 s, mostly importing torch
    if args.query is None:
        mcp.run()  # stdio transport: stdout carries the protocol, so nothing else may print there
        return
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        print(search(args.query, args.workdir))
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
