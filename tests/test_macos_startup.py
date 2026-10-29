"""Exercise dependency failures without Docker, launchd or a loaded embedding model."""
import os
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


class MacStartupConfigurationTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "scripts/install-macos-autostart.py"
        spec = importlib.util.spec_from_file_location("macos_installer", path)
        installer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(installer)
        self.installer = installer

    def test_optional_sources_are_passed_without_credentials(self):
        installer = self.installer
        args = (Path("/repo"), Path("/venv/python"), "/bin/docker", Path("/routes"),
                ["memory-db"], "colima", 8765, Path("/logs"))
        self.assertNotIn("MEMORY_SOURCES", installer.launch_agent(*args)["EnvironmentVariables"])
        env = installer.launch_agent(*args, sources=Path("/private/sources.json"))["EnvironmentVariables"]
        self.assertEqual(env["MEMORY_SOURCES"], "/private/sources.json")
        self.assertNotIn("CONFLUENCE_API_TOKEN", env)

    def test_bootstrap_retries_unloading_job_but_not_other_errors(self):
        failed = subprocess.CompletedProcess([], 5, '', 'still unloading')
        ready = subprocess.CompletedProcess([], 0, '', '')
        with patch.object(self.installer.subprocess, 'run', side_effect=[failed, ready]) as run, \
                patch.object(self.installer.time, 'sleep'):
            self.installer.bootstrap('gui/501', Path('/agent.plist'))
            self.assertEqual(run.call_count, 2)
        for code, attempts in ((5, 30), (78, 1)):
            with patch.object(self.installer.subprocess, 'run', return_value=subprocess.CompletedProcess([], code, '', 'failed')) as run, \
                    patch.object(self.installer.time, 'sleep'):
                with self.assertRaises(subprocess.CalledProcessError):
                    self.installer.bootstrap('gui/501', Path('/agent.plist'))
                self.assertEqual(run.call_count, attempts)


@unittest.skipIf(os.name == "nt", "macOS launcher requires a POSIX shell")
class MacStartupTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.calls = self.root / "calls"
        self.launcher = Path(__file__).resolve().parents[1] / "scripts/start-memory-macos.sh"
        self.docker = self.script("fake docker", '''
printf '%s\\n' "$*" >> "$CALLS"
case "$3" in
  info) [ "$DOCKER_READY" = 1 ] ;;
  start) [ "$START_OK" = 1 ] ;;
  exec) [ "$REDIS_READY" = 1 ] && echo PONG ;;
  *) exit 2 ;;
esac
''')
        self.python = self.script("venv python", 'printf "PYTHON:%s\\n" "$@" >> "$CALLS"\n')
        self.script("sleep", "exit 0\n")
        self.env = {**os.environ, "CALLS": str(self.calls), "PATH": str(self.root) + ":" + os.environ["PATH"],
                    "DOCKER_READY": "1", "START_OK": "1", "REDIS_READY": "1"}

    def script(self, name, content):
        path = self.root / name
        path.write_text("#!/bin/sh\n" + content)
        path.chmod(0o700)
        return path

    def run_launcher(self, containers=("work-db", "personal-db")):
        return subprocess.run(["/bin/bash", str(self.launcher), str(self.docker), "colima",
                               str(self.python), str(self.root / "repo with spaces"), "8765", *containers],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def test_ready_containers_start_one_http_server_with_quoted_paths(self):
        self.assertEqual(self.run_launcher().returncode, 0)
        calls = self.calls.read_text().splitlines()
        self.assertIn("--context colima start work-db personal-db", calls)
        for name in ("work-db", "personal-db"):
            self.assertIn(f"--context colima exec {name} redis-cli ping", calls)
        self.assertIn(f"PYTHON:{self.root}/repo with spaces/mcp_server.py", calls)
        self.assertEqual(calls[-3:], ["PYTHON:streamable-http", "PYTHON:--port", "PYTHON:8765"])

    def test_missing_docker_exits_for_launchd_retry_without_loading_model(self):
        self.env["DOCKER_READY"] = "0"
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Docker unavailable", result.stderr)
        calls = self.calls.read_text()
        self.assertNotIn("start work-db", calls)
        self.assertNotIn("PYTHON:", calls)

    def test_failed_container_start_does_not_launch_server(self):
        self.env["START_OK"] = "0"
        self.assertNotEqual(self.run_launcher().returncode, 0)
        self.assertNotIn("PYTHON:", self.calls.read_text())

    def test_unready_redis_exits_for_retry_without_loading_model(self):
        self.env["REDIS_READY"] = "0"
        result = self.run_launcher()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Redis unavailable", result.stderr)
        self.assertNotIn("PYTHON:", self.calls.read_text())

    def test_no_containers_fails_without_contacting_docker(self):
        self.assertNotEqual(self.run_launcher(containers=()).returncode, 0)
        self.assertFalse(self.calls.exists())
