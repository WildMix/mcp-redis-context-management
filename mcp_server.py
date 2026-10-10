"""Stdio MCP server over the project memory that migrate_memory.py indexes into Redis Stack.

One tool, search_project_memory(query): it embeds the query with the same local sentence-transformers
model the index was built with and returns the 3 nearest Markdown chunks (KNN over the cosine HNSW
index), each with its source file, line range and section.

    python mcp_server.py                  # serve MCP over stdin/stdout (what Claude Code and Codex launch)
    python mcp_server.py --query "..."    # print one search to the terminal, to try it without a client
"""
from __future__ import annotations

import argparse
import functools
import sys

import anyio
import numpy as np
from mcp.server.fastmcp import FastMCP
from redis.commands.search.query import Query

from memory_redis import HERE, INDEX_NAME, META_KEY, MODEL_NAME, connect, hold_wsl_open

TOP_K = 3
# Every worktree holds its own copy of the shared docs, so the nearest neighbours are often one
# paragraph three times over. Fetch more candidates and fold identical chunks into one result.
CANDIDATES = 30


@functools.cache
def embedding_model():
    # main() calls this before the server reads stdin, never alongside it. On Windows, scipy's Fortran
    # runtime (which sentence-transformers pulls in through scikit-learn) probes stdin while it loads,
    # and that probe blocks for as long as the MCP transport has a read pending on the same pipe.
    from sentence_transformers import SentenceTransformer

    try:  # from the local cache, without asking the Hugging Face Hub for updates
        return SentenceTransformer(MODEL_NAME, local_files_only=True)
    except Exception:  # not downloaded yet
        return SentenceTransformer(MODEL_NAME)


mcp = FastMCP(
    "project-memory",
    log_level="WARNING",  # the root logger, to stderr: keeps per-request and model-loading INFO lines out
    instructions="Long-term project memory shared by every agent and worktree: docs, design notes, handoffs "
                 "and agent memory files. Search it before working on a topic to recover earlier decisions "
                 "and constraints instead of re-deriving them.",
)


def search(query: str) -> str:
    query = query.strip()
    if not query:
        return "Empty query: ask a question or name a topic."
    r = connect()
    built_with = r.hget(META_KEY, "model")
    if built_with is None:
        raise RuntimeError(f"Index '{INDEX_NAME}' does not exist yet: run `python migrate_memory.py` in {HERE}.")
    if built_with != MODEL_NAME:
        raise RuntimeError(f"Index '{INDEX_NAME}' was built with {built_with}, but this server embeds with "
                           f"{MODEL_NAME}: set EMBEDDING_MODEL to match, or rebuild with `migrate_memory.py --reset`.")

    vector = embedding_model().encode(query, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)
    knn = (Query(f"*=>[KNN {CANDIDATES} @embedding $vector AS distance]")
           .sort_by("distance")
           .return_fields("content", "heading", "source", "start_line", "end_line", "content_sha", "distance")
           .paging(0, CANDIDATES)
           .dialect(2))
    docs = r.ft(INDEX_NAME).search(knn, query_params={"vector": vector.tobytes()}).docs

    groups: dict[str, list] = {}  # content_sha -> that chunk in each worktree, nearest first
    for doc in docs:
        if doc.content_sha in groups or len(groups) < TOP_K:
            groups.setdefault(doc.content_sha, []).append(doc)
    if not groups:
        return f'No matches for "{query}" in the project memory.'

    parts = []
    for i, (best, *copies) in enumerate(groups.values(), 1):
        lines = [f"[{i}] {best.source}:{best.start_line}-{best.end_line}  similarity {1 - float(best.distance):.2f}"]
        if best.heading:
            lines.append(f"section: {best.heading}")
        if copies:
            lines.append("same text in: " + ", ".join(f"{c.source}:{c.start_line}-{c.end_line}" for c in copies))
        parts.append("\n".join(lines) + "\n\n" + best.content)
    header = f'Top {len(groups)} passages for "{query}" (cosine similarity, 1.0 = same meaning):'
    return header + "\n\n" + "\n\n---\n\n".join(parts)


@mcp.tool()
async def search_project_memory(query: str) -> str:
    """Search the project's shared long-term memory: the Markdown docs, design notes, handoffs and agent
    memory files of every indexed worktree.

    Ask in natural language ("how is the fight stance animated?", "which asset licences are allowed?").
    Returns the 3 most similar passages, each with its file path, line range and section heading; open
    the file at those lines when you need the surrounding context.

    Args:
        query: the question or topic to look up
    """
    return await anyio.to_thread.run_sync(search, query)


def main() -> None:
    parser = argparse.ArgumentParser(description="Project-memory MCP server (stdio).")
    parser.add_argument("--query", help="print one search and exit instead of serving MCP")
    args = parser.parse_args()
    hold_wsl_open()  # WSL and Redis start up while the model loads
    embedding_model()  # 5-15 s, mostly importing torch
    if args.query is None:
        mcp.run()  # stdio transport: stdout carries the protocol, so nothing else may print there
        return
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        print(search(args.query))
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    main()
