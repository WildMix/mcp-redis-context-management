"""Opt in with MEMORY_RUN_INTEGRATION=1; every test uses a disposable Redis index."""
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import redis
import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import memory_store as store
import mcp_server as server
import migrate_memory as migrate
from tests.support import isolated_store, quiet_import, quiet_rebuild


@unittest.skipUnless(os.environ.get("MEMORY_RUN_INTEGRATION") == "1", "set MEMORY_RUN_INTEGRATION=1 for Redis tests")
class RedisTests(unittest.TestCase):
    def setUp(self):
        self.r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True, socket_connect_timeout=3, socket_timeout=10)
        self.r.ping()
        self.addCleanup(self.r.close)
        self.model = self.enterContext(isolated_store(self.r))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="memory-integration-")))
        self.project = store.project_of(str(self.root))

    def save(self, content="A lasting storage decision."):
        return server.save("Decision", content, str(self.root), "integration")

    def documents(self):
        namespace = store.namespace_of(self.r)
        return {key: self.r.hget(key, "content") for key in self.r.scan_iter(match=namespace.docs + "*")}

    def test_parallel_first_saves_both_succeed(self):
        barrier = threading.Barrier(2)
        def save_one(n):
            barrier.wait(timeout=5)
            return self.save("Parallel decision " + str(n))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(save_one, [1, 2]))
        self.assertTrue(all(result.startswith("Saved note") for result in results))
        self.assertEqual(len(list(self.r.scan_iter(match=store.namespace_of(self.r).notes + "*"))), 2)

    def test_long_note_is_reconstructed_exactly(self):
        content = "x=1; " * 170 + "\n\nCritical constraint: expired sessions must be rejected."
        self.save(content)
        result = server.search("expired sessions", str(self.root))
        self.assertIn("title: Decision\n\n" + content, result)
        self.assertIn('Nothing in the memory', server.search("decision", str(self.root / "unrelated")))

    def test_import_update_skip_remove_preserves_notes(self):
        self.save()
        path = self.root / "design.md"
        path.write_text("# Storage\n\nOriginal document.", encoding="utf-8")
        quiet_import(self.r, [self.root], self.project)
        with patch.object(migrate, "load_model", side_effect=AssertionError("unchanged import loaded model")):
            quiet_import(self.r, [self.root], self.project)
        path.write_text("# Storage\n\nReplacement document.", encoding="utf-8")
        quiet_import(self.r, [self.root], self.project)
        self.assertEqual(list(self.documents().values()), ["Replacement document."])
        path.unlink()
        quiet_import(self.r, [self.root], self.project)
        self.assertEqual(self.documents(), {})
        self.assertIn("lasting storage decision", server.search("decision", str(self.root)))

    def test_failed_replacement_preserves_old_chunks_and_fingerprint(self):
        path = self.root / "design.md"
        path.write_text("Old document.", encoding="utf-8")
        quiet_import(self.r, [self.root], self.project)
        old_docs, old_files = self.documents(), self.r.hgetall(store.FILES_KEY)
        path.write_text("Replacement document.", encoding="utf-8")
        execute = redis.client.Pipeline.execute
        def fail_commit(pipe, *args, **kwargs):
            if any(command[0][0] == "HINCRBY" for command in pipe.command_stack):
                raise redis.ConnectionError("simulated loss before EXEC")
            return execute(pipe, *args, **kwargs)
        with patch.object(redis.client.Pipeline, "execute", fail_commit):
            with self.assertRaises(redis.ConnectionError):
                quiet_import(self.r, [self.root], self.project)
        self.assertEqual(self.documents(), old_docs)
        self.assertEqual(self.r.hgetall(store.FILES_KEY), old_files)

    def test_git_scan_failure_preserves_existing_import(self):
        path = self.root / "design.md"
        path.write_text("Original document.", encoding="utf-8")
        quiet_import(self.r, [self.root], self.project)
        before = self.documents()
        with patch.object(migrate, "markdown_files", side_effect=RuntimeError("Git timed out")):
            with self.assertRaises(RuntimeError):
                quiet_import(self.r, [self.root], self.project)
        self.assertEqual(self.documents(), before)

    def test_rebuild_load_and_encoding_failure_preserve_search(self):
        self.save()
        before = self.r.hgetall(store.META_KEY)
        for target in ("load_model", "encode"):
            with self.subTest(target=target), patch.object(migrate, target, side_effect=RuntimeError("injected failure")):
                with self.assertRaisesRegex(RuntimeError, "injected failure"):
                    quiet_rebuild(self.r)
            self.assertEqual(self.r.hgetall(store.META_KEY), before)
            self.assertIn("lasting storage decision", server.search("decision", str(self.root)))
        self.assertFalse(list(self.r.scan_iter(match=store.INDEX_NAME + ":generation:*")))

    def test_rebuild_index_creation_failure_preserves_search(self):
        self.save()
        with patch.object(migrate, "create_index", side_effect=RuntimeError("index failed")):
            with self.assertRaisesRegex(RuntimeError, "index failed"):
                quiet_rebuild(self.r)
        self.assertIn("lasting storage decision", server.search("decision", str(self.root)))

    def test_rebuild_validation_failure_preserves_search(self):
        self.save()
        with patch.object(migrate, "encode", return_value=np.array([[1, 0]], dtype=np.float32)):
            with self.assertRaisesRegex(RuntimeError, "staged index failed validation"):
                quiet_rebuild(self.r)
        self.assertIn("lasting storage decision", server.search("decision", str(self.root)))

    def test_lost_cutover_reply_never_deletes_the_published_generation(self):
        self.save()
        execute = redis.client.Pipeline.execute
        def lose_reply(pipe, *args, **kwargs):
            publishing = any(command[0][0] in ("FT.ALIASADD", "FT.ALIASUPDATE") for command in pipe.command_stack)
            result = execute(pipe, *args, **kwargs)
            if publishing:
                raise redis.ConnectionError("simulated lost reply after cutover")
            return result
        with patch.object(redis.client.Pipeline, "execute", lose_reply):
            with self.assertRaises(redis.ConnectionError):
                quiet_rebuild(self.r)
        active = store.namespace_of(self.r)
        self.assertTrue(active.generation)
        self.assertTrue(store.index_exists(self.r, active.index))
        self.assertIn("lasting storage decision", server.search("decision", str(self.root)))

    def test_search_retries_if_rebuild_removes_old_note_chunks(self):
        self.save()
        original = server._note_text
        switched = False
        def read_after_rebuild(*args):
            nonlocal switched
            if not switched:
                switched = True
                quiet_rebuild(self.r)
            return original(*args)
        with patch.object(server, "_note_text", side_effect=read_after_rebuild):
            self.assertIn("lasting storage decision", server.search("decision", str(self.root)))

    def test_rebuild_detects_concurrent_save(self):
        self.save()
        original_encode = migrate.encode
        written = False
        def concurrent_write(*args):
            nonlocal written
            if not written:
                written = True
                self.save("Concurrent note must survive.")
            return original_encode(*args)
        with patch.object(migrate, "encode", side_effect=concurrent_write):
            with self.assertRaisesRegex(RuntimeError, "Memory changed during rebuild"):
                quiet_rebuild(self.r)
        self.assertIn("Concurrent note must survive.", server.search("decision", str(self.root)))

    def test_successful_rebuild_twice_and_subsequent_writes(self):
        body = ("Dense code x=1; " * 60).strip()
        self.save(body)
        path = self.root / "design.md"
        path.write_text("# Design\n\nStored document.", encoding="utf-8")
        quiet_import(self.r, [self.root], self.project)
        quiet_rebuild(self.r)
        first = store.namespace_of(self.r)
        self.assertTrue(first.generation)
        self.assertIn(body, server.search("decision", str(self.root)))
        self.assertEqual(self.r.ft(store.INDEX_NAME).info()["index_name"], first.index)
        quiet_rebuild(self.r)
        second = store.namespace_of(self.r)
        self.assertNotEqual(first, second)
        self.assertFalse(store.index_exists(self.r, first.index))
        self.assertFalse(list(self.r.scan_iter(match=first.prefix + ":*")))
        self.save("After rebuild.")
        self.assertIn("After rebuild.", server.search("decision", str(self.root)))
        path.write_text("# Design\n\nUpdated after rebuild.", encoding="utf-8")
        quiet_import(self.r, [self.root], self.project)
        self.assertEqual(list(self.documents().values()), ["Updated after rebuild."])

    def test_old_generation_writer_is_rejected(self):
        self.save()
        old = store.namespace_of(self.r)
        quiet_rebuild(self.r)
        with self.assertRaisesRegex(RuntimeError, "rebuilt during"):
            store.mutate(self.r, old, lambda pipe: pipe.hset(old.notes + "stale:0", "content", "wrong"))
        self.assertFalse(self.r.exists(old.notes + "stale:0"))

    def test_rebuild_rechunks_legacy_notes_and_preserves_model_mismatch_guard(self):
        ns = store.ensure_index(self.r, 3)
        content = "x=1; " * 180
        self.r.hset(ns.notes + "legacy:0", mapping={"kind": "note", "note_id": "legacy", "parts": 1,
            "project": self.project, "heading": "Legacy", "content": content, "source": "note legacy",
            "created": 1, "embedding": np.array([1, 0, 0], dtype=np.float32).tobytes()})
        self.r.hset(store.META_KEY, "model", "previous-model")
        with self.assertRaisesRegex(RuntimeError, "built with"):
            self.save()
        quiet_rebuild(self.r)
        self.assertIn(content, server.search("decision", str(self.root)))
        keys = list(self.r.scan_iter(match=store.namespace_of(self.r).notes + "*"))
        self.assertGreater(len(keys), 1)

    def test_search_expands_past_duplicate_candidates(self):
        ns = store.ensure_index(self.r, 3)
        with self.r.pipeline() as pipe:
            for n in range(45):
                pipe.hset(ns.docs + f"copy{n}:0000", mapping={"kind": "doc", "content": "Shared paragraph.",
                    "heading": "Shared", "source": f"copy{n}.md", "start_line": 1, "end_line": 1,
                    "content_sha": "duplicate", "project": self.project,
                    "embedding": np.array([1, 0, 0], dtype=np.float32).tobytes()})
            for n in range(2):
                pipe.hset(ns.docs + f"unique{n}:0000", mapping={"kind": "doc", "content": f"Unique paragraph {n}.",
                    "heading": "Unique", "source": f"unique{n}.md", "start_line": 1, "end_line": 1,
                    "content_sha": f"unique{n}", "project": self.project,
                    "embedding": np.array([0.9, 0.1 + n * 0.1, 0], dtype=np.float32).tobytes()})
            pipe.execute()
        result = server.search("decision", str(self.root))
        self.assertIn("3 closest entries", result)
        self.assertIn("Unique paragraph 0.", result)
        self.assertIn("Unique paragraph 1.", result)


@unittest.skipUnless(os.environ.get("MEMORY_TEST_REAL_MODEL") == "1", "set MEMORY_TEST_REAL_MODEL=1 for cached model test")
class RealModelTests(unittest.TestCase):
    def test_real_tokenizer_save_search_and_rebuild(self):
        model = store.load_model()
        r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True)
        self.addCleanup(r.close)
        with isolated_store(r, model), tempfile.TemporaryDirectory(prefix="memory-real-model-") as tmp:
            content = "x=1; " * 170 + "Critical constraint: expired sessions must be rejected."
            pieces = server.note_chunks(content, model, "Authentication decision")
            for piece in pieces:
                text = store.embedding_text("note", "", "Authentication decision", piece)
                self.assertLessEqual(len(model.tokenizer(text, truncation=False)["input_ids"]), model.max_seq_length)
            server.save("Authentication decision", content, tmp, "integration")
            self.assertIn(content, server.search("expired sessions must be rejected", tmp))
            quiet_rebuild(r)
            self.assertIn(content, server.search("expired sessions must be rejected", tmp))
            # Exercise the actual stdio transport (including the Windows startup workaround),
            # tool registration, async wrappers and metadata after rebuilding.
            async def protocol_check():
                environment = dict(os.environ, MEMORY_INDEX=store.INDEX_NAME, MEMORY_PROJECT="", HF_HUB_OFFLINE="1")
                params = StdioServerParameters(command=sys.executable, args=[str(Path(server.__file__).resolve())],
                                               env=environment, cwd=tmp)
                with anyio.fail_after(120):
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            names = {tool.name for tool in (await session.list_tools()).tools}
                            self.assertEqual(names, {"search_project_memory", "save_to_project_memory"})
                            result = await session.call_tool("save_to_project_memory", {"title": "MCP smoke",
                                "content": "The stdio client can save and retrieve notes.", "workdir": tmp})
                            self.assertFalse(result.isError)
                            found = await session.call_tool("search_project_memory", {"query": "stdio client", "workdir": tmp})
                            self.assertFalse(found.isError)
                            self.assertIn("stdio client can save", "\n".join(part.text for part in found.content if hasattr(part, "text")))
            anyio.run(protocol_check)
