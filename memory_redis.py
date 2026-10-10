"""Settings and the Redis connection shared by migrate_memory.py and mcp_server.py.

When Docker runs inside a WSL distro, WSL stops that distro a few seconds after its last session
ends, and Redis stops with it. With REDIS_WSL_DISTRO set (e.g. "Ubuntu"), every process that uses
Redis holds the distro open for as long as it lives, and waits for Docker to bring the container
back up (restart: unless-stopped in docker-compose.yml).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
INDEX_NAME = os.environ.get("MEMORY_INDEX", "project_memory")
MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
WSL_DISTRO = os.environ.get("REDIS_WSL_DISTRO", "")

CHUNK_PREFIX = f"{INDEX_NAME}:chunk:"  # one hash per chunk: {CHUNK_PREFIX}{file id}:{n}
FILES_KEY = f"{INDEX_NAME}:files"      # source path -> digest of the indexed content
META_KEY = f"{INDEX_NAME}:meta"        # model and dimension the index was built with

HERE = Path(__file__).resolve().parent

_client: redis.Redis | None = None
_keepalive: subprocess.Popen | None = None
_keepalive_lock = threading.Lock()


def hold_wsl_open() -> None:
    """Start REDIS_WSL_DISTRO, if set, and keep it running until this process exits."""
    global _keepalive
    if not WSL_DISTRO:
        return
    with _keepalive_lock:
        if _keepalive is not None and _keepalive.poll() is None:
            return
        # `cat` reads a pipe only this process writes to. However this process ends, the pipe closes,
        # cat exits and WSL is free to stop again. Its output goes nowhere: in the MCP server, stdout
        # is the protocol channel.
        _keepalive = subprocess.Popen(
            ["wsl.exe", "-d", WSL_DISTRO, "--exec", "cat"],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def connect(timeout: float = 60.0) -> redis.Redis:
    """The shared client, once Redis answers; with REDIS_WSL_DISTRO, waits up to `timeout` seconds for it."""
    global _client
    if _client is None:
        _client = redis.Redis.from_url(REDIS_URL, decode_responses=True, socket_connect_timeout=3)
    hold_wsl_open()
    deadline = time.monotonic() + (timeout if WSL_DISTRO else 0)
    while True:
        try:
            _client.ping()
            return _client
        except redis.ConnectionError as e:
            if time.monotonic() >= deadline:
                start = (f"wsl -d {WSL_DISTRO} --cd {_wsl_path(HERE)} -- docker compose up -d" if WSL_DISTRO
                         else f"`docker compose up -d` in {HERE}")
                raise RuntimeError(f"Redis is not reachable at {REDIS_URL} ({e}). Start it with: {start}") from e
            time.sleep(1)


def _wsl_path(path: Path) -> str:
    """C:\\Users\\me\\x -> /mnt/c/Users/me/x"""
    return f"/mnt/{path.drive[0].lower()}{path.as_posix()[2:]}" if path.drive else path.as_posix()
