import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import memory_store as store
import migrate_memory as migrate
import mcp_server as server
from tests.support import TinyModel


class ChunkTests(unittest.TestCase):
    def test_skipped_heading_levels_are_siblings(self):
        chunks = store.chunk_markdown("# Root\n### First\nalpha\n### Second\nbeta\n## Last\ngamma")
        self.assertEqual([c.heading for c in chunks], ["Root > First", "Root > Second", "Root > Last"])

    def test_headings_inside_fences_are_content(self):
        chunks = store.chunk_markdown("# Root\n```python\n### not a heading\n\nvalue=1\n```\n")
        self.assertEqual(len(chunks), 1)
        self.assertIn("### not a heading", chunks[0].text)
        self.assertEqual(chunks[0].heading, "Root")

    def test_token_budget_includes_title_and_preserves_all_text(self):
        model = TinyModel()
        content = "x=1; " * 170 + "Critical constraint: expired sessions must be rejected."
        pieces = server.note_chunks(content, model, "Authentication decision")
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), content)
        for piece in pieces:
            self.assertLessEqual(len(model.tokenizer(store.embedding_text("note", "", "Authentication decision", piece))["input_ids"]), model.max_seq_length)

    def test_document_budget_includes_filename_and_heading(self):
        model = TinyModel()
        body = "Authorization/config/v2: " * 60
        original = store.Chunk("Decisions > Authentication", body, 12, 25)
        chunks = store.fit_chunks(model, "doc", "system_design.md", [original])
        self.assertEqual("".join(c.text for c in chunks), body)
        for chunk in chunks:
            self.assertEqual((chunk.start_line, chunk.end_line), (12, 25))
            encoded = store.embedding_text("doc", "system_design.md", chunk.heading, chunk.text)
            self.assertLessEqual(len(model.tokenizer(encoded)["input_ids"]), model.max_seq_length)

    def test_oversized_heading_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "title or section path"):
            server.note_chunks("body", TinyModel(), "x" * 400)

    def test_default_model_prompt_is_included_in_budget(self):
        model = TinyModel()
        model.default_prompt_name = "document"
        model.prompts = {"document": "Document: " * 10}
        content = "A decision. " * 100
        pieces = server.note_chunks(content, model, "Title")
        self.assertEqual("".join(pieces), content)
        for piece in pieces:
            text = model.prompts["document"] + store.embedding_text("note", "", "Title", piece)
            self.assertLessEqual(len(model.tokenizer(text)["input_ids"]), model.max_seq_length)

    def test_unicode_chunks_preserve_text(self):
        body = "日本語 café 🚆\n" * 100
        pieces = server.note_chunks(body, TinyModel(), "Unicode")
        self.assertEqual("".join(pieces), body)


class ScanTests(unittest.TestCase):
    def test_ls_files_timeout_is_not_an_empty_scan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            with patch.object(store.subprocess, "run", side_effect=[MagicMock(stdout="true"), subprocess.TimeoutExpired("git", 10)]):
                with self.assertRaisesRegex(RuntimeError, "Git failed"):
                    migrate.markdown_files(root)

    def test_repository_probe_failure_aborts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").write_text("gitdir: missing", encoding="utf-8")
            with patch.object(store.subprocess, "run", side_effect=subprocess.CalledProcessError(128, "git")):
                with self.assertRaises(RuntimeError):
                    migrate.markdown_files(root)

    def test_non_git_scan_excludes_build_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("read me", encoding="utf-8")
            (root / "build").mkdir()
            (root / "build" / "generated.md").write_text("skip", encoding="utf-8")
            with patch.object(migrate, "git", return_value=""):
                self.assertEqual(migrate.markdown_files(root), [root / "README.md"])

    def test_scan_error_never_prunes_existing_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = MagicMock()
            r.hgetall.return_value = {str(Path(tmp) / "README.md"): "digest"}
            with patch.object(migrate, "check_model"), patch.object(migrate, "markdown_files", side_effect=RuntimeError("Git failed")):
                with self.assertRaises(RuntimeError):
                    migrate.import_dirs(r, [Path(tmp)], "project")
            r.delete.assert_not_called()
            r.pipeline.assert_not_called()

    def test_model_load_failure_does_not_touch_active_index(self):
        r = MagicMock()
        with patch.object(migrate, "load_model", side_effect=RuntimeError("missing model")):
            with self.assertRaises(RuntimeError):
                migrate.rebuild(r)
        self.assertEqual(r.mock_calls, [])


class ProjectTests(unittest.TestCase):
    def test_repository_id_is_shared_by_worktrees(self):
        with patch.object(store, "PROJECT_OVERRIDE", ""), patch.object(store, "git", side_effect=["C:/main/.git", "team-service", "C:/main/.git", "team-service"]):
            self.assertEqual(store.project_of("C:/main"), "team-service")
            self.assertEqual(store.project_of("C:/feature"), "team-service")

    def test_same_folder_names_can_use_different_ids(self):
        with patch.object(store, "PROJECT_OVERRIDE", ""), patch.object(store, "git", side_effect=["C:/a/api/.git", "team-one-api", "C:/b/api/.git", "team-two-api"]):
            self.assertNotEqual(store.project_of("C:/a/api"), store.project_of("C:/b/api"))

    def test_default_names_and_override_remain_compatible(self):
        with patch.object(store, "PROJECT_OVERRIDE", ""), patch.object(store, "git", side_effect=["C:/existing/.git", ""]):
            self.assertEqual(store.project_of("C:/existing"), "existing")
        with patch.object(store, "PROJECT_OVERRIDE", "Pinned"):
            self.assertEqual(store.project_of("C:/existing"), "pinned")

    def test_invalid_repository_id_is_rejected(self):
        with patch.object(store, "PROJECT_OVERRIDE", ""), patch.object(store, "git", side_effect=["C:/main/.git", "one,two"]):
            with self.assertRaises(ValueError):
                store.project_of("C:/main")
