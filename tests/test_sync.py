import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import sync_memory as sync
from process_lock import process_lock

spec = importlib.util.spec_from_file_location("remove_hooks", Path(__file__).resolve().parents[1] / "scripts/remove-memory-hooks.py")
remove_hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(remove_hooks)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(sync, "STATE_DIR", self.root / "state"))
        self.config = self.root / "projects.json"
        self.config.write_text(json.dumps({"projects": [{"project": "example", "directories": [str(self.root)]}],
                                          "interval_seconds": 10, "poll_seconds": 1, "retry_seconds": 3}), encoding="utf-8")
        self.now = 0

    def sleep(self, seconds):
        self.now += seconds
        self.assertLess(self.now, 100, "worker did not exit")

    def run_worker(self, detect, synchronize):
        sync.worker(self.config, detect=detect, sleep=self.sleep, clock=lambda: self.now, synchronize=synchronize)

    def test_initial_periodic_and_final_import_then_exit(self):
        calls = []
        self.run_worker(lambda names: [1] if self.now < 23 else [],
                        lambda config: calls.append(self.now) or True)
        self.assertEqual(calls, [0, 10, 20, 23])
        self.assertFalse(sync.pending_final(sync.state_path(self.config)))

    def test_short_session_still_has_final_import(self):
        calls = []
        self.run_worker(lambda names: [1] if self.now < 2 else [],
                        lambda config: calls.append(self.now) or True)
        self.assertEqual(calls, [0, 2])

    def test_last_client_controls_final_import(self):
        calls = []
        self.run_worker(lambda names: [1, 2] if self.now < 2 else ([2] if self.now < 4 else []),
                        lambda config: calls.append(self.now) or True)
        self.assertEqual(calls, [0, 4])

    def test_failure_retries_without_waiting_for_full_interval(self):
        calls = []
        def synchronize(config):
            calls.append(self.now)
            return len(calls) != 1
        self.run_worker(lambda names: [1] if self.now < 5 else [], synchronize)
        self.assertEqual(calls, [0, 3, 5])

    def test_failed_final_scan_retries_while_no_clients_are_open(self):
        calls = []
        def synchronize(config):
            calls.append(self.now)
            return len(calls) > 1
        self.run_worker(lambda names: [], synchronize)
        self.assertEqual(calls, [0, 3])
        self.assertFalse(sync.pending_final(sync.state_path(self.config)))

    def test_crash_leaves_final_scan_pending_for_restart(self):
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.run_worker(lambda names: [], lambda config: (_ for _ in ()).throw(RuntimeError("interrupted")))
        self.assertTrue(sync.pending_final(sync.state_path(self.config)))
        self.run_worker(lambda names: [], lambda config: True)
        self.assertFalse(sync.pending_final(sync.state_path(self.config)))

    def test_client_opened_during_final_scan_keeps_worker_alive(self):
        calls = []
        def synchronize(config):
            calls.append(self.now)
            if self.now == 2:
                self.now = 3  # A new client opens while the final scan is running.
            return True
        self.run_worker(lambda names: [1] if self.now < 2 or 3 <= self.now < 5 else [], synchronize)
        self.assertEqual(calls, [0, 2, 5])

    def test_one_failed_project_does_not_skip_other_projects(self):
        config = sync.read_config(self.config)
        config["projects"].append({"project": "second", "directories": [str(self.root)]})
        with patch("migrate_memory.main", side_effect=[1, 0]) as migrate:
            self.assertFalse(sync.sync_once(config))
        self.assertEqual(migrate.call_count, 2)
        self.assertIn("--agent-context", migrate.call_args.args[0])
        self.assertNotIn("--include-transcripts", migrate.call_args.args[0])

    def test_invalid_or_duplicate_project_ids_are_rejected(self):
        for names in (["same", "same"], ["../unsafe"]):
            self.config.write_text(json.dumps({"projects": [{"project": name, "directories": [str(self.root)]}
                                                            for name in names]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                sync.read_config(self.config)

    def test_lock_prevents_concurrent_processes_and_releases(self):
        command = [sys.executable, "-c", "from pathlib import Path; from process_lock import process_lock; "
                   "lock=process_lock('test',Path(__import__('sys').argv[1])); lock.__enter__(); lock.__exit__(None,None,None)", str(self.root)]
        with process_lock("test", self.root):
            result = subprocess.run(command, capture_output=True, text=True, stdin=subprocess.DEVNULL)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("holds this lock", result.stderr)
        self.assertEqual(subprocess.run(command, capture_output=True, stdin=subprocess.DEVNULL).returncode, 0)

    @unittest.skipUnless(os.name == "nt", "Windows process enumeration")
    def test_native_process_detection_finds_this_interpreter_only_when_requested(self):
        name = Path(sys.executable).name
        self.assertIn(os.getpid(), sync.active_clients([name]))
        self.assertEqual(sync.active_clients(["project-memory-nonexistent-client.exe"]), [])

    def test_removing_registrations_preserves_unrelated_hooks_and_settings(self):
        script = self.root / "hooks" / "stop_hook.py"
        own = {"type": "command", "command": f'python "{script}"'}
        other = {"type": "command", "command": "another-hook"}
        data = {"permissions": {"allow": ["keep"]}, "hooks": {"Stop": [{"matcher": "", "hooks": [own, other]}],
                "SessionStart": [{"matcher": "^compact$", "hooks": [own]}]}}
        updated, count = remove_hooks.remove_hooks(data, script)
        self.assertEqual(count, 2)
        self.assertEqual(updated["permissions"], data["permissions"])
        self.assertEqual(updated["hooks"], {"Stop": [{"matcher": "", "hooks": [other]}]})
        self.assertIn("SessionStart", data["hooks"])
        self.assertEqual(remove_hooks.remove_hooks(updated, script), (updated, 0))

    def test_retired_hook_is_inert_even_with_old_input(self):
        path = Path(__file__).resolve().parents[1] / "hooks" / "stop_hook.py"
        result = subprocess.run([sys.executable, str(path)], input='{"hook_event_name":"Stop"}',
                                capture_output=True, text=True)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


if __name__ == "__main__":
    unittest.main()
