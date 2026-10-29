import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

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
            "reference": {"domain": "reference", "redis_url": "redis://localhost:6380/0", "project": "platform", "read_only": True},
            "projects": {"domain": "projects", "redis_url": "redis://localhost:6381/0", "read_only": False}},
            "routes": [{"root": str(self.root / "reference"), "target": "reference"},
                       {"root": str(self.root / "game"), "target": "projects", "project": "game"}]}

    def router(self):
        self.config.write_text(json.dumps(self.data), encoding="utf-8")
        return Router(self.config)

    def test_explicit_domain_and_project_without_prefix_leak(self):
        router = self.router()
        self.assertEqual(router.resolve(str(self.root / "reference" / "src"))[1], "platform")
        self.assertEqual(router.resolve(str(self.root / "game"))[0].domain, "projects")
        for path in ("reference-other", "unmapped"):
            with self.assertRaisesRegex(ValueError, "No memory route"):
                router.resolve(str(self.root / path))

    def test_new_worktree_uses_common_git_directory(self):
        router = self.router()
        with patch("memory_routing.git", return_value=str(self.root / "game" / ".git")):
            self.assertEqual(router.resolve(str(self.root / "new-worktree"))[1], "game")
            with self.assertRaisesRegex(ValueError, "Conflicting"):
                router.resolve(str(self.root / "reference"))

    def test_read_only_write_rejected_before_connecting(self):
        router = self.router()
        with patch.object(server, "_router", router), patch.object(router, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "read-only"):
                server.save("Title", "Text", str(self.root / "reference"), "test")
            connect.assert_not_called()

    def test_explicit_selection_needs_no_directory_and_keeps_fixed_scope(self):
        router = self.router()
        with patch("memory_routing.git") as git:
            self.assertEqual(router.select("reference")[1], "platform")
            self.assertEqual(router.select("projects", "Game")[1], "game")
            self.assertEqual(router.select("projects", "new-project")[1], "new-project")
            git.assert_not_called()
        for context, project, error in (("reference", "game", "fixed"), ("unknown", "game", "Unknown"),
                                         ("projects", "", "requires"), ("", "game", "requires context"),
                                         ("projects", "*", "1-128")):
            with self.subTest(context=context, project=project), self.assertRaisesRegex(ValueError, error):
                router.select(context, project)

    def test_explicit_read_only_save_rejected_even_from_writable_directory(self):
        router = self.router()
        with patch.object(server, "_router", router), patch.object(router, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "read-only"):
                server.save("Title", "Text", str(self.root / "game"), "test", context="reference")
            connect.assert_not_called()

    def test_catalog_excludes_stale_tags_and_lists_stored_projects_without_routes(self):
        router = self.router()
        with patch.object(router, "connect") as connect, patch("memory_routing.index_exists", return_value=True):
            connect.return_value.hget.return_value = ""
            connect.return_value.ft.return_value.tagvals.return_value = ["archived-project", "game", "retired-project"]
            def metadata(query):
                expression = query.get_args()[0]
                return SimpleNamespace(docs=[], total=int(expression != "@kind:{doc}" and "retired" not in expression))
            connect.return_value.ft.return_value.search.side_effect = metadata
            contexts = {item["context"]: item for item in router.contexts()}
        self.assertEqual(contexts["reference"]["projects"], ["platform"])
        self.assertTrue(contexts["reference"]["read_only"])
        self.assertFalse(contexts["reference"]["project_required"])
        self.assertEqual(contexts["projects"]["projects"], ["archived-project", "game"])
        self.assertTrue(contexts["projects"]["project_required"])

    def test_catalog_maps_resources_to_projects_and_deduplicates_paged_chunks(self):
        router = self.router()
        reference, projects = MagicMock(), MagicMock()
        for client in (reference, projects):
            client.hget.return_value = ""
        reference.ft.return_value.search.side_effect = [
            SimpleNamespace(total=3, docs=[SimpleNamespace(project="platform", source="COMPONENT-A.md")]*2),
            SimpleNamespace(total=3, docs=[SimpleNamespace(project="platform", source="COMPONENT-B.md")])]
        projects.ft.return_value.tagvals.return_value = ["game", "archived"]
        projects.ft.return_value.search.return_value = SimpleNamespace(total=2, docs=[
            SimpleNamespace(project="game", source="gameplay.md"),
            SimpleNamespace(project="archived", source="design.md")])
        with patch.object(router, "connect", side_effect=[reference, projects]), \
                patch("memory_routing.index_exists", return_value=True):
            contexts = {item["context"]: item for item in router.contexts()}
        self.assertEqual(contexts["reference"]["resources"], {"platform": ["COMPONENT-A.md", "COMPONENT-B.md"]})
        self.assertEqual(contexts["projects"]["resources"], {"archived": ["design.md"], "game": ["gameplay.md"]})
        self.assertEqual(reference.ft.return_value.search.call_count, 2)
        for call in reference.ft.return_value.search.call_args_list:
            args = call.args[0].get_args()
            self.assertEqual(args[0], "@kind:{doc} @project:{platform}")
            fields = args.index("RETURN")
            self.assertEqual(args[fields+1:fields+4], [2, "project", "source"])

    def test_explicit_selection_requires_routing_configuration(self):
        with patch.object(server, "_router", None), patch.object(server, "connect") as connect:
            with self.assertRaisesRegex(ValueError, "require MEMORY_ROUTES"):
                server.destination(str(self.root), context="projects", project="game")
            connect.assert_not_called()

    def test_default_only_for_single_fixed_scope(self):
        self.data["default_target"] = "reference"
        with self.assertRaisesRegex(ValueError, "only for one"):
            self.router()
        del self.data["targets"]["projects"]
        self.data["routes"] = []
        self.assertEqual(self.router().resolve(str(self.root))[1], "platform")

    def test_permissions_are_independent_of_context_and_domain_names(self):
        for domain in ("work", "personal", "custom-corpus"):
            with self.subTest(domain=domain):
                self.data["targets"]["reference"].update(domain=domain, read_only=False)
                router = self.router()
                client = MagicMock()
                client.hget.return_value = domain
                with patch.object(server, "_router", router), patch.object(router, "connect", return_value=client):
                    self.assertEqual(server.destination("", writing=True, context="reference"), ("platform", client))
                self.data["targets"]["reference"]["read_only"] = True
                router = self.router()
                with patch.object(server, "_router", router), patch.object(router, "connect") as connect:
                    with self.assertRaisesRegex(ValueError, "read-only"):
                        server.destination("", writing=True, context="reference")
                    connect.assert_not_called()

    def test_optional_corpus_label_and_default_read_only(self):
        self.data["targets"]["archive"] = {"redis_url": "redis://localhost:6382/0"}
        router = self.router()
        self.assertTrue(router.targets["archive"].read_only)
        client = MagicMock()
        client.hget.return_value = None
        with patch("memory_routing.redis.Redis.from_url", return_value=client), patch("memory_routing.check_model"):
            self.assertIs(router.connect(router.targets["archive"]), client)
        client.hget.assert_not_called()

    def test_labeled_target_checks_corpus_identity(self):
        router = self.router()
        client = MagicMock()
        client.hget.return_value = "wrong-corpus"
        with patch("memory_routing.redis.Redis.from_url", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "domain does not match"):
                router.connect(router.targets["reference"])

    def test_invalid_corpus_labels_are_rejected(self):
        for domain in ("../escape", None, 3):
            self.data["targets"]["reference"]["domain"] = domain
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                self.router()

    def test_targets_cannot_share_instance(self):
        self.data["targets"]["projects"]["redis_url"] = "redis://localhost:6380/0"
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

    def write(self, text="# Example\n## Role\nOriginal purpose.\n## Tests\nRun the tests.\n", domain="projects"):
        file = self.root / "guide.md"
        file.write_text(text, encoding="utf-8")
        self.manifest.write_text(json.dumps({"schema": 1, "domain": domain, "corpus_root": ".",
            "documents": [{"document_id": domain + "/example", "project": "example", "file": "guide.md",
                           "content_sha256": hashlib.sha256(file.read_bytes()).hexdigest()}]}), encoding="utf-8")

    def docs(self):
        return {k: self.r.hget(k, "content") for k in self.r.scan_iter(match=store.DOC_PREFIX + "*")}

    def publish(self):
        return import_curated.publish(self.r, self.manifest, self.model)

    def test_replace_remove_move_and_unchanged_preserve_agent_notes(self):
        self.write()
        self.assertEqual(self.publish()["chunks"], 2)
        old_keys = set(self.docs())
        with patch.object(store, "encode", side_effect=AssertionError("Unchanged import re-embedded")):
            self.assertEqual(self.publish()["status"], "unchanged")
        server.save("Decision", "A lasting design decision.", str(self.root), "test")
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
        self.write(domain="reference")
        self.publish()
        server.save("Decision", "Explicit saves are independent of the corpus label.", str(self.root), "test")
        from migrate_memory import import_dirs, rebuild
        with self.assertRaisesRegex(ValueError, "curated"):
            import_dirs(self.r, [self.root], "example")
        with self.assertRaisesRegex(ValueError, "curated"):
            rebuild(self.r)
        self.write(domain="projects")
        with self.assertRaisesRegex(ValueError, "different corpus"):
            self.publish()

    def test_mixed_destination_is_rejected(self):
        server.save("Prior", "Unclassified memory.", str(self.root), "test")
        self.write()
        with self.assertRaisesRegex(ValueError, "unclassified"):
            self.publish()
