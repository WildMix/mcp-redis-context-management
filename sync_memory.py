"""Hook-free Windows client watcher and incremental project synchronization.

The idle watcher imports only the standard library. A separate worker owns the model,
performs the initial/periodic/final scans and exits when the last client closes.
"""
from __future__ import annotations

import argparse
import contextlib
from functools import cache
import io
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

from process_lock import process_lock

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "sync_projects.json"
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "project-memory"
LOG = logging.getLogger("project-memory-sync")


def read_config(path: Path) -> dict:
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    projects = config.get("projects")
    if not isinstance(projects, list) or not projects:
        raise ValueError("Sync configuration must contain a nonempty projects list.")
    seen = set()
    for project in projects:
        name = project.get("project", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", name) or name in seen:
            raise ValueError("Sync project IDs must be unique, lowercase letters/digits/dots/underscores/hyphens.")
        seen.add(name)
        directories = project.get("directories")
        if not isinstance(directories, list) or not directories or not all(isinstance(d, str) and d for d in directories):
            raise ValueError(f"Project {name} needs a nonempty directories list.")
        project["directories"] = [str(Path(os.path.expandvars(d)).expanduser().resolve()) for d in directories]
    for key, default in (("interval_seconds", 900), ("poll_seconds", 5), ("retry_seconds", 60)):
        value = config.setdefault(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
            raise ValueError(f"{key} must be at least one second.")
    names = config.setdefault("client_process_names", ["codex.exe", "claude.exe"])
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        raise ValueError("client_process_names must be a nonempty list.")
    return config


def active_clients(names: list[str]) -> list[int]:
    """Native clients in this Windows logon session; no hooks, transcript polling or model calls."""
    if os.name != "nt":
        raise RuntimeError("Automatic client monitoring currently supports Windows. Use --once elsewhere.")
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    for function in (kernel.Process32FirstW, kernel.Process32NextW):
        function.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        function.restype = wintypes.BOOL
    kernel.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel.ProcessIdToSessionId.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    own_session = wintypes.DWORD()
    if not kernel.ProcessIdToSessionId(os.getpid(), ctypes.byref(own_session)):
        raise ctypes.WinError(ctypes.get_last_error())
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    result = []
    names = {name.lower() for name in names}
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        found = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        while found:
            if entry.szExeFile.lower() in names:
                session = wintypes.DWORD()
                if kernel.ProcessIdToSessionId(entry.th32ProcessID, ctypes.byref(session)) and session.value == own_session.value:
                    result.append(entry.th32ProcessID)
            found = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if ctypes.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        kernel.CloseHandle(snapshot)
    return sorted(result)


def state_path(config_path: Path) -> Path:
    import hashlib
    identifier = hashlib.sha256(str(config_path.resolve()).encode()).hexdigest()[:16]
    return STATE_DIR / f"sync-state-{identifier}.json"


def pending_final(path: Path) -> bool:
    try:
        return bool(json.loads(path.read_text(encoding="utf-8")).get("pending_final"))
    except FileNotFoundError:
        return False


def write_state(path: Path, pending: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pending_final": pending}), encoding="utf-8")
    temporary.replace(path)


def sync_once(config: dict, model_loader=None) -> bool:
    # Heavy imports are restricted to the active worker (or an explicit --once).
    import migrate_memory as migrate
    success = True
    for project in config["projects"]:
        arguments = ["--project", project["project"], "--agent-context", *project["directories"]]
        for setting, flag in (("claude_home", "--claude-home"), ("codex_home", "--codex-home")):
            if config.get(setting):
                arguments.extend([flag, config[setting]])
        LOG.info("Syncing project %s", project["project"])
        # Continue to other projects when one fails; its previous success timestamp remains.
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                result = migrate.main(arguments, model_loader=model_loader)
        except Exception:
            LOG.exception("Project %s failed", project["project"])
            result = 1
        if output.getvalue().strip():
            LOG.info("%s", output.getvalue().strip())
        success &= result == 0
        LOG.info("Project %s: %s", project["project"], "complete" if result == 0 else "failed")
    return success


def worker(config_path: Path, *, detect=active_clients, sleep=time.sleep, clock=time.monotonic,
           synchronize=None) -> None:
    if synchronize is None:
        import migrate_memory
        loader = cache(migrate_memory.load_model)
        synchronize = lambda config: sync_once(config, loader)
    state = state_path(config_path)
    write_state(state, True)  # Crash/sign-out recovery: retry the final scan on the next startup.
    due = clock()
    while True:
        config = read_config(config_path)
        active = bool(detect(config["client_process_names"]))
        if not active or clock() >= due:
            LOG.info("Starting %s sync", "active" if active else "final")
            success = synchronize(config)
            if not active and success:
                # An app may have opened during this last import.
                if not detect(config["client_process_names"]):
                    write_state(state, False)
                    LOG.info("Final sync complete; worker exiting")
                    return
                active = True
            due = clock() + (config["interval_seconds"] if success else config["retry_seconds"])
            if not active:
                sleep(config["retry_seconds"])
                continue
        sleep(config["poll_seconds"])


def watch(config_path: Path) -> None:
    child = None
    retry_at = 0.0
    while True:
        config = read_config(config_path)
        if child is not None and child.poll() is not None:
            LOG.info("Sync worker exited (%s)", child.returncode)
            retry_at = time.monotonic() + (config["retry_seconds"] if child.returncode else 0)
            child = None
        if child is None and time.monotonic() >= retry_at and (
                active_clients(config["client_process_names"]) or pending_final(state_path(config_path))):
            LOG.info("Starting sync worker")
            child = subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "--worker",
                                      "--config", str(config_path), "--parent-pid", str(os.getpid())], stdin=subprocess.DEVNULL,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        time.sleep(config["poll_seconds"])


def exit_with_parent(pid: int):
    import ctypes
    import threading
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        raise RuntimeError("The sync watcher has already exited.")
    def wait():
        kernel.WaitForSingleObject(handle, 0xFFFFFFFF)
        kernel.CloseHandle(handle)
        os._exit(1)
    threading.Thread(target=wait, name="sync-parent", daemon=True).start()


def configure_logging(watcher=False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(STATE_DIR / ("sync-watch.log" if watcher else "sync.log"), maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)
    # Imports may configure a root console handler. This is a hidden service with its own
    # rotating log; propagating to inherited pipes can block when nobody drains them.
    LOG.propagate = False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--watch", action="store_true", help="wait for clients and manage sync workers")
    mode.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    mode.add_argument("--once", action="store_true", help="sync configured projects now regardless of open clients")
    mode.add_argument("--status", action="store_true", help="show detected clients and per-project Redis sync timestamps")
    args = parser.parse_args(argv)
    args.config = args.config.expanduser().resolve()
    try:
        if not args.status:
            configure_logging(args.watch)
        if args.parent_pid:
            exit_with_parent(args.parent_pid)
        config = read_config(args.config)
        if args.status:
            from memory_store import connect, sync_status_key
            r = connect()
            print(json.dumps({"active_client_pids": active_clients(config["client_process_names"]),
                              "pending_final": pending_final(state_path(args.config)),
                              "projects": {p["project"]: r.hgetall(sync_status_key(p["project"]))
                                           for p in config["projects"]}}, indent=2))
            return 0
        # One supervisor and one active worker for this configuration, even across manual starts.
        mode_name = "watch" if args.watch else "worker"
        with process_lock(f"sync:{args.config}:{mode_name}"):
            if args.watch:
                watch(args.config)
            elif args.worker:
                worker(args.config)
            else:
                import migrate_memory
                return 0 if sync_once(config, cache(migrate_memory.load_model)) else 1
        return 0
    except Exception:
        LOG.exception("Synchronizer stopped")
        return 1


if __name__ == "__main__":
    sys.exit(main())
