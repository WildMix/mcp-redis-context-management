import os
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import redis

import memory_history as history
import memory_relations as relations
import memory_store as store
import mcp_server as server
from tests.support import isolated_store, quiet_rebuild, quiet_import


class RelationTests(unittest.TestCase):
    def test_suggestions_do_not_supersede_accepted_facts(self):
        self.assertTrue(relations.explicit_update("Requests now time out after 60 seconds."))
        for text in ("Should requests now time out after 60 seconds?", "Perhaps we should change it.",
                     "The new timeout is not yet decided.", "Requests time out after 60 seconds."):
            self.assertFalse(relations.explicit_update(text))

    def test_bidirectional_nli_and_no_truncation(self):
        model = SimpleNamespace(max_seq_length=512, tokenizer=lambda *a, **kw: {"input_ids": [1] * 10},
                                predict=lambda *a, **kw: np.array([[.99, .005, .005], [.99, .005, .005]]))
        with patch.object(relations, "load_model", return_value=model):
            self.assertEqual(relations.compare("The timeout is 30 seconds.", "The timeout is 60 seconds."), "contradicts")
            self.assertEqual(relations.compare("The timeout is 30 seconds.", "The timeout is now 60 seconds."), "updates")
            model.tokenizer = lambda *a, **kw: {"input_ids": [1] * 513}
            self.assertEqual(relations.compare("long old", "long new"), "uncertain")

    def test_scope_and_older_dates_are_not_overridden(self):
        self.assertFalse(relations.same_scope("Production timeout is 30 seconds.", "Staging timeout is now 60 seconds."))
        self.assertFalse(relations.same_scope("CAT accepts updates.", "DUC now rejects updates."))
        self.assertFalse(relations.same_scope("As of 2026-10-29 it is enabled.", "As of 2025-01-01 it is disabled."))


@unittest.skipUnless(os.environ.get("MEMORY_RUN_INTEGRATION") == "1", "requires disposable Redis")
class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True)
        self.addCleanup(self.r.close)
        self.enterContext(isolated_store(self.r))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = store.project_of(str(self.root))

    def save(self, text):
        return server.save("Timeout policy", text, str(self.root), "test")

    def memory_id(self, result):
        return re.search(r"Memory (n-[a-z0-9]+),", result)[1]

    def all_notes(self):
        return history.records(self.r, store.namespace_of(self.r), "@kind:{note}")

    def test_update_retains_old_text_and_search_attaches_current(self):
        memory_id = self.memory_id(self.save("The timeout is 30 seconds."))
        with patch.object(relations, "compare", return_value="updates"):
            self.save("The timeout is now 60 seconds.")
        notes = self.all_notes()
        self.assertEqual({d.memory_id for d in notes}, {memory_id})
        self.assertEqual({d.state for d in notes}, {"current", "historical"})
        # Force a historical vector to match most closely; current must still be included.
        for d in notes:
            vector = [1, 0, 0] if d.state == "historical" else [0, 1, 0]
            self.r.hset(d.id, "embedding", np.array(vector, dtype=np.float32).tobytes())
        result = server.search("old timeout", str(self.root))
        self.assertIn("1 closest entry", result)
        self.assertIn("revision 1 (historical)", result)
        self.assertIn("Current replacement (revision 2", result)
        self.assertIn("now 60 seconds", result)
        versions = server.history(memory_id, str(self.root))
        self.assertIn("30 seconds", versions)
        self.assertIn("60 seconds", versions)

    def test_exact_duplicate_is_idempotent_and_does_not_create_history(self):
        first = self.save("A stable rule.")
        revision = self.r.hget(store.META_KEY, "revision")
        self.assertIn("Already remembered", self.save("A stable rule."))
        self.assertEqual(self.r.hget(store.META_KEY, "revision"), revision)
        self.assertEqual(len(self.all_notes()), 1)

    def test_conflict_keeps_both_current_and_links_them(self):
        first = self.memory_id(self.save("The timeout is 30 seconds."))
        with patch.object(relations, "compare", return_value="contradicts"):
            second = self.memory_id(self.save("The timeout is 60 seconds."))
        notes = self.all_notes()
        self.assertEqual({d.state for d in notes}, {"current"})
        for doc in notes:
            self.assertIn(second if doc.memory_id == first else first, doc.conflicts)

    def test_equivalent_wording_is_grouped_without_discarding_original(self):
        first = self.memory_id(self.save("Entries expire after thirty seconds."))
        with patch.object(relations, "compare", return_value="equivalent"):
            second = self.memory_id(self.save("Entries have a thirty-second lifetime."))
        self.assertEqual(first, second)
        versions = server.history(first, str(self.root))
        self.assertIn("expire after thirty", versions)
        self.assertIn("thirty-second lifetime", versions)

    def test_update_carries_unresolved_conflicts_forward(self):
        first = self.memory_id(self.save("Original timeout."))
        with patch.object(relations, "compare", return_value="contradicts"):
            second = self.memory_id(self.save("Conflicting timeout."))
        with patch.object(relations, "compare", side_effect=lambda old, new: "updates" if old == "Original timeout." else "uncertain"):
            self.save("Timeout is now sixty seconds.")
        head = history.current(self.r, store.namespace_of(self.r), self.project, first)
        self.assertEqual(head.revision, "2")
        self.assertIn(second, head.conflicts)

    def test_versions_and_conflicts_do_not_cross_projects(self):
        memory_id = self.memory_id(self.save("The timeout is 30 seconds."))
        other = str(self.root / "different")
        with patch.object(relations, "compare", return_value="updates"):
            server.save("Timeout policy", "The timeout is now 60 seconds.", other, "test")
        self.assertIn("No matching history", server.history(memory_id, other))
        self.assertNotIn("60 seconds", server.history(memory_id, str(self.root)))

    def test_history_pagination_and_long_notes_survive_rebuild(self):
        first = "First sentence. " * 40
        memory_id = self.memory_id(self.save(first))
        with patch.object(relations, "compare", return_value="extends"):
            self.save(first + "Additional constraint.")
            self.save(first + "Additional constraint. Another constraint.")
        quiet_rebuild(self.r)
        all_versions = server.history(memory_id, str(self.root))
        self.assertIn(first, all_versions)
        self.assertEqual(all_versions.count("Revision "), 3)
        page = server.history(memory_id, str(self.root), before_revision=3, limit=1)
        self.assertIn("Revision 2", page)
        self.assertNotIn("Revision 3", page)

    def test_two_simultaneous_updates_keep_every_revision(self):
        memory_id = self.memory_id(self.save("Original timeout."))
        with patch.object(relations, "compare", return_value="updates"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(self.save, ["Timeout is now 40 seconds.", "Timeout is now 60 seconds."]))
        notes = self.all_notes()
        self.assertEqual({d.revision for d in notes}, {"1", "2", "3"})
        self.assertEqual(sum(d.state == "current" for d in notes), 1)

    def test_failed_transaction_keeps_original_current(self):
        memory_id = self.memory_id(self.save("Original rule."))
        execute = redis.client.Pipeline.execute
        def fail(pipe, *args, **kwargs):
            if any(cmd[0][0] == "HINCRBY" for cmd in pipe.command_stack):
                raise redis.ConnectionError("before EXEC")
            return execute(pipe, *args, **kwargs)
        with patch.object(relations, "compare", return_value="updates"), \
                patch.object(redis.client.Pipeline, "execute", fail):
            with self.assertRaises(redis.ConnectionError):
                self.save("The rule has now changed.")
        notes = self.all_notes()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0].state, "current")

    def test_legacy_backfill_preserves_text_and_is_repeatable(self):
        memory_id = self.memory_id(self.save("Legacy knowledge."))
        for d in self.all_notes():
            self.r.hdel(d.id, "memory_id", "state", "revision", "content_digest")
        self.assertEqual(history.backfill(self.r), 1)
        self.assertEqual(history.backfill(self.r), 0)
        self.assertIn("Legacy knowledge.", server.history(memory_id, str(self.root)))

    def test_upgrade_from_old_index_schema_keeps_vectors(self):
        from redis.commands.search.field import TagField, TextField, VectorField
        from redis.commands.search.index_definition import IndexDefinition, IndexType
        namespace = store.Namespace()
        self.r.ft(namespace.index).create_index(
            [TagField("project"), TagField("kind"), TextField("content"),
             VectorField("embedding", "HNSW", {"TYPE": "FLOAT32", "DIM": 3, "DISTANCE_METRIC": "COSINE"})],
            definition=IndexDefinition(prefix=[namespace.notes, namespace.docs], index_type=IndexType.HASH))
        self.r.hset(store.META_KEY, mapping={"model": store.MODEL_NAME, "dim": 3, "revision": 0})
        vector = np.array([1, 0, 0], dtype=np.float32).tobytes()
        self.r.hset(namespace.notes + "legacy:0", mapping={"kind": "note", "project": self.project,
            "note_id": "legacy", "parts": 1, "heading": "Legacy", "content": "Old knowledge.",
            "embedding": vector})
        self.assertEqual(history.backfill(self.r), 1)
        self.assertEqual(self.r.execute_command("HGET", namespace.notes + "legacy:0", "embedding", NEVER_DECODE=True), vector)
        self.assertIn("Old knowledge.", server.history("n-legacy", str(self.root)))

    def test_document_history_remains_after_removal_and_rebuild(self):
        path = self.root / "design.md"
        path.write_text("Original document.")
        quiet_import(self.r, [self.root], self.project)
        namespace = store.namespace_of(self.r)
        memory_id = history.records(self.r, namespace, "@kind:{doc}")[0].memory_id
        path.write_text("Replacement document.")
        quiet_import(self.r, [self.root], self.project)
        self.assertIn("Original document.", server.history(memory_id, str(self.root)))
        path.unlink()
        quiet_import(self.r, [self.root], self.project)
        quiet_rebuild(self.r)
        versions = server.history(memory_id, str(self.root))
        self.assertIn("Original document.", versions)
        self.assertIn("Replacement document.", versions)
        self.assertIn("retired", versions)

    def test_large_document_versions_are_reconstructed_in_order(self):
        path = self.root / "design.md"
        first = "# Original\n\n" + "First chunk has durable evidence. " * 40
        second = "# Replacement\n\n" + "Second revision has different evidence. " * 40
        path.write_text(first)
        quiet_import(self.r, [self.root], self.project)
        memory_id = history.records(self.r, store.namespace_of(self.r), "@kind:{doc}")[0].memory_id
        path.write_text(second)
        quiet_import(self.r, [self.root], self.project)
        quiet_rebuild(self.r)
        versions = server.history(memory_id, str(self.root))
        self.assertEqual(versions.count("First chunk has durable evidence."), 40)
        self.assertEqual(versions.count("Second revision has different evidence."), 40)


@unittest.skipUnless(os.environ.get("MEMORY_TEST_REAL_MODEL") == "1", "requires cached local models")
class RealHistoryTests(unittest.TestCase):
    def test_local_reconciliation_updates_and_preserves_scope(self):
        compare = relations.compare
        r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True)
        self.addCleanup(r.close)
        with isolated_store(r, store.load_model()), patch.object(relations, "compare", compare), \
                tempfile.TemporaryDirectory() as directory:
            first = server.save("Timeout", "Requests time out after 30 seconds.", directory, "test")
            memory_id = re.search(r"Memory (n-[a-z0-9]+),", first)[1]
            result = server.save("Updated timeout", "Requests now time out after 60 seconds.", directory, "test")
            self.assertIn("revision 2", result)
            self.assertIn("30 seconds", server.history(memory_id, directory))
            self.assertIn("60 seconds", server.history(memory_id, directory))
            self.assertEqual(compare("The production timeout is 30 seconds.",
                                     "The staging timeout is now 60 seconds."), "uncertain")


if __name__ == "__main__":
    unittest.main()
