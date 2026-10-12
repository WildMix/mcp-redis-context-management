"""Read-only embedding comparison using a frozen copy of the live memory corpus.

No operation in this script writes to Redis. Model downloads and corpus snapshots
stay under .embedding-benchmark, which is excluded from Git.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import platform
import random
import re
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / ".embedding-benchmark"
MODELS = {
    "minilm": "sentence-transformers/all-MiniLM-L6-v2",
    "arctic": "Snowflake/snowflake-arctic-embed-xs",
    "bge": "BAAI/bge-small-en-v1.5",
}
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def snapshot():
    import redis
    import memory_store as store

    r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True, socket_timeout=30)
    before = r.hgetall(store.META_KEY)
    namespace = store.namespace_of(r)
    keys = sorted(set(r.scan_iter(match=namespace.docs + "*")) |
                  set(r.scan_iter(match=namespace.notes + "*")))
    fields = ["project", "kind", "source", "heading", "content", "content_sha",
              "note_id", "parts", "join_mode", "start_line", "end_line", "created"]
    records = []
    for start in range(0, len(keys), 256):
        pipe = r.pipeline(transaction=False)
        batch = keys[start:start + 256]
        for key in batch:
            pipe.hmget(key, fields)
        for key, values in zip(batch, pipe.execute()):
            record = dict(zip(fields, (value or "" for value in values)))
            if not record["kind"]:
                raise RuntimeError("A record disappeared during the snapshot; rerun.")
            record["key"] = key
            record["text"] = store.embedding_text(record["kind"], record["source"],
                                                   record["heading"], record["content"])
            record["group"] = (record["note_id"] or record["content_sha"] or key)
            records.append(record)
    if before != r.hgetall(store.META_KEY):
        raise RuntimeError("Memory changed during the snapshot; rerun.")
    result = {"captured_at": store.utc_timestamp(), "metadata": before,
              "records": records}
    write_json(OUTPUT / "corpus.json", result)
    print(json.dumps({"records": len(records), "unique_texts": len({r['text'] for r in records}),
                      "projects": collections.Counter(r["project"] for r in records),
                      "kinds": collections.Counter(r["kind"] for r in records)}, indent=2))


def download():
    from huggingface_hub import snapshot_download

    for name in ("arctic", "bge"):
        print("Downloading " + MODELS[name], flush=True)
        path = snapshot_download(MODELS[name], cache_dir=str(OUTPUT / "models"),
                                 allow_patterns=["*.json", "*.safetensors", "vocab.txt", "*.txt"],
                                 ignore_patterns=["onnx/*", "openvino/*"])
        print(name + ": " + path, flush=True)


def corpus():
    return json.loads((OUTPUT / "corpus.json").read_text(encoding="utf-8"))


def cases():
    return json.loads((ROOT / "benchmarks/embedding_cases.json").read_text(encoding="utf-8"))


def questions():
    return [{"id": intent["id"] + ("d" if n == 0 else "p"),
             "intent": intent["id"], "project": intent["project"],
             "style": "direct" if n == 0 else "paraphrased", "query": query}
            for intent in cases()["intents"] for n, query in enumerate(intent["queries"])]


def labels():
    records = corpus()["records"]
    result = {}
    for intent in cases()["intents"]:
        matched = [i for i, r in enumerate(records) if r["project"] == intent["project"]
                   and all(re.search(pattern, r["content"], re.I | re.S) for pattern in intent["patterns"])
                   and r["group"] not in intent.get("excluded_groups", [])]
        groups = sorted({records[i]["group"] for i in matched} | set(intent.get("additional_groups", [])))
        if not groups:
            raise ValueError("No evidence for " + intent["id"])
        result[intent["id"]] = {"answer": intent["answer"], "groups": groups, "record_ids": matched}
        print(intent["id"], len(groups), "answer groups; evidence IDs:", matched, flush=True)
    write_json(OUTPUT / "labels.json", result)


def model_path(name):
    from huggingface_hub import snapshot_download
    return snapshot_download(MODELS[name], local_files_only=True,
                             cache_dir=None if name == "minilm" else str(OUTPUT / "models"),
                             allow_patterns=["*.json", "*.safetensors", "vocab.txt", "*.txt"],
                             ignore_patterns=["onnx/*", "openvino/*"])


def run(name, rounds, timing_only=False):
    print(name + ": importing embedding runtime", flush=True)
    started = time.perf_counter()
    import numpy as np
    import torch
    import sentence_transformers
    from sentence_transformers import SentenceTransformer

    imports_seconds = time.perf_counter() - started
    records = corpus()["records"]
    texts = list(dict.fromkeys(r["text"] for r in records))
    text_indexes = {text: i for i, text in enumerate(texts)}
    path = model_path(name)
    started = time.perf_counter()
    model = SentenceTransformer(path, device="cpu", local_files_only=True)
    load_seconds = time.perf_counter() - started
    model.eval()
    qs = questions()
    prefix = "" if name == "minilm" else QUERY_PREFIX
    # Explicit formatting avoids implicit/default prompts. Both Arctic and BGE
    # prescribe this query-only instruction for short-query passage retrieval.
    def encode(values, query=False):
        return model.encode(values, prompt=prefix if query else "", batch_size=64,
                            normalize_embeddings=True, show_progress_bar=False).astype(np.float32)

    token_counts = [len(ids) for ids in model.tokenizer(texts, truncation=False,
                    add_special_tokens=True, verbose=False)["input_ids"]]
    too_long = sum(n > model.max_seq_length for n in token_counts)
    if too_long:
        raise ValueError(f"{too_long} existing chunks exceed {name}'s sequence limit")
    for _ in range(5):
        encode([qs[0]["query"]], query=True)
    print(f"{name}: {len(texts)} inputs, {torch.get_num_threads()} CPU threads, "
          f"max input {max(token_counts)} tokens", flush=True)
    if timing_only:
        previous = json.loads((OUTPUT / f"{name}-results.json").read_text(encoding="utf-8"))
        assert previous["corpus_sha256"] == hashlib.sha256((OUTPUT / "corpus.json").read_bytes()).hexdigest()
        assert [q["query"] for q in previous["rankings"]] == [q["query"] for q in qs]
        vectors = np.load(OUTPUT / f"{name}-documents.npy")
        query_vectors = np.load(OUTPUT / f"{name}-queries.npy")
        index_seconds = previous["index_seconds"]
    else:
        started = time.perf_counter()
        batches = []
        for i in range(0, len(texts), 256):
            batches.append(encode(texts[i:i + 256]))
            print(f"{name}: embedded {min(i + 256, len(texts))}/{len(texts)}", flush=True)
        vectors = np.concatenate(batches)
        index_seconds = time.perf_counter() - started
        np.save(OUTPUT / f"{name}-documents.npy", vectors)
        query_vectors = encode([q["query"] for q in qs], query=True)
        np.save(OUTPUT / f"{name}-queries.npy", query_vectors)
    assert np.isfinite(vectors).all() and np.isfinite(query_vectors).all()
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1, atol=1e-5)
    assert np.allclose(np.linalg.norm(query_vectors, axis=1), 1, atol=1e-5)

    # Deduplicate identical content and fold note chunks just as the server does.
    project_rows = {project: [i for i, r in enumerate(records) if r["project"] == project]
                    for project in {r["project"] for r in records}}
    project_vectors = {p: vectors[[text_indexes[records[i]["text"]] for i in ids]]
                       for p, ids in project_rows.items()}
    def search(vector, project):
        ids = project_rows[project]
        scores = project_vectors[project] @ vector
        seen = set()
        ranked = []
        for local_id in np.argsort(-scores, kind="stable"):
            record_id = ids[int(local_id)]
            group = records[record_id]["group"]
            if group in seen:
                continue
            seen.add(group)
            ranked.append({"record_id": record_id, "group": group,
                           "score": float(scores[local_id])})
            if len(ranked) == 10:
                break
        return ranked

    rankings = [{**q, "top10": search(vector, q["project"])}
                for q, vector in zip(qs, query_vectors)]
    timings = []
    rng = random.Random(20261012)
    for round_number in range(rounds):
        order = list(range(len(qs)))
        rng.shuffle(order)
        for i in order:
            q = qs[i]
            start = time.perf_counter_ns()
            vector = encode([q["query"]], query=True)[0]
            encoded = time.perf_counter_ns()
            search(vector, q["project"])
            end = time.perf_counter_ns()
            timings.append({"id": q["id"], "round": round_number,
                            "encode_ms": (encoded - start) / 1e6,
                            "local_search_ms": (end - start) / 1e6})
        print(f"{name}: latency round {round_number + 1}/{rounds}", flush=True)
    result = {"model": MODELS[name], "revision": Path(path).name, "device": str(model.device),
              "parameters": sum(p.numel() for p in model.parameters()),
              "dimensions": int(vectors.shape[1]), "max_seq_length": model.max_seq_length,
              "threads": torch.get_num_threads(), "interop_threads": torch.get_num_interop_threads(),
              "torch": torch.__version__, "sentence_transformers": sentence_transformers.__version__,
              "python": platform.python_version(), "platform": platform.platform(),
              "corpus_sha256": hashlib.sha256((OUTPUT / "corpus.json").read_bytes()).hexdigest(),
              "cases_sha256": hashlib.sha256((ROOT / "benchmarks/embedding_cases.json").read_bytes()).hexdigest(),
              "unique_texts": len(texts), "records": len(records),
              "token_percentiles": np.percentile(token_counts, [50, 95, 100]).tolist(),
              "query_prefix": prefix, "imports_seconds": imports_seconds,
              "cached_model_load_seconds": load_seconds, "index_seconds": index_seconds,
              "texts_per_second": len(texts) / index_seconds, "timings": timings, "rankings": rankings}
    result["timing_only"] = timing_only
    suffix = "-recheck" if timing_only else ""
    write_json(OUTPUT / f"{name}{suffix}-results.json", result)
    print(json.dumps({"model": name, "texts_per_second": result["texts_per_second"],
                      "encode_p50_ms": float(np.median([t["encode_ms"] for t in timings]))}), flush=True)


def pool():
    """Make a model-blind pool for judging answer-bearing passages, not ranks."""
    records = corpus()["records"]
    results = [json.loads((OUTPUT / f"{name}-results.json").read_text(encoding="utf-8"))
               for name in MODELS]
    gold = json.loads((OUTPUT / "labels.json").read_text(encoding="utf-8"))
    output = []
    for intent in cases()["intents"]:
        ids = {}
        for result in results:
            for q in result["rankings"]:
                if q["intent"] == intent["id"]:
                    for hit in q["top10"][:3]:
                        ids[hit["group"]] = hit["record_id"]
        candidates = []
        for group, record_id in sorted(ids.items()):
            r = records[record_id]
            # A note hit returns the full note in the actual server.
            content = r["content"]
            if r["note_id"]:
                content = "\n".join(part["content"] for part in records
                                    if part["note_id"] == r["note_id"])
            candidates.append({"record_id": record_id, "group": group,
                               "initial_match": group in gold[intent["id"]]["groups"],
                               "heading": r["heading"], "content": content})
        output.append({"id": intent["id"], "answer": intent["answer"], "candidates": candidates})
    write_json(OUTPUT / "review-pool.json", output)
    print("Wrote a model-blind top-three pool with", sum(len(i["candidates"]) for i in output), "passages")


def score():
    import numpy as np
    gold = json.loads((OUTPUT / "labels.json").read_text(encoding="utf-8"))
    judgments_path = ROOT / "benchmarks/embedding_judgments.json"
    judgments = json.loads(judgments_path.read_text(encoding="utf-8")) if judgments_path.exists() else None
    results = {}
    for name in MODELS:
        path = OUTPUT / f"{name}-results.json"
        if not path.exists():
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        timing_passes = [result["timings"]]
        recheck_path = OUTPUT / f"{name}-recheck-results.json"
        if recheck_path.exists():
            timing_passes.append(json.loads(recheck_path.read_text(encoding="utf-8"))["timings"])
        timings = [t for one_pass in timing_passes for t in one_pass]
        interleaved_path = OUTPUT / "interleaved-timings.json"
        interleaved = json.loads(interleaved_path.read_text(encoding="utf-8")) if interleaved_path.exists() else None
        comparison_timings = interleaved["models"][name] if interleaved else timings
        evaluated = []
        for q in result["rankings"]:
            allowed = judgments[q["intent"]]["relevant_groups"] if judgments else gold[q["intent"]]["groups"]
            rank = next((n for n, hit in enumerate(q["top10"][:3], 1)
                         if hit["group"] in allowed), None)
            evaluated.append({"id": q["id"], "intent": q["intent"], "style": q["style"],
                              "project": q["project"], "rank": rank})
        def quality(rows):
            return {"queries": len(rows), "hits_at_1": sum(q["rank"] == 1 for q in rows),
                    "hits_at_3": sum(q["rank"] is not None for q in rows),
                    "mrr_at_3": sum(1 / q["rank"] if q["rank"] else 0 for q in rows) / len(rows)}
        summary = {"model": result["model"], **quality(evaluated),
                   "by_project": {p: quality([q for q in evaluated if q["project"] == p])
                                  for p in sorted({q["project"] for q in evaluated})},
                   "by_style": {p: quality([q for q in evaluated if q["style"] == p])
                                for p in ("direct", "paraphrased")},
                   "encode_ms": dict(zip(("p50", "p95"), np.percentile(
                       [t["encode_ms"] for t in comparison_timings], [50, 95]).tolist())),
                   "sequential_pass_local_search_ms": dict(zip(("p50", "p95"), np.percentile(
                       [t["local_search_ms"] for t in timings], [50, 95]).tolist())),
                   "timing_samples": len(comparison_timings),
                   "timing_method": "interleaved" if interleaved else "sequential passes",
                   "encode_p50_by_pass_ms": [float(np.median([t["encode_ms"] for t in ts])) for ts in timing_passes],
                   "index_seconds": result["index_seconds"], "texts_per_second": result["texts_per_second"],
                   "parameters": result["parameters"], "revision": result["revision"],
                   "cached_model_load_seconds": result["cached_model_load_seconds"],
                   "evaluated_queries": evaluated}
        results[name] = summary
        print(json.dumps({"name": name, **{k: v for k, v in summary.items()
                         if k not in ("evaluated_queries", "by_project", "by_style")}}, indent=2))
    write_json(OUTPUT / "summary.json", results)


def latency(rounds):
    """Counterbalance desktop timing drift by interleaving all models in one process."""
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer
    models = {name: SentenceTransformer(model_path(name), device="cpu", local_files_only=True)
              for name in MODELS}
    qs = questions()
    def encode(name, query):
        return models[name].encode([query], prompt="" if name == "minilm" else QUERY_PREFIX,
                                  batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    for _ in range(5):
        for name in models:
            encode(name, qs[0]["query"])
    timings = {name: [] for name in models}
    rng = random.Random(20261012)
    for round_id in range(rounds):
        order = list(range(len(qs)))
        rng.shuffle(order)
        for query_id in order:
            names = list(models)
            rng.shuffle(names)
            for name in names:
                start = time.perf_counter_ns()
                encode(name, qs[query_id]["query"])
                elapsed = (time.perf_counter_ns() - start) / 1e6
                timings[name].append({"id": qs[query_id]["id"], "round": round_id, "encode_ms": elapsed})
        print(f"Interleaved latency round {round_id + 1}/{rounds}", flush=True)
    result = {"threads": torch.get_num_threads(), "rounds": rounds, "seed": 20261012,
              "models": timings, "method": "Random model order for each query; single process, serial inference, all three models resident"}
    write_json(OUTPUT / "interleaved-timings.json", result)
    print(json.dumps({name: np.percentile([t["encode_ms"] for t in values], [50, 95]).tolist()
                      for name, values in timings.items()}, indent=2), flush=True)


def validate():
    """Compare baseline exact ranking with the existing Redis HNSW index, read-only."""
    import numpy as np
    import redis
    from redis.commands.search.query import Query
    import memory_store as store

    snapshot = corpus()
    records = snapshot["records"]
    r = redis.Redis.from_url(store.REDIS_URL, decode_responses=True, socket_timeout=30)
    if r.hgetall(store.META_KEY) != snapshot["metadata"]:
        raise RuntimeError("Live memory changed; cannot validate against the frozen snapshot.")
    namespace = store.namespace_of(r)
    baseline = json.loads((OUTPUT / "minilm-results.json").read_text(encoding="utf-8"))
    vectors = np.load(OUTPUT / "minilm-queries.npy")
    checks = []
    for q, vector in zip(baseline["rankings"], vectors):
        count = 30
        while True:
            query = (Query(f"(@project:{{{store.tag(q['project'])}}})=>[KNN {count} @embedding $vector AS distance]")
                     .sort_by("distance").return_fields("note_id", "content_sha")
                     .paging(0, count).dialect(2))
            docs = r.ft(namespace.index).search(query, query_params={"vector": vector.tobytes()}).docs
            groups = list(dict.fromkeys(getattr(d, "note_id", "") or getattr(d, "content_sha", "") or d.id
                                        for d in docs))
            if len(groups) >= 3 or len(docs) < count:
                break
            count *= 2
        expected = [h["group"] for h in q["top10"][:3]]
        checks.append({"id": q["id"], "same_top3_order": groups[:3] == expected,
                       "top3_overlap": len(set(groups[:3]) & set(expected))})
    assert r.hgetall(store.META_KEY) == snapshot["metadata"]
    texts = list(dict.fromkeys(record["text"] for record in records))
    text_ids = {text: i for i, text in enumerate(texts)}
    embedded = np.load(OUTPUT / "minilm-documents.npy")
    raw = redis.Redis.from_url(store.REDIS_URL, decode_responses=False, socket_timeout=30)
    errors = []
    for record in records[::100]:
        existing = np.frombuffer(raw.hget(record["key"], "embedding"), dtype=np.float32)
        fresh = embedded[text_ids[record["text"]]]
        errors.append(float(np.max(np.abs(existing - fresh))))
    result = {"query_checks": checks, "same_top3_order": sum(c["same_top3_order"] for c in checks),
              "queries": len(checks), "stored_vector_samples": len(errors),
              "max_component_difference": max(errors), "live_metadata_unchanged": True}
    write_json(OUTPUT / "baseline-validation.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "query_checks"}, indent=2))


def report():
    score()
    snapshot = corpus()
    summary = json.loads((OUTPUT / "summary.json").read_text(encoding="utf-8"))
    judgments = json.loads((ROOT / "benchmarks/embedding_judgments.json").read_text(encoding="utf-8"))
    original = {name: json.loads((OUTPUT / f"{name}-results.json").read_text(encoding="utf-8")) for name in MODELS}
    baseline = original["minilm"]
    for result in original.values():
        assert result["corpus_sha256"] == baseline["corpus_sha256"]
        assert [(q["id"], q["query"]) for q in result["rankings"]] == [(q["id"], q["query"]) for q in baseline["rankings"]]
        for q in result["rankings"]:
            judged = set(judgments[q["intent"]]["relevant_groups"] + judgments[q["intent"]]["irrelevant_groups"])
            assert all(hit["group"] in judged for hit in q["top10"][:3]), q["id"]
    paired = {}
    base_by_id = {q["id"]: q["rank"] for q in summary["minilm"]["evaluated_queries"]}
    for name in ("arctic", "bge"):
        rows = summary[name]["evaluated_queries"]
        paired[name] = {
            "new_top3_hits_vs_minilm": [q["id"] for q in rows if q["rank"] and not base_by_id[q["id"]]],
            "lost_top3_hits_vs_minilm": [q["id"] for q in rows if not q["rank"] and base_by_id[q["id"]]],
        }
    result = {
        "date": snapshot["captured_at"], "corpus_sha256": baseline["corpus_sha256"],
        "corpus_revision": snapshot["metadata"],
        "records": len(snapshot["records"]), "unique_embedding_inputs": baseline["unique_texts"],
        "projects": dict(collections.Counter(r["project"] for r in snapshot["records"])),
        "environment": {**{k: baseline[k] for k in ("device", "threads", "interop_threads", "torch", "sentence_transformers", "python", "platform")},
                        "hardware": json.loads((OUTPUT / "hardware.json").read_text(encoding="utf-8-sig"))
                        if (OUTPUT / "hardware.json").exists() else None},
        "methodology": [
            "40 authored English queries: 20 intents, each with direct and paraphrased wording; 20 queries per project.",
            "Questions and expected answers defined before model retrieval; pooled top-three passages reviewed without model names or ranks.",
            "Binary relevance means a passage supplies useful, non-contradictory evidence for the intended answer, not necessarily every requested detail.",
            "Same frozen chunks and title/filename/heading prefixes; all inputs fit 256 tokens. No model-specific rechunking.",
            "Normalized float32 PyTorch CPU embeddings; no quantization. Model-prescribed query instruction for Arctic and BGE only.",
            "Exact cosine ranking within each project, folding identical content and note chunks as in production. Note relevance considers the full returned note.",
            "Warm single-query encoding first measured in two sequential passes with reversed model order. Timing drift prompted a final randomized interleaved run: five rounds of 40 queries per model, all models resident, serial inference. Headline latency uses this interleaved run.",
            "Batch encoding throughput measured once per model on 2,157 unique existing chunks, batch_size 64; excludes token-budget validation, Redis writes and file discovery.",
            "Timing covers embedding and optional in-memory ranking, not MCP/HTTP or Redis round trips.",
        ],
        "limitations": [
            "Small author-judged diagnostic set, not a held-out user-query log; paired paraphrases are not independent intents.",
            "Balanced project weighting is not an observed distribution of user traffic.",
            "Pooled relevance judgments cover the first three results only; do not use them for Recall@10 or exhaustive recall.",
            "One laptop under ordinary desktop conditions; latency and one-pass throughput may vary with power, temperature and background work.",
            "English only; no multilingual or raw source-code retrieval evaluation.",
            "Alternative Redis HNSW indexes were not built. The baseline was checked against the live HNSW index.",
        ],
        "models": summary, "paired_comparison": paired,
        "baseline_validation": json.loads((OUTPUT / "baseline-validation.json").read_text(encoding="utf-8")),
        "sources": {"arctic_query_format": "https://huggingface.co/Snowflake/snowflake-arctic-embed-xs",
                    "bge_query_format": "https://huggingface.co/BAAI/bge-small-en-v1.5"},
    }
    destination = ROOT / "benchmarks" / f"embedding-comparison-{snapshot['captured_at'][:10]}.json"
    write_json(destination, result)
    print("Wrote " + str(destination.relative_to(ROOT)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["snapshot", "download", "labels", "run", "pool", "score", "validate", "report", "latency"])
    parser.add_argument("--model", choices=list(MODELS), default="minilm")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--timing-only", action="store_true")
    args = parser.parse_args()
    OUTPUT.mkdir(exist_ok=True)
    if args.command == "run":
        run(args.model, args.rounds, args.timing_only)
    elif args.command == "latency":
        latency(args.rounds)
    else:
        {"snapshot": snapshot, "download": download, "labels": labels,
         "pool": pool, "score": score, "validate": validate, "report": report}[args.command]()


if __name__ == "__main__":
    main()
