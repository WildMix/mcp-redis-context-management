"""Bundle policy must follow explicit flags rather than corpus names."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from scripts import export_curated, restore_curated
import memory_store as store


class BundleTests(unittest.TestCase):
    def test_restore_permissions_and_context_are_user_selected(self):
        for label, flags, expected_context, writable in (
                ("work", [], "work", False),
                ("personal", [], "personal", False),
                ("research", ["--context", "library", "--writable"], "library", True)):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                snapshot = b"test snapshot"
                (root / "dump.rdb").write_bytes(snapshot)
                (root / "checksums.json").write_text(json.dumps({"dump.rdb": hashlib.sha256(snapshot).hexdigest()}))
                (root / "bundle.json").write_text(json.dumps({
                    "snapshot_sha256": hashlib.sha256(snapshot).hexdigest(), "dbsize": 4,
                    "domain": label, "index": "memory", "model": "test-model", "chunks": 2,
                    "note_chunks": 0, "image": "test-image", "project": "platform", "documents": 1}))
                def docker(command, **kwargs):
                    if "redis-cli" not in command:
                        return ""
                    operation = command[command.index("redis-cli") + 2:]
                    return {("PING",): "PONG", ("DBSIZE",): "4",
                            ("HGET", "memory:meta", "corpus_domain"): label,
                            ("HGET", "memory:meta", "model"): "test-model",
                            ("FT.INFO", "memory"): "num_docs\n2",
                            ("CONFIG", "SET", "appendonly", "yes"): "OK",
                            ("INFO", "persistence"): "aof_enabled:1\naof_rewrite_in_progress:0\naof_rewrite_scheduled:0\naof_last_bgrewrite_status:ok"}[tuple(operation)]
                with patch("sys.argv", ["restore.py", "--bundle", tmp, *flags]), \
                        patch.object(restore_curated.subprocess, "check_output", side_effect=docker), \
                        contextlib.redirect_stdout(io.StringIO()):
                    restore_curated.main()
                config = json.loads((root / "project-memory-copy.routes.json").read_text())
                self.assertEqual(config["default_target"], expected_context)
                self.assertEqual(config["targets"][expected_context]["read_only"], not writable)
                self.assertEqual(config["targets"][expected_context]["domain"], label)

    def test_export_notes_require_opt_in_and_fixed_project_matches(self):
        for label in ("work", "personal", "research"):
            for flags, note_project, expected in (
                    ([], "platform", "include-notes"),
                    (["--include-notes"], "other", "every saved note"),
                    (["--include-notes"], "platform", "snapshot reached")):
                with self.subTest(label=label, flags=flags, project=note_project), tempfile.TemporaryDirectory() as tmp:
                    client = MagicMock()
                    client.__enter__.return_value = client
                    client.info.side_effect = lambda section: {"run_id": "test-run"} if section == "server" else {"db0": {}}
                    client.hget.side_effect = lambda key, field: label if field == "corpus_domain" else note_project
                    client.hgetall.return_value = {"documents": "[]", "keys": '["doc:1"]'}
                    client.hmget.return_value = ["topic", label, "platform", "guide.md", "Role", "Text"]
                    client.scan_iter.side_effect = lambda **kwargs: iter(["note:1"] if kwargs else [store.META_KEY, f"{store.INDEX_NAME}:curated", "doc:1", "note:1"])
                    client.execute_command.return_value = ["test-index"]
                    client.save.side_effect = RuntimeError("snapshot reached")
                    def docker(command, **kwargs):
                        return '["test-image@sha256:digest"]' if command[1:3] == ["image", "inspect"] else "run_id:test-run"
                    with patch("sys.argv", ["export.py", "manifest.json", "--redis-url", "redis://localhost:6380/0",
                                            "--container", "test", "--project", "platform", "--output", str(Path(tmp) / "bundle"), *flags]), \
                            patch.object(export_curated, "prepare", return_value={"domain": label, "documents": [], "records": [{"project": "platform", "topic_id": "topic", "source": "guide.md", "heading": "Role", "content": "Text"}]}), \
                            patch.object(export_curated.store, "load_model"), \
                            patch.object(export_curated.store, "check_model"), \
                            patch.object(export_curated.store, "namespace_of", return_value=SimpleNamespace(notes="note:", index="test-index")), \
                            patch.object(export_curated.redis.Redis, "from_url", return_value=client), \
                            patch.object(export_curated.subprocess, "check_output", side_effect=docker):
                        with self.assertRaisesRegex((ValueError, RuntimeError), expected):
                            export_curated.main()
