"""Real MCP peer for context-source transport/contract tests; no external services."""
import json
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("source-fixture", log_level="ERROR")


def record(tool, args):
    with Path(os.environ["SOURCE_TEST_CALLS"]).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"tool": tool, "args": args, "pid": os.getpid()}) + "\n")


@mcp.tool()
def context_search(query: str, limit: int) -> dict:
    record("context_search", {"query": query, "limit": limit})
    return {"results": [{"id": "page-1", "title": "Architecture", "url": "https://example.org/page-1",
                         "excerpt": "A current source statement."}][:limit]}


@mcp.tool()
def context_fetch(id: str) -> dict:
    record("context_fetch", {"id": id})
    return {"id": id, "title": "Architecture", "url": "https://example.org/page-1",
            "content": "A current source statement.", "version": "7", "updatedAt": "2026-10-29T12:00:00Z"}


@mcp.tool()
def write_page(text: str) -> str:
    record("write_page", {"text": text})
    raise RuntimeError("Context gathering must never call this tool")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        mcp.settings.port = int(sys.argv[1])
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
