import os
import json
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import numpy as np
import redis

import memory_history as history
import memory_store as store
import mcp_server as server
from tests.support import isolated_store, quiet_rebuild, quiet_import


class RelationTests(unittest.TestCase):
    def test_relationship_requires_target_and_observed_revision(self):
        history.validate_relationship("", "", 0)
        for relation in ("supersedes", "contradicts"):
            history.validate_relationship(relation, "n-example", 1)
        for args in [("updates", "n-example", 1), ("supersedes", "", 1),
                     ("contradicts", "n-example", 0), ("", "n-example", 1),
                     ("supersedes", "n-example", True), ("supersedes", "n-x} *", 1)]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                history.validate_relationship(*args)


@unittest.skipUnless(os.environ.get("MEMORY_RUN_INTEGRATION") == "1", "requires disposable Redis")
class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True)
        self.addCleanup(self.r.close)
        self.enterContext(isolated_store(self.r))
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.project = store.project_of(str(self.root))

    def save(self, text, target="", revision=1, relationship="supersedes", agent="test"):
        return server.save("Timeout policy", text, str(self.root), agent,
                           relationship=relationship if target else "", related_memory_id=target,
                           expected_revision=revision if target else 0)

    def memory_id(self, result):
        return re.search(r"Memory (n-[a-z0-9]+),", result)[1]

    def all_notes(self):
        return history.records(self.r, store.namespace_of(self.r), "@kind:{note}")

    def test_update_retains_old_text_and_search_attaches_current(self):
        memory_id = self.memory_id(self.save("The timeout is 30 seconds."))
        self.save("The timeout is now 60 seconds.", memory_id)
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
        second = self.memory_id(self.save("The timeout is 60 seconds.", first, relationship="contradicts"))
        notes = self.all_notes()
        self.assertEqual({d.state for d in notes}, {"current"})
        for doc in notes:
            self.assertIn(second if doc.memory_id == first else first, doc.conflicts)

    def test_equivalent_wording_is_grouped_without_discarding_original(self):
        first = self.memory_id(self.save("Entries expire after thirty seconds."))
        second = self.memory_id(self.save("Entries have a thirty-second lifetime.", first))
        self.assertEqual(first, second)
        versions = server.history(first, str(self.root))
        self.assertIn("expire after thirty", versions)
        self.assertIn("thirty-second lifetime", versions)

    def test_update_carries_unresolved_conflicts_forward(self):
        first = self.memory_id(self.save("Original timeout."))
        second = self.memory_id(self.save("Conflicting timeout.", first, relationship="contradicts"))
        self.save("Timeout is now sixty seconds.", first)
        head = history.current(self.r, store.namespace_of(self.r), self.project, first)
        self.assertEqual(head.revision, "2")
        self.assertIn(second, head.conflicts)

    def test_versions_and_conflicts_do_not_cross_projects(self):
        memory_id = self.memory_id(self.save("The timeout is 30 seconds."))
        other = str(self.root / "different")
        for relation in ("supersedes", "contradicts"):
            with self.assertRaisesRegex(ValueError, "No current target"):
                server.save("Timeout policy", "The timeout is now 60 seconds.", other, "test",
                            relationship=relation, related_memory_id=memory_id, expected_revision=1)
        self.assertIn("No matching history", server.history(memory_id, other))
        self.assertNotIn("60 seconds", server.history(memory_id, str(self.root)))

    def test_history_pagination_and_long_notes_survive_rebuild(self):
        first = "First sentence. " * 40
        memory_id = self.memory_id(self.save(first))
        self.save(first + "Additional constraint.", memory_id)
        self.save(first + "Additional constraint. Another constraint.", memory_id, revision=2)
        quiet_rebuild(self.r)
        all_versions = server.history(memory_id, str(self.root))
        self.assertIn(first, all_versions)
        self.assertEqual(all_versions.count("Revision "), 3)
        page = server.history(memory_id, str(self.root), before_revision=3, limit=1)
        self.assertIn("Revision 2", page)
        self.assertNotIn("Revision 3", page)
        head = history.current(self.r, store.namespace_of(self.r), self.project, memory_id)
        self.assertEqual(head.related_revision, "2")
        self.assertEqual(head.relationship, "supersedes")

    def test_two_simultaneous_updates_reject_the_stale_writer(self):
        memory_id = self.memory_id(self.save("Original timeout."))
        barrier = threading.Barrier(2)
        def write(text):
            barrier.wait()
            try:
                return self.save(text, memory_id)
            except ValueError as error:
                return str(error)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(write, ["Timeout is now 40 seconds.", "Timeout is now 60 seconds."]))
        self.assertEqual(sum("Saved note" in s for s in results), 1)
        self.assertEqual(sum("Stale memory" in s for s in results), 1)
        notes = self.all_notes()
        self.assertEqual({d.revision for d in notes}, {"1", "2"})
        self.assertEqual(sum(d.state == "current" for d in notes), 1)

    def test_failed_transaction_keeps_original_current(self):
        memory_id = self.memory_id(self.save("Original rule."))
        execute = redis.client.Pipeline.execute
        def fail(pipe, *args, **kwargs):
            if any(cmd[0][0] == "HINCRBY" for cmd in pipe.command_stack):
                raise redis.ConnectionError("before EXEC")
            return execute(pipe, *args, **kwargs)
        with patch.object(redis.client.Pipeline, "execute", fail):
            with self.assertRaises(redis.ConnectionError):
                self.save("The rule has now changed.", memory_id)
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

    def test_no_relation_never_guesses_from_supersedes_prose(self):
        first = self.memory_id(self.save("Use the Shell compatibility fix."))
        second = self.memory_id(self.save("This supersedes the Shell fix: consumers own their zone."))
        self.assertNotEqual(first, second)
        self.assertEqual({d.state for d in self.all_notes()}, {"current"})
        self.assertTrue(all(d.conflicts == "[]" for d in self.all_notes()))

    def test_other_author_updates_long_note_preserving_every_original_chunk(self):
        old = "A technical constraint. " * 100
        first = self.memory_id(self.save(old, agent="agent-a"))
        self.save(old + "Consumer ownership replaces host ownership.", first, agent="agent-b")
        self.assertEqual({d.agent for d in self.all_notes() if d.revision == "1"}, {"agent-a"})
        self.assertEqual({d.agent for d in self.all_notes() if d.revision == "2"}, {"agent-b"})
        head = history.current(self.r, store.namespace_of(self.r), self.project, first)
        self.assertEqual(history.text(self.r, store.namespace_of(self.r), head),
                         old + "Consumer ownership replaces host ownership.")
        self.assertIn(old, server.history(first, str(self.root)))

    def test_supersedes_retry_is_idempotent_but_cannot_roll_back_a_later_revision(self):
        first = self.memory_id(self.save("Initial decision."))
        self.save("New decision.", first)
        before = self.r.hget(store.META_KEY, "revision")
        self.assertIn("Already remembered", self.save("New decision.", first))
        self.assertEqual(self.r.hget(store.META_KEY, "revision"), before)
        self.save("Latest decision.", first, revision=2)
        with self.assertRaisesRegex(ValueError, "current revision is 3.*|Latest decision"):
            self.save("New decision.", first)
        self.assertEqual(len(self.all_notes()), 3)

    def test_contradiction_retry_links_both_without_duplicates(self):
        first = self.memory_id(self.save("Cache lasts thirty seconds."))
        second = self.memory_id(self.save("Cache lasts sixty seconds.", first, relationship="contradicts"))
        self.assertIn("Already remembered", self.save("Cache lasts sixty seconds.", first, relationship="contradicts"))
        self.assertEqual(len(self.all_notes()), 2)
        self.assertIn(first, server.history(second, str(self.root)))
        self.assertIn(second, server.history(first, str(self.root)))

    def test_stale_conflict_and_identical_self_conflict_fail_without_writes(self):
        first = self.memory_id(self.save("Original."))
        self.save("Current.", first)
        before = self.r.hget(store.META_KEY, "revision")
        with self.assertRaisesRegex(ValueError, "Stale memory"):
            self.save("Conflicting.", first, relationship="contradicts")
        with self.assertRaisesRegex(ValueError, "identical content"):
            self.save("Current.", first, revision=2, relationship="contradicts")
        self.assertEqual(self.r.hget(store.META_KEY, "revision"), before)

    def test_duplicate_does_not_silently_discard_requested_conflict(self):
        first = self.memory_id(self.save("Rule A."))
        self.save("Rule B.")
        with self.assertRaisesRegex(ValueError, "without the requested relationship"):
            self.save("Rule B.", first, relationship="contradicts")
        self.assertEqual(len(self.all_notes()), 2)

    def test_missing_target_rejected_before_duplicate_shortcut(self):
        self.save("Known content.")
        with self.assertRaisesRegex(ValueError, "No current target"):
            self.save("Known content.", "n-missing")

    def test_explicit_relationship_cannot_bypass_read_only_context(self):
        from memory_routing import Router
        first = self.memory_id(self.save("Accepted decision."))
        config = self.root / 'routes.json'
        config.write_text(json.dumps({'schema': 1, 'targets': {
            'reference': {'redis_url': store.REDIS_URL, 'read_only': True}}, 'routes': []}))
        before = self.r.hget(store.META_KEY, 'revision')
        with patch.object(server, '_router', Router(config)):
            for relation in ('supersedes', 'contradicts'):
                with self.assertRaisesRegex(ValueError, 'read-only'):
                    server.save('Decision', 'Changed.', '', 'other-agent', context='reference',
                                project=self.project, relationship=relation,
                                related_memory_id=first, expected_revision=1)
        self.assertEqual(self.r.hget(store.META_KEY, 'revision'), before)

    def test_conflict_failure_commits_neither_side(self):
        first = self.memory_id(self.save('Original.'))
        execute = redis.client.Pipeline.execute
        def fail(pipe, *args, **kwargs):
            if any(cmd[0][0] == 'HINCRBY' for cmd in pipe.command_stack):
                raise redis.ConnectionError('before EXEC')
            return execute(pipe, *args, **kwargs)
        with patch.object(redis.client.Pipeline, 'execute', fail):
            with self.assertRaises(redis.ConnectionError):
                self.save('Conflicting.', first, relationship='contradicts')
        self.assertEqual(len(self.all_notes()), 1)
        self.assertEqual(self.all_notes()[0].conflicts, '[]')

    def test_curated_document_cannot_be_superseded_but_all_chunks_can_be_linked(self):
        path = self.root / "design.md"
        path.write_text("# Shared contract\n\n" + "Reference constraint. " * 90)
        quiet_import(self.r, [self.root], self.project)
        ns = store.namespace_of(self.r)
        docs = history.records(self.r, ns, "@kind:{doc}")
        self.assertGreater(len(docs), 1)
        target = docs[0].memory_id
        with self.assertRaisesRegex(ValueError, "cannot supersede curated"):
            self.save("Replacement.", target)
        new = self.memory_id(self.save("Conflicting observed behavior.", target, relationship="contradicts"))
        self.assertTrue(all(new in d.conflicts for d in history.records(self.r, ns, "@kind:{doc}")))
        self.assertEqual({d.state for d in history.records(self.r, ns, "@kind:{doc}")}, {"current"})

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
    def test_explicit_update_uses_real_embeddings(self):
        r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True)
        self.addCleanup(r.close)
        with isolated_store(r, store.load_model()), \
                tempfile.TemporaryDirectory() as directory:
            first = server.save("Timeout", "Requests time out after 30 seconds.", directory, "test")
            memory_id = re.search(r"Memory (n-[a-z0-9]+),", first)[1]
            result = server.save("Updated timeout", "Requests now time out after 60 seconds.", directory, "another-agent",
                                 relationship="supersedes", related_memory_id=memory_id, expected_revision=1)
            self.assertIn("revision 2", result)
            self.assertIn("30 seconds", server.history(memory_id, directory))
            self.assertIn("60 seconds", server.history(memory_id, directory))
            self.assertIn("author: another-agent", server.history(memory_id, directory))


if __name__ == "__main__":
    unittest.main()
