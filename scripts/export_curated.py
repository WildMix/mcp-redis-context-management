"""Build a domain-only RDB + Markdown + local MCP runtime ZIP from a reviewed corpus."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import redis
from curated_corpus import prepare
import memory_store as store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", type=Path, required=True, help="New bundle directory; also writes its .zip sibling")
    parser.add_argument("--project", help="Optional fixed project; must match all documents and notes")
    parser.add_argument("--include-notes", action="store_true", help="Explicitly include saved agent notes in the snapshot")
    parser.add_argument("--wsl-distro")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() or output.with_suffix(".zip").exists():
        parser.error("Bundle output already exists; choose a new versioned name")
    docker = ["wsl", "-d", args.wsl_distro, "--", "docker"] if args.wsl_distro else ["docker"]
    def run(*command):
        return subprocess.check_output([*docker, *command], text=True, encoding="utf-8",
            stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()
    report = prepare(args.manifest, store.load_model())
    domain = report["domain"]
    if args.project and {x["project"] for x in report["records"]} != {args.project}:
        parser.error("--project must match every selected document")
    with redis.Redis.from_url(args.redis_url, decode_responses=True) as r:
        store.check_model(r)
        if r.hget(store.META_KEY, "corpus_domain") != domain:
            raise ValueError("Destination domain does not match the manifest")
        # Prove the network client and Docker snapshot refer to the same Redis process.
        container_info = run("exec", args.container, "redis-cli", "--raw", "INFO", "server")
        if "run_id:" + r.info("server")["run_id"] not in container_info:
            raise ValueError("Container and Redis URL refer to different servers")
        if set(r.info("keyspace")) != {"db0"}:
            raise ValueError("Snapshot would include unexpected logical databases")
        namespace = store.namespace_of(r)
        registry = r.hgetall(f"{store.INDEX_NAME}:curated")
        if json.loads(registry["documents"]) != report["documents"]:
            raise ValueError("Selected documents do not match the published registry")
        expected = {x["topic_id"]: x for x in report["records"]}
        keys = json.loads(registry["keys"])
        seen = set()
        for key in keys:
            fields = r.hmget(key, ["topic_id", "domain", "project", "source", "heading", "content"])
            topic, stored_domain, project, source, heading, content = fields
            item = expected.get(topic)
            if not item or stored_domain != domain or [project, source, heading, content] != [item[k] for k in ("project", "source", "heading", "content")]:
                raise ValueError("Stored topic differs from reviewed content")
            seen.add(topic)
        if seen != set(expected) or len(keys) != len(expected):
            raise ValueError("Stored topic coverage is incomplete")
        notes = list(r.scan_iter(match=namespace.notes + "*"))
        history = list(r.scan_iter(match=namespace.docs + "history:*"))
        if notes and not args.include_notes:
            raise ValueError("Snapshot contains agent notes; use --include-notes to include them explicitly")
        if args.project and any(r.hget(key, "project") != args.project for key in notes):
            raise ValueError("--project must match every saved note")
        if history and not args.include_notes:
            raise ValueError("Snapshot contains historical documents; use --include-notes to include history explicitly")
        if args.project and any(r.hget(key, "project") != args.project for key in history):
            raise ValueError("--project must match every historical document")
        allowed = {store.META_KEY, f"{store.INDEX_NAME}:curated", *keys, *notes, *history}
        if set(r.scan_iter()) != allowed or set(r.execute_command("FT._LIST")) != {namespace.index}:
            raise ValueError("Instance contains additional keys or indexes; refusing a potentially mixed export")
        revision = r.hget(store.META_KEY, "revision")
        image_id = run("inspect", args.container, "--format", "{{.Image}}")
        digests = json.loads(run("image", "inspect", image_id, "--format", "{{json .RepoDigests}}"))
        if not digests:
            raise ValueError("Snapshot image needs a portable registry digest")
        output.mkdir(parents=True)
        r.save()
        destination = output / "dump.rdb"
        docker_path = store._wsl_path(destination) if args.wsl_distro else str(destination)
        run("cp", args.container + ":/data/dump.rdb", docker_path)
        if r.hget(store.META_KEY, "revision") != revision:
            raise RuntimeError("Memory changed during export; discard this unfinished bundle and retry")
        metadata = {"schema": 1, "domain": domain, "project": args.project,
                    "model": store.MODEL_NAME, "index": store.INDEX_NAME, "image": digests[0],
                    "documents": len(report["documents"]), "chunks": len(keys), "note_chunks": len(notes),
                    "history_chunks": len(history),
                    "dbsize": r.dbsize(), "exported_at": store.utc_timestamp(),
                    "snapshot_sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source_root = (args.manifest.resolve().parent / manifest["corpus_root"]).resolve()
    for doc in manifest["documents"]:
        target = output / domain / doc["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        data = (source_root / doc["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != doc["content_sha256"]:
            raise RuntimeError("Source changed during export")
        target.write_bytes(data)
    manifest["corpus_root"] = domain
    manifest.pop("excluded", None)
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (output / "bundle.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    runtime = output / "runtime"
    runtime.mkdir()
    for name in ("mcp_server.py", "memory_store.py", "memory_routing.py", "curated_corpus.py",
                 "import_curated.py", "memory_history.py", "memory_relations.py", "requirements.txt"):
        shutil.copyfile(ROOT / name, runtime / name)
    (runtime / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/provision-models.py", runtime / "scripts/provision-models.py")
    shutil.copyfile(ROOT / "scripts" / "restore_curated.py", output / "restore.py")
    shutil.copyfile(ROOT / "docs" / "BUNDLE_README.md", output / "README.md")
    checksums = {p.relative_to(output).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in sorted(output.rglob("*")) if p.is_file()}
    (output / "checksums.json").write_text(json.dumps(checksums, indent=2) + "\n", encoding="utf-8")
    archive = output.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(output.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(output).as_posix())
    print(json.dumps({**metadata, "zip": str(archive), "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2))


if __name__ == "__main__":
    main()
