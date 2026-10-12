"""Validate explicitly selected, portable Markdown topics with the production chunker.

This module never connects to Redis. Its records are an import preview, not a new
storage/indexing implementation. One authored H2 section must be one fitted chunk.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re

from memory_store import CHUNKER_VERSION, MODEL_NAME, chunk_markdown, embedding_text, fit_chunks


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def topic_headings(text: str) -> list[str]:
    """Reject implicit/preamble chunks and unclosed fences before calling the importer."""
    title = None
    headings = []
    fence = None
    for line in text.splitlines():
        stripped = line.strip()
        if fence:
            if re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", stripped):
                fence = None
            continue
        match = re.match(r"^(`{3,}|~{3,})", stripped)
        if match:
            if not headings:
                raise ValueError("Content must be inside a topic section")
            fence = match[1]
        elif line.startswith("# "):
            if title is not None or headings:
                raise ValueError("Use exactly one document title")
            title = line[2:].strip()
        elif line.startswith("## "):
            if not title or not line[3:].strip():
                raise ValueError("A topic needs a document title")
            headings.append(title + " > " + line[3:].strip())
        elif re.match(r"^#{1,6}\s", line):
            raise ValueError("Use H2 topics, without nested or skipped heading levels")
        elif stripped and not headings:
            raise ValueError("Move preamble/metadata out of embedding content")
    if fence:
        raise ValueError("Unclosed code fence")
    if not title or not headings or len(set(map(slug, headings))) != len(headings):
        raise ValueError("Use a title and unique, nonempty topic headings")
    return headings


def prepare(manifest_path: Path, model) -> dict:
    """Read an allowlist; verify reviewed bytes and one complete chunk per topic.

    Files absent from the allowlist never become input. Logical source paths and
    document/topic IDs remain independent of this machine's corpus directory.
    """
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or manifest.get("domain") not in ("work", "personal"):
        raise ValueError("Expected a schema 1, single-domain curated manifest")
    domain = manifest["domain"]
    root = (manifest_path.parent / manifest["corpus_root"]).resolve(strict=True)
    seen_ids, seen_files = set(), set()
    records, summaries = [], []
    if not manifest.get("documents"):
        raise ValueError("An empty allowlist is not an importable corpus")
    for doc in manifest["documents"]:
        doc_id, relative = doc["document_id"], Path(doc["file"])
        if (relative.is_absolute() or ".." in relative.parts or relative.suffix != ".md"
                or not doc_id.startswith(domain + "/") or doc_id in seen_ids):
            raise ValueError("Invalid or duplicate document identity: " + doc_id)
        path = (root / relative).resolve(strict=True)
        if not path.is_relative_to(root) or path in seen_files:
            raise ValueError("Duplicate file or file outside corpus root: " + str(relative))
        seen_ids.add(doc_id)
        seen_files.add(path)
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != doc["content_sha256"]:
            raise ValueError("Unreviewed content change: " + str(relative))
        text = data.decode("utf-8")
        expected = topic_headings(text)
        source = domain + "/" + relative.as_posix()
        raw = chunk_markdown(text)
        fitted = fit_chunks(model, "doc", source, raw)
        if [c.heading for c in fitted] != expected:
            counts = Counter(c.heading for c in fitted)
            bad = [h for h in expected if counts[h] != 1]
            raise ValueError(f"{relative}: each topic must produce exactly one complete chunk: {bad}")
        per_doc = []
        for chunk in fitted:
            topic_id = doc_id + "#" + slug(chunk.heading.split(" > ", 1)[1])
            embedded = embedding_text("doc", source, chunk.heading, chunk.text)
            prompt = getattr(model, "default_prompt_name", None)
            token_input = model.prompts[prompt] + embedded if prompt else embedded
            tokens = len(model.tokenizer(token_input, add_special_tokens=True,
                                         truncation=False, verbose=False)["input_ids"])
            record = {"document_id": doc_id, "topic_id": topic_id, "domain": domain,
                      "project": doc["project"], "source": source, "heading": chunk.heading,
                      "content": chunk.text, "start_line": chunk.start_line, "end_line": chunk.end_line,
                      "tokens": tokens, "characters": len(chunk.text), "embedding_text": embedded}
            records.append(record)
            per_doc.append(topic_id)
        if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,127}", doc["project"]):
            raise ValueError("Invalid project scope: " + doc["project"])
        summaries.append({"document_id": doc_id, "file": relative.as_posix(),
                          "content_sha256": digest, "topics": per_doc})
    return {"schema": 1, "status": "validated-preview-not-imported", "domain": domain,
            "model": MODEL_NAME, "model_token_limit": model.max_seq_length,
            "chunker": CHUNKER_VERSION, "documents": summaries, "records": records,
            "excluded": manifest.get("excluded", []),
            "unselected_files": sorted(p.relative_to(root).as_posix() for p in root.rglob("*.md")
                                       if p.resolve() not in seen_files)}
