"""Restore a curated bundle into a new local container/volume; existing data is never overwritten."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--name", default="project-memory-work-copy")
    parser.add_argument("--port", type=int, default=6380)
    parser.add_argument("--wsl-distro")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{2,80}", args.name) or not 1 <= args.port <= 65535:
        parser.error("Use a simple container name and a valid port")
    root = args.bundle.resolve()
    for relative, digest in json.loads((root / "checksums.json").read_text(encoding="utf-8")).items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Bundle checksum mismatch: " + relative)
    meta = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
    if hashlib.sha256((root / "dump.rdb").read_bytes()).hexdigest() != meta["snapshot_sha256"]:
        raise ValueError("Snapshot checksum mismatch")
    docker = ["wsl", "-d", args.wsl_distro, "--", "docker"] if args.wsl_distro else ["docker"]
    def run(*command):
        return subprocess.check_output([*docker, *command], text=True, encoding="utf-8",
            stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()
    volume = args.name + "-data"
    if args.name in run("ps", "-a", "--format", "{{.Names}}").splitlines() or volume in run("volume", "ls", "--format", "{{.Name}}").splitlines():
        raise ValueError("Container or volume already exists; choose a fresh --name. Nothing was changed.")
    def create(aof):
        run("create", "--name", args.name, "--restart", "unless-stopped", "--network", "bridge",
            "-p", f"127.0.0.1:{args.port}:6379", "-v", volume + ":/data", "-e",
            "REDIS_ARGS=--appendonly " + aof + " --save 60 1", meta["image"])
    def cli(*command):
        result = run("exec", args.name, "redis-cli", "--raw", *command)
        if result.startswith(("ERR ", "WRONGTYPE", "NOAUTH")):
            raise RuntimeError(result)
        return result
    def wait_ready():
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if cli("PING") == "PONG":
                    return
            except subprocess.CalledProcessError:
                pass
            time.sleep(0.5)
        raise RuntimeError("Redis startup timed out; inspect docker logs " + args.name)
    def verify():
        if int(cli("DBSIZE")) != meta["dbsize"] or cli("HGET", meta["index"] + ":meta", "corpus_domain") != meta["domain"]:
            raise RuntimeError("Restored database size/domain does not match the bundle")
        if cli("HGET", meta["index"] + ":meta", "model") != meta["model"]:
            raise RuntimeError("Restored model does not match the bundle")
        info = cli("FT.INFO", meta["index"]).splitlines()
        if int(info[info.index("num_docs") + 1]) != meta["chunks"] + meta["note_chunks"]:
            raise RuntimeError("Restored index count does not match the bundle")
    create("no")
    path = root / "dump.rdb"
    source = f"/mnt/{path.drive[0].lower()}{path.as_posix()[2:]}" if args.wsl_distro and path.drive else str(path)
    run("cp", source, args.name + ":/data/dump.rdb")
    run("start", args.name)
    wait_ready()
    verify()
    if cli("CONFIG", "SET", "appendonly", "yes") != "OK":
        raise RuntimeError("Could not enable AOF")
    deadline = time.monotonic() + 60
    while True:
        info = cli("INFO", "persistence")
        required = ("aof_enabled:1", "aof_rewrite_in_progress:0", "aof_rewrite_scheduled:0", "aof_last_bgrewrite_status:ok")
        if all(value in info for value in required):
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("AOF did not finish; the restored volume is retained for diagnosis")
        time.sleep(0.2)
    # Recreate only the container we just created. The named volume is preserved.
    run("stop", args.name)
    run("rm", args.name)
    create("yes")
    run("start", args.name)
    wait_ready()
    verify()
    target = {"redis_url": f"redis://127.0.0.1:{args.port}/0", "domain": meta["domain"], "read_only": meta["domain"] == "work"}
    config = {"schema": 1, "targets": {meta["domain"]: target}, "routes": []}
    if meta.get("project"):
        target["project"] = meta["project"]
        config["default_target"] = meta["domain"]
    routes = root / (args.name + ".routes.json")
    routes.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "restored-and-restarted", "container": args.name, "volume": volume,
                      "redis_port": args.port, "routes": str(routes), "documents": meta["documents"],
                      "chunks": meta["chunks"]}, indent=2))


if __name__ == "__main__":
    main()
