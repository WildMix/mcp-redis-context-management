"""Publish one reviewed corpus to an explicitly selected, dedicated Redis instance.

Only manifest-selected Markdown is read. Whole-corpus replacement is atomic,
portable across machines, and preserves independently saved agent notes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

import redis

from curated_corpus import prepare
import memory_store as store


def publish(r, manifest: Path, model) -> dict:
    report = prepare(manifest, model)
    domain = report["domain"]
    fingerprint = hashlib.sha256(json.dumps({k: report[k] for k in
        ("domain", "model", "chunker", "documents", "records")}, sort_keys=True).encode()).hexdigest()
    registry = f"{store.INDEX_NAME}:curated"
    # The short publication lock covers all writers of the curated corpus. Notes use mutate's
    # revision check and are preserved. Do not run the broad directory importer on this instance.
    with r.lock(f"{store.INDEX_NAME}:curated-publish", timeout=300, blocking_timeout=5):
        bound = r.hget(store.META_KEY, "corpus_domain")
        if bound and bound != domain:
            raise ValueError("Destination belongs to a different corpus domain")
        if not bound and (r.hget(store.META_KEY, "generation") or
                          list(r.scan_iter(match=f"{store.INDEX_NAME}:doc:*", count=1000)) or
                          list(r.scan_iter(match=f"{store.INDEX_NAME}:note:*", count=1000))):
            raise ValueError("Destination contains unclassified memory; use a fresh dedicated instance")
        if r.hget(registry, "fingerprint") == fingerprint:
            store.check_model(r)
            return {"domain": domain, "documents": len(report["documents"]),
                    "chunks": len(report["records"]), "status": "unchanged"}
        vectors = store.encode(model, [record["embedding_text"] for record in report["records"]])
        namespace = store.ensure_index(r, store.model_dimension(model))
        old_keys = json.loads(r.hget(registry, "keys") or "[]")
        if any(not key.startswith(namespace.docs) for key in old_keys):
            raise ValueError("Curated registry has invalid or rebuilt keys; restore the matching corpus first")
        now = store.utc_timestamp()
        records = []
        for record, vector in zip(report["records"], vectors, strict=True):
            key = namespace.docs + hashlib.sha256(record["topic_id"].encode()).hexdigest()
            fields = {k: record[k] for k in ("project", "domain", "document_id", "topic_id", "source",
                                            "heading", "content", "start_line", "end_line")}
            fields.update(kind="doc", created=int(time.time()), updated_at=now,
                          content_sha=hashlib.sha256(record["embedding_text"].encode()).hexdigest(),
                          embedding=vector.tobytes())
            records.append((key, fields))
        keys = [key for key, _ in records]
        def replace(pipe):
            if old_keys:
                pipe.delete(*old_keys)
            for key, fields in records:
                pipe.hset(key, mapping=fields)
            pipe.hset(registry, mapping={"fingerprint": fingerprint, "keys": json.dumps(keys),
                                        "documents": json.dumps(report["documents"]), "published_at": now})
            pipe.hset(store.META_KEY, mapping={"corpus_domain": domain, "corpus_schema": 1})
        store.mutate(r, namespace, replace)
    deadline = time.monotonic() + 60
    while True:
        info = r.ft(namespace.index).info()
        notes = sum(1 for _ in r.scan_iter(match=namespace.notes + "*"))
        if not int(info.get("indexing", 0)) and int(info["num_docs"]) == len(keys) + notes:
            if int(info.get("hash_indexing_failures", 0)):
                raise RuntimeError("Redis reported indexing failures")
            break
        if time.monotonic() >= deadline:
            raise RuntimeError("Publication committed, but index verification timed out; inspect before export")
        time.sleep(0.1)
    return {"domain": domain, "documents": len(report["documents"]), "chunks": len(keys),
            "status": "published", "fingerprint": fingerprint}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--redis-url", required=True, help="Explicit dedicated Redis destination, database 0")
    args = parser.parse_args()
    from urllib.parse import urlparse
    if urlparse(args.redis_url).path not in ("", "/0"):
        parser.error("Use database 0 of a dedicated instance")
    store.hold_wsl_open()
    with redis.Redis.from_url(args.redis_url, decode_responses=True) as r:
        result = publish(r, args.manifest, store.load_model())
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
