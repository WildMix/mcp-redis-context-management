import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hooks import stop_hook as hook


class HookTests(unittest.TestCase):
    def setUp(self):
        self.cwd = str(Path.cwd())
        self.roots = [hook._native(self.cwd, self.cwd)]
        self.edit = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit", "input": {"file_path": "memory_store.py"}}]}}
        self.success = 'Saved note 20261011-abc123 to the memory of project "review".'

    def activity(self, *entries):
        return hook.turn_activity([self.edit, *entries], self.cwd, self.roots)

    def claude_save(self, failed=False, workdir=None):
        call = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "save1", "name": "mcp__project-memory__save_to_project_memory", "input": {"workdir": workdir or self.cwd}}]}}
        result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "save1", "is_error": failed, "content": self.success if not failed else "Redis is not reachable"}]}}
        return call, result

    def codex_save(self, status="completed", failed=False, workdir=None):
        return {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "McpToolCall", "tool": "save_to_project_memory", "status": status,
            "arguments": {"workdir": workdir or self.cwd},
            "result": {"isError": failed, "content": [{"type": "text", "text": self.success}]}}}}

    def test_claude_success_and_failure(self):
        self.assertEqual(self.activity(*self.claude_save()), (True, True))
        self.assertEqual(self.activity(*self.claude_save(failed=True)), (True, False))

    def test_attempt_without_result_is_not_a_save(self):
        call, _ = self.claude_save()
        self.assertEqual(self.activity(call), (True, False))

    def test_codex_success_and_failures(self):
        self.assertEqual(self.activity(self.codex_save()), (True, True))
        self.assertEqual(self.activity(self.codex_save(failed=True)), (True, False))
        self.assertEqual(self.activity(self.codex_save(status="failed")), (True, False))

    def test_tool_discovery_or_code_mention_is_not_a_save(self):
        entry = {"type": "response_item", "payload": {"type": "custom_tool_call", "input": 'text(ALL_TOOLS.filter(t => t.name.includes("save_to_project_memory")))'}}
        self.assertEqual(self.activity(entry), (True, False))

    def test_success_in_another_project_does_not_suppress_reminder(self):
        other = str(Path(self.cwd).parent / "unrelated-repository")
        self.assertEqual(self.activity(*self.claude_save(workdir=other)), (True, False))
        self.assertEqual(self.activity(self.codex_save(workdir=other)), (True, False))

    def test_old_codex_call_result_pair(self):
        call = {"payload": {"type": "function_call", "name": "mcp__project_memory__save_to_project_memory", "call_id": "save1", "arguments": json.dumps({"workdir": self.cwd})}}
        result = {"payload": {"type": "function_call_output", "call_id": "save1", "output": json.dumps({"isError": False, "content": [{"type": "text", "text": self.success}]})}}
        self.assertEqual(self.activity(call, result), (True, True))

    def test_stop_continuation_does_not_loop(self):
        with patch.object(hook, "listed", return_value=True), patch.object(hook, "new_entries", return_value=[self.edit]):
            answer = hook.decide({"cwd": self.cwd, "transcript_path": "ignored", "stop_hook_active": True})
        self.assertIsNone(answer)

    def test_transcript_offset_preserves_unfinished_line(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(hook, "STATE_DIR", Path(tmp) / "state"):
            path = Path(tmp) / "session.jsonl"
            path.write_bytes(b'{"n":1}\n{"n":')
            self.assertEqual(hook.new_entries(path, "test"), [{"n": 1}])
            with path.open("ab") as stream:
                stream.write(b'2}\n')
            self.assertEqual(hook.new_entries(path, "test"), [{"n": 2}])
            self.assertEqual(hook.new_entries(path, "test"), [])
