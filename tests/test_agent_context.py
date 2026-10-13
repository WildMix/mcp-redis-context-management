import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_context as agents
import migrate_memory as migrate


class AgentContextTests(unittest.TestCase):
    def setUp(self):
        # macOS exposes its temporary directory through /var -> /private/var.
        # Production import paths and Git-discovered worktrees are already resolved.
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.root = self.base / "project"
        self.root.mkdir()
        self.claude = self.base / "claude"
        self.codex = self.base / "codex"

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def session(self, path, *rows):
        return self.write(path, "".join(json.dumps(row) + "\n" for row in rows))

    def discover(self, transcripts=False):
        return agents.discover([self.root], self.claude, self.codex, transcripts)

    def test_local_and_global_memories_are_scoped_to_the_project(self):
        local = self.write(self.root / ".codex" / "notes.md", "Local knowledge.")
        self.write(self.root / ".claude" / "settings.json", '{"secret": "do not import"}')
        self.write(self.root / ".codex" / "cache" / "generated.md", "Cache output.")
        slug = re.sub(r"[^a-zA-Z0-9]", "-", str(self.root))
        claude_memory = self.write(self.claude / "projects" / slug / "memory" / "MEMORY.md", "Claude knowledge.")
        summary = self.write(self.codex / "memories" / "rollout_summaries" / "ours.md",
                             f"cwd: {self.root}\n\nA project decision.")
        self.write(summary.with_name("other.md"), f"cwd: {self.root}-other\n\nOther project.")
        registry = self.write(self.codex / "memories" / "MEMORY.md",
                             "# Task Group: Ours\nrollout_summaries/ours.md\nConstraint A.\n"
                             "# Task Group: Mixed\nrollout_summaries/ours.md\nrollout_summaries/other.md\n"
                             "# Task Group: Other\nrollout_summaries/other.md\n")
        raw = self.write(self.codex / "memories" / "raw_memories.md",
                         f"# Raw Memories\n## Thread `ours`\ncwd: {self.root}\nRaw knowledge.\n"
                         f"## Thread `other`\ncwd: {self.root}-other\nUnrelated knowledge.\n")
        self.write(self.codex / "memories" / "memory_summary.md", "Global preferences and unrelated projects.")
        result = {item.source: item.text for item in self.discover()}
        self.assertEqual(set(result), {local, claude_memory, summary, registry, raw})
        self.assertIn("Constraint A.", result[registry])
        self.assertNotIn("Mixed", result[registry])
        self.assertNotIn("Unrelated", result[raw])

    def test_claude_cwd_disambiguates_project_directory_names(self):
        slug = re.sub(r"[^a-zA-Z0-9]", "-", str(self.root))
        directory = self.claude / "projects" / slug
        self.write(directory / "memory" / "MEMORY.md", "Another project's notes.")
        self.session(directory / "session.jsonl", {"type": "user", "cwd": str(self.base / "other"),
                                                  "message": {"role": "user", "content": "Hi"}})
        self.assertEqual(self.discover(), [])

    def test_claude_session_cwd_finds_memory_without_a_matching_slug(self):
        directory = self.claude / "projects" / "nonstandard-folder"
        memory = self.write(directory / "memory" / "topic.md", "Saved context.")
        self.session(directory / "session.jsonl", {"type": "user", "cwd": str(self.root),
                                                  "message": {"role": "user", "content": "Hi"}})
        self.assertEqual([item.source for item in self.discover()], [memory])

    def test_codex_summaries_default_and_optional_conversations(self):
        path = self.session(self.codex / "sessions" / "session.jsonl",
                            {"type": "session_meta", "payload": {"cwd": str(self.root)}},
                            {"type": "compacted", "payload": {"message": "A lasting constraint."}},
                            {"type": "response_item", "payload": {"type": "message", "role": "user",
                                "content": [{"type": "input_text", "text": "User request."}]}},
                            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                "channel": "final", "content": [{"type": "output_text", "text": "Answer."}]}},
                            {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                "channel": "analysis", "content": [{"type": "text", "text": "Private reasoning."}]}},
                            {"type": "event_msg", "payload": {"type": "agent_message", "message": "Answer."}},
                            {"type": "response_item", "payload": {"type": "function_call_output", "output": "Tool output."}},
                            {"type": "response_item", "payload": {"type": "message", "role": "developer",
                                "content": [{"type": "text", "text": "Instructions."}]}},
                            {"type": "response_item", "payload": {"type": "compaction", "encrypted_content": "Encrypted."}})
        default = self.discover()
        self.assertEqual(len(default), 1)
        self.assertEqual(default[0].source, path)
        self.assertEqual(default[0].category, "summaries")
        self.assertIn("source line 2", default[0].text)
        full = self.discover(True)
        self.assertEqual({item.category for item in full}, {"summaries", "transcripts"})
        transcript = next(item.text for item in full if item.category == "transcripts")
        self.assertIn("User request.", transcript)
        self.assertEqual(transcript.count("Answer."), 1)
        for excluded in ("Private reasoning.", "Instructions.", "Tool output.", "Encrypted."):
            self.assertNotIn(excluded, transcript)

    def test_claude_summary_and_text_blocks_skip_thinking_and_tools(self):
        path = self.session(self.claude / "projects" / "custom" / "session.jsonl",
                            {"type": "user", "cwd": str(self.root), "isCompactSummary": True,
                             "message": {"role": "user", "content": "Summary decision."}},
                            {"type": "assistant", "cwd": str(self.root), "message": {"role": "assistant", "content": [
                                {"type": "text", "text": "Visible answer."},
                                {"type": "thinking", "thinking": "Hidden reasoning."},
                                {"type": "tool_use", "input": {"secret": "Tool input."}}]}},
                            {"type": "user", "cwd": str(self.root), "message": {"role": "user", "content": [
                                {"type": "tool_result", "content": "Tool result."}]}})
        self.assertEqual(len(self.discover()), 1)
        result = agents.session_context(path, "Claude", [self.root], True)
        transcript = next(item.text for item in result if item.category == "transcripts")
        self.assertIn("Visible answer.", transcript)
        self.assertNotIn("Tool", transcript)
        self.assertNotIn("Hidden", transcript)

    def test_mixed_or_unassigned_sessions_are_skipped(self):
        path = self.codex / "sessions" / "session.jsonl"
        summary = {"type": "compacted", "payload": {"message": "Summary."}}
        self.session(path, summary)
        self.assertEqual(self.discover(), [])
        self.session(path, {"type": "session_meta", "payload": {"cwd": str(self.root)}}, summary,
                     {"type": "turn_context", "payload": {"cwd": str(self.base / "other")}})
        self.assertEqual(self.discover(True), [])

    def test_invalid_json_aborts_but_incomplete_active_tail_is_ignored(self):
        path = self.session(self.codex / "sessions" / "session.jsonl",
                            {"type": "session_meta", "payload": {"cwd": str(self.root)}},
                            {"type": "compacted", "payload": {"message": "Summary."}})
        valid = path.read_text(encoding="utf-8")
        self.write(path, valid + '{"type":')
        self.assertEqual(len(self.discover()), 1)
        self.write(path, valid + '{broken}\n')
        with self.assertRaisesRegex(RuntimeError, "Invalid session JSON"):
            self.discover()

    def test_worktree_discovery_and_path_boundaries(self):
        linked = self.base / "linked worktree"
        with patch.object(agents, "git", side_effect=["true", f"worktree {self.root}\0HEAD abc\0\0worktree {linked}\0"]):
            self.assertEqual(agents.project_roots([self.root]), sorted([self.root, linked]))
        self.assertTrue(agents.belongs(str(self.root / "src"), [self.root]))
        self.assertFalse(agents.belongs(str(self.root) + "-other", [self.root]))
        if os.name == "nt":
            self.assertTrue(agents.belongs("\\\\?\\" + str(self.root).upper(), [self.root]))

    def test_exports_incremental_and_transcripts_survive_default_refresh(self):
        destination = self.base / "exports"
        memory = agents.Extract(self.root / "MEMORY.md", "memories", "Decision.")
        transcript = agents.Extract(self.root / "session.jsonl", "transcripts", "Conversation.")
        roots = agents.export([memory, transcript], destination, True)
        path = roots[0] / memory.filename
        original_time = path.stat().st_mtime_ns
        self.assertIn(str(memory.source), path.read_text(encoding="utf-8"))
        agents.export([memory], destination)
        self.assertEqual(path.stat().st_mtime_ns, original_time)
        self.assertTrue((roots[2] / transcript.filename).is_file())
        custom = self.write(roots[0] / "handwritten.md", "Keep this.")
        agents.export([], destination)
        self.assertFalse(path.exists())
        self.assertTrue(custom.exists())
        self.assertTrue((roots[2] / transcript.filename).is_file())

    def test_dry_run_never_connects_or_writes_exports(self):
        self.write(self.root / ".claude" / "notes.md", "Decision.")
        argv = ["migrate_memory.py", str(self.root), "--project", "example", "--agent-context", "--dry-run",
                "--claude-home", str(self.claude), "--codex-home", str(self.codex)]
        with patch("sys.argv", argv), patch.object(migrate, "connect") as connect, \
                patch.object(agents, "export") as export, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(migrate.main(), 0)
        connect.assert_not_called()
        export.assert_not_called()
        self.assertIn("notes.md", output.getvalue())

    def test_discovery_error_reports_failure_without_importing(self):
        argv = ["migrate_memory.py", str(self.root), "--project", "example", "--agent-context"]
        with patch("sys.argv", argv), patch.object(agents, "discover", side_effect=OSError("read failed")), \
                patch.object(migrate, "connect") as connect, patch.object(agents, "export") as export, \
                patch.object(migrate, "import_dirs") as importer, \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(migrate.main(), 1)
        importer.assert_not_called()
        export.assert_not_called()
        self.assertEqual(connect.return_value.hset.call_args.kwargs["mapping"]["status"], "error")

    def test_unreadable_source_is_not_treated_as_missing(self):
        with patch.object(Path, "stat", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                agents.files_under(self.codex / "sessions", ".jsonl")


if __name__ == "__main__":
    unittest.main()
