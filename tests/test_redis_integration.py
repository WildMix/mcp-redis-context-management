"""Opt in with MEMORY_RUN_INTEGRATION=1; every test uses a disposable Redis index."""
import os
import contextlib
import io
import json
import shutil
import subprocess
import time
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
import agent_context as agents
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

    def test_timestamps_track_content_changes_and_survive_rebuild(self):
        path = self.root / "design.md"
        path.write_text("Original context.", encoding="utf-8")
        self.save()
        with patch.object(migrate, "utc_timestamp", return_value="2026-10-11T10:00:00.000Z"):
            quiet_import(self.r, [self.root], self.project)
        key = next(iter(self.documents()))
        self.assertEqual(self.r.hget(key, "updated_at"), "2026-10-11T10:00:00.000Z")
        with patch.object(migrate, "utc_timestamp", return_value="2026-10-11T11:00:00.000Z"):
            quiet_import(self.r, [self.root], self.project)
            self.assertEqual(self.r.hget(key, "updated_at"), "2026-10-11T10:00:00.000Z")
            path.write_text("Revised context.", encoding="utf-8")
            quiet_import(self.r, [self.root], self.project)
        self.assertEqual(self.r.hget(key, "updated_at"), "2026-10-11T11:00:00.000Z")
        quiet_rebuild(self.r)
        key = next(iter(self.documents()))
        self.assertEqual(self.r.hget(key, "updated_at"), "2026-10-11T11:00:00.000Z")
        for key in self.r.scan_iter(match=store.namespace_of(self.r).notes + "*"):
            self.assertTrue(self.r.hget(key, "updated_at").endswith("Z"))

    def test_sync_status_advances_on_unchanged_scan_but_not_failure(self):
        path = self.root / "design.md"
        path.write_text("Context.", encoding="utf-8")
        arguments = [str(self.root), "--project", self.project]
        with patch.object(migrate, "connect", return_value=self.r), contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            with patch.object(migrate, "utc_timestamp", return_value="2026-10-11T10:00:00.000Z"):
                self.assertEqual(migrate.main(arguments), 0)
            key = store.sync_status_key(self.project)
            with patch.object(migrate, "utc_timestamp", return_value="2026-10-11T11:00:00.000Z"), \
                    patch.object(migrate, "load_model", side_effect=AssertionError("unchanged scan loaded model")):
                self.assertEqual(migrate.main(arguments), 0)
            self.assertEqual(self.r.hget(key, "last_success_completed_at"), "2026-10-11T11:00:00.000Z")
            self.assertEqual(self.r.hget(key, "imported"), "0")
            path.write_text("Changed context.", encoding="utf-8")
            with patch.object(migrate, "load_model", side_effect=RuntimeError("failed")):
                self.assertEqual(migrate.main(arguments), 1)
            self.assertEqual(self.r.hget(key, "status"), "error")
            self.assertEqual(self.r.hget(key, "last_success_completed_at"), "2026-10-11T11:00:00.000Z")

    @unittest.skipUnless(os.name == "nt", "Windows native client lifecycle")
    def test_worker_detects_real_client_exit_and_runs_final_import(self):
        # A uniquely named loopback ping process represents a native client without affecting
        # the user's actual Claude/Codex sessions. All Redis keys use the isolated test namespace.
        client_path = self.root / "project-memory-test-client.exe"
        shutil.copyfile(Path(os.environ["WINDIR"]) / "System32" / "ping.exe", client_path)
        config = self.root / "sync.json"
        config.write_text(json.dumps({"projects": [{"project": self.project, "directories": [str(self.root)]}],
            "client_process_names": [client_path.name], "interval_seconds": 900, "poll_seconds": 1}), encoding="utf-8")
        document = self.root / "context.md"
        document.write_text("Initial context.", encoding="utf-8")
        client = subprocess.Popen([str(client_path), "-t", "127.0.0.1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        script = ("import sys; from pathlib import Path; import migrate_memory as m; import sync_memory as s; "
                  "from tests.support import TinyModel; m.load_model=lambda:TinyModel(); "
                  "m.AGENT_CACHE_ROOT=Path(sys.argv[1])/'.memory-imports'; "
                  "sys.exit(s.main(['--worker','--config',sys.argv[2]]))")
        environment = dict(os.environ, MEMORY_INDEX=store.INDEX_NAME, REDIS_URL=store.REDIS_URL,
            LOCALAPPDATA=str(self.root / "local"), CODEX_HOME=str(self.root / "codex"),
            CLAUDE_CONFIG_DIR=str(self.root / "claude"), MEMORY_PROJECT="")
        worker = subprocess.Popen([sys.executable, "-c", script, str(self.root), str(config)], env=environment,
                                  cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 30
            while self.r.hget(store.sync_status_key(self.project), "status") != "ok":
                if worker.poll() is not None:
                    self.fail(worker.stderr.read().decode())
                self.assertLess(time.monotonic(), deadline, "initial sync did not finish")
                time.sleep(0.1)
            self.assertIn("Initial context.", self.documents().values())
            document.write_text("Final context after a short session.", encoding="utf-8")
            client.terminate()
            client.wait(timeout=5)
            try:
                worker.wait(timeout=30)
            except subprocess.TimeoutExpired:
                log = self.root / "local" / "project-memory" / "sync.log"
                self.fail("Worker did not finish its final scan:\n" + log.read_text(encoding="utf-8"))
            self.assertEqual(worker.returncode, 0, worker.stderr.read().decode())
            self.assertIn("Final context after a short session.", self.documents().values())
            self.assertEqual(self.r.hget(store.sync_status_key(self.project), "status"), "ok")
            states = list((self.root / "local" / "project-memory").glob("sync-state-*.json"))
            self.assertEqual(len(states), 1)
            self.assertFalse(json.loads(states[0].read_text())["pending_final"])
        finally:
            for process in (client, worker):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            worker.stderr.close()

    def test_agent_exports_import_incrementally_and_prune_only_selected_categories(self):
        self.save()
        cache = self.root / ".memory-imports"
        memory = agents.Extract(self.root / ".claude" / "MEMORY.md", "memories", "Agent decision.")
        transcript = agents.Extract(self.root / "session.jsonl", "transcripts", "Older conversation.")
        with patch.object(migrate, "AGENT_CACHE_ROOT", cache):
            exports = agents.export([memory, transcript], cache / "project", True)
            migrate.import_dirs(self.r, [self.root], self.project, literal_roots=tuple(exports))
            self.assertTrue(any("Agent decision." in value for value in self.documents().values()))
            self.assertTrue(any("Older conversation." in value for value in self.documents().values()))
            with patch.object(migrate, "load_model", side_effect=AssertionError("unchanged import loaded model")):
                migrate.import_dirs(self.r, [self.root], self.project, literal_roots=tuple(exports))
                # A docs-only import must leave every cached category alone.
                quiet_import(self.r, [self.root], self.project)
            self.assertTrue(any("Older conversation." in value for value in self.documents().values()))
            defaults = agents.export([], cache / "project")
            migrate.import_dirs(self.r, [self.root], self.project, literal_roots=tuple(defaults))
            self.assertFalse(any("Agent decision." in value for value in self.documents().values()))
            self.assertTrue(any("Older conversation." in value for value in self.documents().values()))
            self.assertIn("lasting storage decision", server.search("decision", str(self.root)))

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
