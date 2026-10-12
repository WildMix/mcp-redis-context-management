import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import redis
import import_curated
import memory_store as store
import mcp_server as server
from memory_routing import Router
from tests.support import isolated_store


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.config = self.root / "routes.json"
        self.enterContext(patch("memory_routing.git", return_value=""))
        self.data = {"schema": 1, "targets": {
            "work": {"domain": "work", "redis_url": "redis://localhost:6380/0", "project": "platform", "read_only": True},
            "personal": {"domain": "personal", "redis_url": "redis://localhost:6381/0", "read_only": False}},
            "routes": [{"root": str(self.root / "work"), "target": "work"},
                       {"root": str(self.root / "game"), "target": "personal", "project": "game"}]}

    def router(self):
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        return Router(self.config)

    def test_explicit_domain_and_project_without_prefix_leak(self):
        router = self.router()
        self.assertEqual(router.resolve(str(self.root / "work" / "src"))[1], "platform")
        self.assertEqual(router.resolve(str(self.root / "game"))[0].domain, "personal")
        for path in ("work-other", "unmapped"):
            with self.assertRaisesRegex(ValueError, "No memory route"):
                router.resolve(str(self.root / path))

    def test_new_worktree_uses_common_git_directory(self):
        router = self.router()
        with patch("memory_routing.git", return_value=str(self.root / "game" / ".git")):
            self.assertEqual(router.resolve(str(self.root / "new-worktree"))[1], "game")
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                router.resolve(str(self.root / "work"))

    def test_work_write_rejected_before_connecting(self):
        router = self.router()
        with patch.object(server, "_router", router), patch.object(router, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "read-only"):
                server.save("Title", "Text", str(self.root / "work"), "test")
            connect.assert_not_called()

    def test_default_only_for_single_fixed_scope(self):
        self.data["default_target"] = "work"
        with self.assertRaisesRegex(ValueError, "only for one"):
            self.router()
        del self.data["targets"]["personal"]
        self.data["routes"] = []
        self.assertEqual(self.router().resolve(str(self.root))[1], "platform")

    def test_work_cannot_be_writable_or_share_instance(self):
        self.data["targets"]["work"]["read_only"] = False
        with self.assertRaisesRegex(ValueError, "read_only"):
            self.router()
        self.data["targets"]["work"]["read_only"] = True
        self.data["targets"]["personal"]["redis_url"] = "redis://localhost:6380/0"
        with self.assertRaisesRegex(ValueError, "separate"):
            self.router()


@unittest.skipUnless(os.environ.get("MEMORY_RUN_INTEGRATION") == "1", "requires disposable Redis test namespace")
class CuratedImportTests(unittest.TestCase):
    def setUp(self):
        self.r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True)
        self.addCleanup(self.r.close)
        self.model = self.enterContext(isolated_store(self.r))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.manifest = self.root / "manifest.json"

    def write(self, text="# Example\n## Role\nOriginal purpose.\n## Tests\nRun the tests.\n", domain="personal"):
        file = self.root / "guide.md"
        file.write_text(text, encoding="utf-8")
        self.manifest.write_text(json.dumps({"schema": 1, "domain": domain, "corpus_root": ".",
            "documents": [{"document_id": domain + "/example", "project": "example", "file": "guide.md",
                           "content_sha256": hashlib.sha256(file.read_bytes()).hexdigest()}]}), encoding="utf-8")

    def docs(self):
        return {k: self.r.hget(k, "content") for k in self.r.scan_iter(match=store.DOC_PREFIX + "*")}

    def publish(self):
        return import_curated.publish(self.r, self.manifest, self.model)

    def test_replace_remove_move_and_unchanged_preserve_personal_notes(self):
        self.write()
        self.assertEqual(self.publish()["chunks"], 2)
        old_keys = set(self.docs())
        with patch.object(store, "encode", side_effect=AssertionError("Unchanged import re-embedded")):
            self.assertEqual(self.publish()["status"], "unchanged")
        server.save("Decision", "A lasting personal decision.", str(self.root), "test")
        self.write("# Example\n## Role\nReplacement purpose.\n")
        self.publish()
        self.assertEqual(list(self.docs().values()), ["Replacement purpose."])
        self.assertTrue(set(self.docs()) < old_keys)
        self.assertEqual(len(list(self.r.scan_iter(match=store.NOTE_PREFIX + "*"))), 1)
        moved = self.root / "moved"
        moved.mkdir()
        for name in ("guide.md", "manifest.json"):
            (moved / name).write_bytes((self.root / name).read_bytes())
        self.manifest = moved / "manifest.json"
        self.assertEqual(self.publish()["status"], "unchanged")

    def test_failed_publication_preserves_old_records(self):
        self.write()
        self.publish()
        old = self.docs()
        self.write("# Example\n## Role\nReplacement purpose.\n")
        execute = redis.client.Pipeline.execute
        def fail(pipe, *args, **kwargs):
            if any(command[0][0] == "HINCRBY" for command in pipe.command_stack):
                raise redis.ConnectionError("simulated before EXEC")
            return execute(pipe, *args, **kwargs)
        with patch.object(redis.client.Pipeline, "execute", fail):
            with self.assertRaises(redis.ConnectionError):
                self.publish()
        self.assertEqual(self.docs(), old)

    def test_domain_guard_and_legacy_writers(self):
        self.write(domain="work")
        self.publish()
        with self.assertRaisesRegex(ValueError, "read-only"):
            server.save("Decision", "Do not write work notes.", str(self.root), "test")
        from migrate_memory import import_dirs, rebuild
        with self.assertRaisesRegex(ValueError, "curated"):
            import_dirs(self.r, [self.root], "example")
        with self.assertRaisesRegex(ValueError, "curated"):
            rebuild(self.r)
        self.write(domain="personal")
        with self.assertRaisesRegex(ValueError, "other corpus"):
            self.publish()

    def test_mixed_destination_is_rejected(self):
        server.save("Prior", "Unclassified memory.", str(self.root), "test")
        self.write()
        with self.assertRaisesRegex(ValueError, "unclassified"):
            self.publish()
