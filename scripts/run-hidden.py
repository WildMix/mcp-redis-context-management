"""Run a task through pythonw, without ever allocating a console for its worker.

The launcher stays alive so Task Scheduler tracks the worker. A Windows job owns the
whole process tree: stopping/killing the launcher also stops its children.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import traceback


def own_process_tree() -> None:
    class BasicLimits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                    ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                    ("scheduling_class", wintypes.DWORD)]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("basic", BasicLimits), ("io_counters", ctypes.c_uint64 * 6),
                    ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetInformationJobObject.restype = wintypes.BOOL
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)  # Unnamed, non-inheritable handle.
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        raise error
    # Join before spawning: even an immediate exit cannot leave an unowned child.
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        error = ctypes.WinError(ctypes.get_last_error())
        kernel.CloseHandle(job)
        raise error
    # Deliberately keep this handle until process exit. Closing it here would kill us too.
    # Windows closes it even on forced termination, and then terminates all descendants.


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    args.log.parent.mkdir(parents=True, exist_ok=True)
    if args.log.exists() and args.log.stat().st_size > 1_000_000:
        args.log.replace(args.log.with_suffix(args.log.suffix + ".previous"))
    with args.log.open("a", encoding="utf-8") as log:
        try:
            if os.name != "nt" or not args.command:
                raise ValueError("This Windows launcher requires a worker command.")
            own_process_tree()
            print(f"{datetime.now().astimezone().isoformat()} Starting background worker (launcher PID {os.getpid()})", file=log, flush=True)
            result = subprocess.run(args.command, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
            print(f"{datetime.now().astimezone().isoformat()} Worker exited ({result.returncode})", file=log, flush=True)
            return result.returncode
        except Exception:
            traceback.print_exc(file=log)
            return 1


if __name__ == "__main__":
    sys.exit(main())
