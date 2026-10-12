import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from curated_corpus import prepare, topic_headings
from tests.support import TinyModel


class CuratedCorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "manifest.json"

    def write(self, text, **changes):
        source = self.root / "guide.md"
        source.write_text(text, encoding="utf-8")
        doc = {"document_id": "work/example/guide", "project": "platform", "file": "guide.md",
               "content_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), **changes}
        self.manifest.write_text(json.dumps({"schema": 1, "domain": "work", "corpus_root": ".",
                                             "documents": [doc]}), encoding="utf-8")

    def test_command_and_prerequisite_are_one_retrievable_unit(self):
        self.write("# Example\n\n## Run locally\n\nNeeds a local database.\n```sh\nrun server\n```\nUse a test identity.\n")
        result = prepare(self.manifest, TinyModel())
        self.assertEqual(len(result["records"]), 1)
        record = result["records"][0]
        self.assertIn("local database", record["content"])
        self.assertIn("run server", record["content"])
        self.assertIn("test identity", record["content"])
        self.assertEqual(record["topic_id"], "work/example/guide#run-locally")
        self.assertEqual(record["source"], "work/guide.md")
        self.assertNotIn(str(self.root), record["embedding_text"])

    def test_unselected_owner_document_is_reported_and_not_imported(self):
        self.write("# Example\n## Role\nA reusable library.\n")
        (self.root / "owner.md").write_text("User addition awaiting review.", encoding="utf-8")
        result = prepare(self.manifest, TinyModel())
        self.assertEqual(result["unselected_files"], ["owner.md"])
        self.assertEqual(len(result["records"]), 1)

    def test_reviewed_hash_prevents_silent_content_changes(self):
        self.write("# Example\n## Role\nA reusable library.\n")
        (self.root / "guide.md").write_text("An unreviewed replacement.", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Unreviewed content"):
            prepare(self.manifest, TinyModel())

    def test_token_split_is_an_error_not_a_silent_edit(self):
        self.write("# Example\n## Long topic\n" + "A meaningful sentence. " * 30)
        with self.assertRaisesRegex(ValueError, "exactly one complete chunk"):
            prepare(self.manifest, TinyModel())

    def test_multiple_paragraph_chunks_require_editorial_split(self):
        model = TinyModel()
        model.max_seq_length = 2000
        self.write("# Example\n## Two topics\n" + "a" * 350 + "\n\n" + "b" * 350)
        with self.assertRaisesRegex(ValueError, "exactly one complete chunk"):
            prepare(self.manifest, model)

    def test_fence_headings_are_code_not_new_topics(self):
        self.write("# Example\n## Run\n```sh\n# a shell comment\n## still code\n```\n")
        result = prepare(self.manifest, TinyModel())
        self.assertEqual(len(result["records"]), 1)

    def test_domain_mismatch_and_path_escape_are_rejected(self):
        for changes in ({"document_id": "personal/example/guide"}, {"file": "../guide.md"}):
            with self.subTest(changes=changes):
                self.write("# Example\n## Role\nA library.\n", **changes)
                with self.assertRaisesRegex(ValueError, "Invalid or duplicate"):
                    prepare(self.manifest, TinyModel())

    def test_invalid_markdown_cannot_create_noise_chunks(self):
        for text in ("# Example\nDomain: work\n## Role\nA library.",
                     "# Example\n## Role\n```sh\nrun",
                     "# Example\n### Nested\nA library.",
                     "# Example\n## Role\nA library.\n## Role\nAnother library.",
                     "# Example\n## \nA library."):
            with self.subTest(text=text), self.assertRaises(ValueError):
                topic_headings(text)


if __name__ == "__main__":
    unittest.main()
