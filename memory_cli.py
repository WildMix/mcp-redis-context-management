"""Project memory from a shell, for agents without MCP support (for example DwarfStar's ds4-agent,
which only has a bash tool). A thin client of the shared HTTP server: the embedding model stays
loaded there, so a call takes about a second instead of the 5-30 s a standalone process needs.

    memory search "why did the build move to clang?"   # project of the current directory
    memory search -c personal -p ds4 "..."             # explicit context/project
    memory save "Title" <<'EOF'                        # note content comes from stdin
    ...
    EOF
    memory contexts                                    # contexts and their project IDs

MEMORY_URL selects the server; MEMORY_CONTEXT and MEMORY_PROJECT give default selectors when no
-c/-p is passed; MEMORY_AGENT names the saving agent; MEMORY_MAX_CHARS bounds the search output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import PurePosixPath

import anyio
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

URL = os.environ.get("MEMORY_URL", "http://127.0.0.1:8765/mcp")
# A local model pays for every result token in prefill (about 21 tokens/s with SSD streaming on a
# 32 GB laptop), and an 8K context cannot hold three whole notes next to the conversation.
MAX_CHARS = int(os.environ.get("MEMORY_MAX_CHARS", "3000"))
ENTRY_SEPARATOR = "\n\n---\n\n"  # how mcp_server._search joins its results


class ToolError(Exception):
    pass


def bounded(text: str, max_chars: int) -> str:
    """Give every result an equal share of max_chars, so one long note cannot crowd out the others."""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    header, _, body = text.partition("\n\n")
    entries = body.split(ENTRY_SEPARATOR)
    share = max(200, (max_chars - len(header)) // len(entries))
    out = []
    for entry in entries:
        if len(entry) > share:
            cut = entry[:share].rstrip()
            entry = f"{cut}\n[{len(entry) - len(cut)} more characters; --max-chars 0 shows everything]"
        out.append(entry)
    return header + "\n\n" + ENTRY_SEPARATOR.join(out)


def describe_project(project: str, resources: list[str]) -> str:
    """A project with the topics of its documents, so an agent can map "what is DUC?" to the work
    context. A document named after its own project adds nothing and is left out."""
    topics = [PurePosixPath(r).stem for r in resources]
    topics = [t for t in topics if t.lower() != project.lower()]
    return f"{project} [{', '.join(topics)}]" if topics else project


def describe_contexts(payload: str) -> str:
    """One line per context. The full catalog (search guidance, document paths) is sized for MCP clients."""
    data = json.loads(payload)
    if not data.get("contexts"):
        return data.get("message", "No memory contexts are configured.")
    lines = []
    for c in data["contexts"]:
        access = "read-only" if c.get("read_only") else "writable"
        select = f"-c {c['context']}" + (" -p <project>" if c.get("project_required") else "")
        resources = c.get("resources", {})
        projects = [describe_project(p, resources.get(p, [])) for p in c.get("projects", [])]
        lines.append(f"{c['context']} ({access}, {select}): {', '.join(projects) or 'no projects yet'}")
    return "\n".join(lines)


def selection(context: str, project: str) -> dict[str, str]:
    """Explicit flags win over the environment defaults; with neither, the server routes by directory."""
    if not (context or project):
        context, project = os.environ.get("MEMORY_CONTEXT", ""), os.environ.get("MEMORY_PROJECT", "")
    if context or project:
        return {"context": context, "project": project}
    return {"workdir": os.getcwd()}


def text_of(result: types.CallToolResult) -> str:
    return "".join(block.text for block in result.content if isinstance(block, types.TextContent))


async def call(tool: str, arguments: dict[str, str]) -> str:
    """Call one tool. When a call routed by directory fails (usually: no route for it), the error
    also lists the contexts, so an agent retries with -c/-p at once instead of spending a turn on
    `memory contexts`; on a slow local model that turn costs about a minute."""
    info = types.Implementation(name=os.environ.get("MEMORY_AGENT", "memory-cli"), version="1")
    listing = None
    async with streamable_http_client(URL) as (read, write, _):
        async with ClientSession(read, write, client_info=info) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            if result.isError and "workdir" in arguments:
                listing = await session.call_tool("list_memory_contexts", {})
    if not result.isError:
        return text_of(result)
    error = text_of(result)
    if listing and not listing.isError:
        try:
            error += "\nRetry with -c <context> -p <project>:\n" + describe_contexts(text_of(listing))
        except (ValueError, KeyError, TypeError):  # a malformed catalog must not hide the original error
            pass
    raise ToolError(error)


def main() -> None:
    parser = argparse.ArgumentParser(prog="memory", description="Search and save the shared project memory.")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="print the 3 entries most similar to a question")
    search.add_argument("query", nargs="+", help="a focused question; quoting is optional")
    search.add_argument("--max-chars", type=int, default=MAX_CHARS, help=f"output bound, 0 = none (default {MAX_CHARS})")
    save = commands.add_parser("save", help="save a note; its content is read from stdin")
    save.add_argument("title")
    for command in (search, save):
        command.add_argument("-c", "--context", default="", help="context from `memory contexts`")
        command.add_argument("-p", "--project", default="", help="project ID in that context")
    commands.add_parser("contexts", help="list contexts and project IDs")
    args = parser.parse_args()

    try:
        if args.command == "search":
            text = anyio.run(call, "search_project_memory",
                             {"query": " ".join(args.query), **selection(args.context, args.project)})
            print(bounded(text, args.max_chars))
        elif args.command == "save":
            content = "" if sys.stdin.isatty() else sys.stdin.read()
            if not content.strip():
                sys.exit("memory: the note content comes from stdin, e.g. memory save \"Title\" <<'EOF' ... EOF")
            print(anyio.run(call, "save_to_project_memory",
                            {"title": args.title, "content": content, **selection(args.context, args.project)}))
        else:
            print(describe_contexts(anyio.run(call, "list_memory_contexts", {})))
    except ToolError as e:
        sys.exit(f"memory: {e}")
    except Exception as e:
        while isinstance(e, BaseExceptionGroup) and len(e.exceptions) == 1:  # anyio wraps transport errors
            e = e.exceptions[0]
        sys.exit(f"memory: cannot reach the memory server at {URL}: {e}")


if __name__ == "__main__":
    main()
