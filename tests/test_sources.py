"""Source isolation, enforced tool boundaries, response contracts and real MCP transport."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from mcp import types
from pydantic import ValidationError

import mcp_server as server
import memory_sources as sources
from memory_routing import Router


def config(**changes):
    values = dict(id="wiki", context="work", projects=["platform"],
                  connection={"transport": "stdio", "command": "unused"},
                  allowed_tools=["context_search", "context_fetch"])
    values.update(changes)
    return sources.SourceConfig(**values)


def confluence(**changes):
    return config(adapter="confluence", allowed_tools=list(sources.READ_TOOLS),
                  search_tool="confluence_searchContent", fetch_tool="confluence_getContent", **changes)


def document(**changes):
    return {**dict(id="1", title="Page", url="https://example.org/page", content="Evidence",
                   version="3", updatedAt="2026-10-29T12:00:00Z"), **changes}


def result(value, structured=True, error=False):
    return types.CallToolResult(content=[] if structured else [types.TextContent(type="text", text=json.dumps(value))],
                                structuredContent=value if structured else None, isError=error)


class ContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_denied_before_upstream_call(self):
        session = SimpleNamespace(call_tool=AsyncMock())
        client = sources.ReadClient(session, ["context_search", "context_fetch"])
        for name in ("write_page", "confluence_updateContent", "confluence_createContent", "context_fetch "):
            with self.assertRaisesRegex(sources.SourceFailure, "tool_not_allowed"):
                await client.call(name, {})
        session.call_tool.assert_not_awaited()

    async def test_contract_validates_types_and_identity(self):
        client = SimpleNamespace(call=AsyncMock(return_value=document()))
        adapter = sources.ContractAdapter(config(), client)
        self.assertEqual((await adapter.fetch("1")).version, "3")
        client.call.return_value = document(id="other")
        with self.assertRaisesRegex(sources.SourceFailure, "invalid_response"):
            await adapter.fetch("1")
        for bad in (document(content=12), document(url="file:///etc/passwd"),
                    document(updatedAt="2026-10-29"), document(content="x" * 12001),
                    {**document(), "call_tool": "write_page"}):
            client.call.return_value = bad
            with self.assertRaises(ValidationError):
                await adapter.fetch("1")

    async def test_search_bounds_and_empty_result_are_distinct(self):
        client = SimpleNamespace(call=AsyncMock(return_value={"results": []}))
        adapter = sources.ContractAdapter(config(), client)
        self.assertEqual(await adapter.search("topic", 1), [])
        row = dict(id="1", title="Page", url=None, excerpt="snippet")
        client.call.return_value = {"results": [row, row]}
        with self.assertRaisesRegex(sources.SourceFailure, "invalid_response"):
            await adapter.search("topic", 1)
        with self.assertRaisesRegex(sources.SourceFailure, "invalid_response"):
            await adapter.search("topic", 3)

    def test_decode_structured_and_text_errors_and_large_responses(self):
        for structured in (False, True):
            self.assertEqual(sources.decode_response(result(document(), structured)), document())
            with self.assertRaisesRegex(sources.SourceFailure, "upstream_error"):
                sources.decode_response(result({"secret": "do not return this"}, structured, error=True))
            with self.assertRaisesRegex(sources.SourceFailure, "response_too_large"):
                sources.decode_response(result({"x": "a" * sources.MAX_RESPONSE_CHARS}, structured))
        with self.assertRaisesRegex(sources.SourceFailure, "invalid_response"):
            sources.decode_response(types.CallToolResult(content=[types.TextContent(type="text", text="not JSON")]))

    def test_allowlists_and_confluence_operations_are_explicit(self):
        for kwargs in ({"allowed_tools": ["context_search", "context_fetch", "write"]},
                       {"allowed_tools": ["context_search", "write"]},
                       {"fetch_tool": "context_search", "allowed_tools": ["context_search", "context_search"]}):
            with self.assertRaises(ValidationError):
                config(**kwargs)
        with self.assertRaises(ValidationError):
            config(adapter="confluence")

    async def test_confluence_mapping_read_tools_and_cql_escaping(self):
        payload = {"success": True, "data": {"results": [{"content": {"id": "1", "title": "DUC", "_links": {"webui": "/pages/1"}},
                   "excerpt": "@@@hl@@@DUC@@@endhl@@@ &amp; approval"}], "_links": {"base": "https://example.org"}}}
        client = SimpleNamespace(call=AsyncMock(return_value=payload))
        adapter = sources.ConfluenceAdapter(confluence(spaces=['NOVA" OR type=attachment']), client)
        matches = await adapter.search('How does DUC approval work? " OR type = attachment', 3)
        self.assertEqual(matches[0].url, "https://example.org/pages/1")
        self.assertEqual(matches[0].excerpt, "DUC & approval")
        name, arguments = client.call.call_args.args
        self.assertEqual(name, "confluence_searchContent")
        self.assertIn('space in ("NOVA\\" OR type=attachment")', arguments["cql"])
        self.assertNotIn(' OR type = attachment', arguments["cql"])
        self.assertEqual(arguments["limit"], 3)

    async def test_confluence_page_metadata_and_out_of_scope_reads(self):
        payload = {"success": True, "data": {"id": "1", "status": "current", "title": "DUC", "space": {"key": "NOVA"},
            "body": {"text": {"value": "Evidence", "truncated": True}}, "version": {"number": 9, "when": "2026-10-29T12:00:00Z"},
            "_links": {"self": "https://example.org/wiki/rest/api/content/1", "webui": "/pages/1"}}}
        client = SimpleNamespace(call=AsyncMock(return_value=payload))
        adapter = sources.ConfluenceAdapter(confluence(spaces=["NOVA"]), client)
        page = await adapter.fetch("1")
        self.assertEqual(page.url, "https://example.org/wiki/pages/1")
        self.assertEqual(page.version, "9")
        self.assertTrue(page.truncated)
        self.assertEqual(client.call.call_args.args[0], "confluence_getContent")
        payload["data"]["space"]["key"] = "OTHER"
        with self.assertRaisesRegex(sources.SourceFailure, "outside_source_scope"):
            await adapter.fetch("1")
        with self.assertRaisesRegex(sources.SourceFailure, "invalid_document_id"):
            await adapter.fetch("../admin")

    async def test_confluence_fallback_is_bounded_and_empty_query_makes_no_calls(self):
        client = SimpleNamespace(call=AsyncMock(return_value={"success": True, "data": {"results": []}}))
        adapter = sources.ConfluenceAdapter(confluence(), client)
        self.assertEqual(await adapter.search("How does it work?", 3), [])
        client.call.assert_not_awaited()
        self.assertEqual(await adapter.search("DUC approval permissions", 3), [])
        self.assertEqual(client.call.await_count, 2)
        self.assertIn('text ~ "duc" AND (text ~ "approval" OR text ~ "permissions")', client.call.call_args.args[1]["cql"])


class RegistryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.router = SimpleNamespace(targets={"work": SimpleNamespace(project="platform"),
                                              "personal": SimpleNamespace(project="")})

    def registry(self, *configs):
        path = self.root / "sources.json"
        path.write_text(json.dumps({"schema": 1, "sources": [c.model_dump() for c in configs]}))
        return sources.SourceRegistry(path, self.router)

    def test_context_and_project_isolation_and_no_secrets_in_catalog(self):
        registry = self.registry(config())
        self.assertEqual(len(registry.selected("work", "platform")), 1)
        self.assertEqual(registry.selected("personal", "platform"), [])
        self.assertEqual(registry.selected("work", "other"), [])
        self.assertEqual(registry.catalog("work"), [{"id": "wiki", "projects": ["platform"], "read_only": True}])
        for configs in ((config(), config()), (config(context="unknown"),), (config(projects=["wrong"]),)):
            with self.assertRaises(ValueError):
                self.registry(*configs)

    async def test_forbidden_scope_fails_before_opening_connection(self):
        registry = self.registry(config())
        with patch.object(sources, "connection") as connect:
            with self.assertRaisesRegex(ValueError, "not configured"):
                await registry.fetch("wiki", "1", "personal", "platform")
            connect.assert_not_called()

    async def test_upstream_failure_keeps_search_excerpt_and_hides_secrets(self):
        registry = self.registry(config())
        adapter = SimpleNamespace(search=AsyncMock(return_value=[sources.SearchResult(id="1", title="Page", url=None, excerpt="Useful")]),
                                  fetch=AsyncMock(side_effect=RuntimeError("Bearer private-token")))
        @asynccontextmanager
        async def connect(*_):
            yield None
        with patch.object(sources, "connection", connect), patch.object(sources, "adapter_for", return_value=adapter):
            output = await registry.gather_one(config(), "topic")
            self.assertEqual(output["results"][0]["error"], "unavailable")
            formatted = sources.format_results([output])
            self.assertIn("Useful", formatted)
            self.assertNotIn("private-token", formatted)
            self.assertEqual((await registry.fetch("wiki", "1", "work", "platform"))["error"], "unavailable")

    async def test_timeout_is_explicit_and_cancels_work(self):
        registry = self.registry(config())
        cancelled = []
        @asynccontextmanager
        async def connect(*_):
            try:
                await asyncio.sleep(30)
                yield None
            finally:
                cancelled.append(True)
        with patch.object(sources, "connection", connect):
            started = time.monotonic()
            output = await registry.gather_one(config(timeout_seconds=1), "topic")
        self.assertEqual(output["errors"], ["timeout"])
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(cancelled)

    async def test_malformed_page_is_reported_and_preview_marks_truncation(self):
        registry = self.registry(config())
        adapter = SimpleNamespace(search=AsyncMock(return_value=[sources.SearchResult(id="1", title="Page", url=None, excerpt="Useful")]),
                                  fetch=AsyncMock(return_value=sources.SourceDocument(**document(content="x" * 5000))))
        @asynccontextmanager
        async def connect(*_):
            yield None
        with patch.object(sources, "connection", connect), patch.object(sources, "adapter_for", return_value=adapter):
            output = await registry.gather_one(config(), "topic")
        formatted = sources.format_results([output])
        self.assertIn('"truncated": true', formatted)
        self.assertNotIn("x" * 4001, formatted)

    async def test_stdio_contract_uses_only_read_operations_and_closes_process(self):
        calls = self.root / "calls.jsonl"
        fixture = Path(__file__).with_name("source_fixture.py")
        spec = config(connection={"transport": "stdio", "command": sys.executable,
                                  "args": [str(fixture)], "env": {"SOURCE_TEST_CALLS": str(calls)}})
        registry = self.registry(spec)
        output = await registry.gather_one(spec, "architecture")
        self.assertEqual(output["errors"], [], output)
        self.assertEqual(output["results"][0]["document"]["version"], "7")
        recorded = [json.loads(line) for line in calls.read_text().splitlines()]
        self.assertEqual([r["tool"] for r in recorded], ["context_search", "context_fetch"])
        if os.name == "posix":
            with self.assertRaises(ProcessLookupError):
                os.kill(recorded[0]["pid"], 0)

    @unittest.skipUnless(os.environ.get("MEMORY_RUN_INTEGRATION") == "1", "enable local HTTP integration")
    async def test_http_contract(self):
        calls = self.root / "calls.jsonl"
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        with (self.root / "server.log").open("w") as log:
            process = subprocess.Popen([sys.executable, str(Path(__file__).with_name("source_fixture.py")), str(port)],
                env={**os.environ, "SOURCE_TEST_CALLS": str(calls)}, stdout=log, stderr=log)
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        self.fail("Fixture exited")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.1):
                            break
                    except OSError:
                        await asyncio.sleep(.05)
                spec = config(connection={"transport": "streamable-http", "url": f"http://127.0.0.1:{port}/mcp"})
                registry = self.registry(spec)
                output = await registry.gather_one(spec, "architecture")
                self.assertEqual(output["errors"], [], output)
                self.assertEqual(output["results"][0]["document"]["id"], "page-1")
            finally:
                process.terminate()
                process.wait(timeout=10)


class ServerSourceTests(unittest.TestCase):
    def setUp(self):
        self.registry = SimpleNamespace(selected=MagicMock(return_value=[config()]),
                                        gather=AsyncMock(return_value=[{"source": "wiki", "retrievedAt": "now", "results": [], "errors": []}]))
        self.enterContext(patch.object(server, "_sources", self.registry))
        self.enterContext(patch.object(server, "source_scope", return_value=("work", "platform")))

    def test_federated_search_keeps_local_results_when_upstream_fails(self):
        self.registry.gather.return_value[0]["errors"] = ["timeout"]
        with patch.object(server, "_local_search", return_value="Saved knowledge"):
            text = server.search("topic", "", "work")
        self.assertIn("Saved knowledge", text)
        self.assertIn('"timeout"', text)

    def test_unconfigured_scope_never_contacts_source(self):
        self.registry.selected.return_value = []
        with patch.object(server, "_local_search", return_value="Personal memory"):
            self.assertEqual(server.search("topic", "", "personal", "game"), "Personal memory")
        self.registry.gather.assert_not_awaited()

    def test_invalid_selection_fails_before_outbound_calls(self):
        with patch.object(server, "source_scope", side_effect=ValueError("Unknown context")):
            with self.assertRaisesRegex(ValueError, "Unknown context"):
                server.search("topic", "", "unknown")
        self.registry.gather.assert_not_awaited()

    def test_redis_failure_does_not_hide_external_results(self):
        with patch.object(server, "_local_search", side_effect=RuntimeError("private connection details")):
            text = server.search("topic", "", "work")
        self.assertIn("Local Redis memory is unavailable", text)
        self.assertIn('"source": "wiki"', text)
        self.assertNotIn("private connection", text)
