"""Windows process tests: no console, diagnostics, and cleanup on forced task stop."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == "nt", "Windows startup tasks")
class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="background test ")))
        self.launcher = Path(__file__).resolve().parents[1] / "scripts/run-hidden.py"
        self.pythonw = Path(sys.executable).with_name("pythonw.exe")
        self.powershell = str(Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        self.log = self.root / "launcher.log"

    def launch(self, script):
        process = subprocess.Popen([str(self.pythonw), str(self.launcher), "--log", str(self.log),
                                    self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                    "-File", str(script)], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(self.stop_tree, process)
        return process

    @staticmethod
    def stop_tree(process):
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        process.wait(timeout=10)

    def test_powershell_and_server_have_no_console_and_keep_logs_and_exit_code(self):
        probe = self.root / "probe.py"
        probe.write_text("import ctypes, sys\n"
                         "print('PY_CONSOLE=' + str(ctypes.windll.kernel32.GetConsoleWindow()), flush=True)\n"
                         "print('diagnostic on stderr', file=sys.stderr, flush=True)\n"
                         "sys.exit(7)\n", encoding="utf-8")
        script = self.root / "worker.ps1"
        # Exercise the same pythonw + Start-Process redirection used by the MCP keeper.
        script.write_text("$ErrorActionPreference = 'Stop'\n"
                          "Add-Type 'using System; using System.Runtime.InteropServices; public class ConsoleProbe {"
                          '[DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow(); }' + "'\n"
                          "Write-Output ('PS_CONSOLE=' + [ConsoleProbe]::GetConsoleWindow())\n"
                          f"$p = Start-Process -FilePath '{self.pythonw}' -ArgumentList '\"{probe}\"' "
                          f"-WindowStyle Hidden -PassThru -RedirectStandardOutput '{self.root / 'stdout.log'}' "
                          f"-RedirectStandardError '{self.root / 'stderr.log'}'\n"
                          "$null = $p.Handle\n$p.WaitForExit()\nexit $p.ExitCode\n", encoding="utf-8")
        process = self.launch(script)
        self.assertEqual(process.wait(timeout=30), 7, self.log.read_text(encoding="utf-8"))
        self.assertIn("PS_CONSOLE=0", self.log.read_text(encoding="utf-8"))
        self.assertIn("PY_CONSOLE=0", (self.root / "stdout.log").read_text(encoding="utf-8"))
        self.assertIn("diagnostic on stderr", (self.root / "stderr.log").read_text(encoding="utf-8"))

    def test_killing_launcher_stops_worker_and_grandchild(self):
        ready = self.root / "ready.json"
        child = self.root / "child.py"
        child.write_text("import json, os, sys, time\nfrom pathlib import Path\n"
                         f"Path({str(ready)!r}).write_text(json.dumps([os.getpid(), int(sys.argv[1])]))\n"
                         "time.sleep(90)\n", encoding="utf-8")
        script = self.root / "worker.ps1"
        script.write_text(f'& "{sys.executable}" "{child}" $PID\n', encoding="utf-8")
        process = self.launch(script)
        deadline = time.monotonic() + 30
        while not ready.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.05)
        self.assertTrue(ready.exists(), self.log.read_text(encoding="utf-8"))
        launcher_pid = int(re.search(r"launcher PID (\d+)", self.log.read_text(encoding="utf-8"))[1])
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handles = [kernel.OpenProcess(0x100000 | 1, False, pid)
                   for pid in [launcher_pid, *json.loads(ready.read_text())]]
        try:
            self.assertTrue(all(handles))
            self.assertTrue(kernel.TerminateProcess(handles[0], 1))
            for handle in handles:
                self.assertEqual(kernel.WaitForSingleObject(handle, 10_000), 0,
                                 "A worker survived its background launcher")
            process.wait(timeout=10)
        finally:
            for handle in handles:
                if handle:
                    kernel.CloseHandle(handle)


if __name__ == "__main__":
    unittest.main()
