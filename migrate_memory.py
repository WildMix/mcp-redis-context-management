"""Import existing Markdown into the project memory: the context written before agents saved their own
notes with save_to_project_memory (docs, design notes, handoffs, earlier agent memory files).

    python migrate_memory.py DIR [DIR ...] [--project NAME]
    python migrate_memory.py --rebuild

Every imported file is filed under one project: --project, or else the project of the current
directory (its repository's main checkout name, which all its worktrees share). Inside a git
checkout only the .md files git does not ignore are read, so dependencies, build output and packaged
copies stay out.

Each file is split into paragraphs (blank-line separated, fenced code kept whole, headings tracked as
the section path), small paragraphs are merged with their neighbours in the same section, and each
chunk is embedded locally. Re-running is incremental: unchanged files are skipped without loading the
model, changed files are re-chunked, and files that disappeared from an imported directory are
dropped. Notes saved by agents are never touched.

--rebuild re-embeds every stored note and doc from its stored text with EMBEDDING_MODEL and recreates
the index: needed after changing the model.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from redis.commands.search.query import Query

from memory_store import (DOC_PREFIX, FILES_KEY, INDEX_NAME, META_KEY, MODEL_NAME, NOTE_PREFIX, CHUNKER_VERSION,
                          check_model, chunk_markdown, connect, create_index, embedding_text, encode, git,
                          index_exists, load_model, model_dimension, project_of, tag)

# Used only outside git checkouts, where .gitignore cannot say what to skip
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "external", ".tools"}


def markdown_files(root: Path) -> list[Path]:
    """Every .md file under root; inside a git checkout only those git does not ignore."""
    if git(root, "rev-parse", "--is-inside-work-tree") == "true":
        listed = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md")
        return sorted(p for p in (root / rel for rel in listed.split("\0") if rel) if p.is_file())
    return sorted(p for p in root.rglob("*.md")
                  if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts[:-1]))


def file_id(path: Path | str) -> str:
    return hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:16]


def is_under(path: str, root: Path) -> bool:
    return os.path.normcase(path).startswith(os.path.normcase(str(root)).rstrip("\\/") + os.sep)


def drop_chunks(r, path: Path | str) -> None:
    keys = list(r.scan_iter(match=f"{DOC_PREFIX}{file_id(path)}:*", count=1000))
    if keys:
        r.delete(*keys)


def import_dirs(r, roots: list[Path], project: str) -> None:
    check_model(r)
    print(f"Importing into project '{project}'")

    # Find what changed before loading the model, so an up-to-date run takes a second, not ten.
    known = r.hgetall(FILES_KEY)
    seen: set[str] = set()
    todo: list[tuple[Path, str, str]] = []
    scanned_roots: list[Path] = []
    for root in roots:
        if not root.is_dir():
            print(f"skipping {root}: not a directory", file=sys.stderr)
            continue
        scanned_roots.append(root)
        for path in markdown_files(root):
            data = path.read_bytes()
            digest = hashlib.sha256(f"{CHUNKER_VERSION}\0{project}\0".encode() + data).hexdigest()
            seen.add(str(path))
            if known.get(str(path)) != digest:
                todo.append((path, data.decode("utf-8", errors="replace"), digest))

    removed = [src for src in known if src not in seen and any(is_under(src, root) for root in scanned_roots)]
    for src in removed:
        drop_chunks(r, src)
        r.hdel(FILES_KEY, src)
        print(f"  removed  {src}")

    total_chunks = 0
    if todo:
        print(f"Loading {MODEL_NAME} ...")
        model = load_model()
        if not index_exists(r):
            create_index(r, model_dimension(model))
            print(f"Created index '{INDEX_NAME}'")

        for path, text, digest in todo:
            chunks = chunk_markdown(text)
            texts = [embedding_text("doc", str(path), c.heading, c.text) for c in chunks]
            vectors = encode(model, texts) if chunks else []
            fid = file_id(path)
            drop_chunks(r, path)
            pipe = r.pipeline(transaction=True)
            for n, (chunk, text_embedded, vector) in enumerate(zip(chunks, texts, vectors)):
                pipe.hset(f"{DOC_PREFIX}{fid}:{n:04d}", mapping={
                    "kind": "doc",
                    "project": project,
                    "content": chunk.text,
                    "heading": chunk.heading,
                    "source": str(path),
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "created": int(path.stat().st_mtime),
                    # identical chunks in several worktrees share this, so a search shows them once
                    "content_sha": hashlib.sha1(text_embedded.encode("utf-8")).hexdigest(),
                    "embedding": vector.tobytes(),
                })
            pipe.hset(FILES_KEY, str(path), digest)
            pipe.execute()
            total_chunks += len(chunks)
            print(f"  imported {path} ({len(chunks)} chunks)")

    in_project = (r.ft(INDEX_NAME).search(Query(f"@project:{{{tag(project)}}}").paging(0, 0)).total
                  if index_exists(r) else 0)
    print(f"{len(seen)} files in {len(scanned_roots)} directories: {len(todo)} imported ({total_chunks} chunks), "
          f"{len(seen) - len(todo)} unchanged, {len(removed)} removed. Project '{project}' holds {in_project} "
          f"entries (notes and doc chunks).")


def rebuild(r) -> None:
    """Re-embed every stored chunk with EMBEDDING_MODEL, then recreate the index around them."""
    if index_exists(r):
        r.ft(INDEX_NAME).dropindex(delete_documents=False)
    r.delete(META_KEY)
    keys = [key for prefix in (NOTE_PREFIX, DOC_PREFIX) for key in r.scan_iter(match=f"{prefix}*", count=1000)]
    print(f"Re-embedding {len(keys)} chunks with {MODEL_NAME} ...")
    model = load_model()
    for i in range(0, len(keys), 256):
        batch = keys[i:i + 256]
        pipe = r.pipeline(transaction=False)
        for key in batch:
            pipe.hmget(key, "kind", "source", "heading", "content")
        texts = [embedding_text(*(value or "" for value in fields)) for fields in pipe.execute()]
        pipe = r.pipeline(transaction=False)
        for key, vector in zip(batch, encode(model, texts)):
            pipe.hset(key, "embedding", vector.tobytes())
        pipe.execute()
    create_index(r, model_dimension(model))
    print(f"Recreated index '{INDEX_NAME}'")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import existing Markdown into the project memory.")
    parser.add_argument("dirs", nargs="*", type=Path, help="directories whose .md files to import")
    parser.add_argument("--project", help="project to file them under (default: the current directory's)")
    parser.add_argument("--rebuild", action="store_true",
                        help="re-embed every stored note and doc with EMBEDDING_MODEL and recreate the index")
    args = parser.parse_args()
    if not args.dirs and not args.rebuild:
        parser.error("give the directories to import, or --rebuild")

    try:
        r = connect()
        if args.rebuild:
            rebuild(r)
        if args.dirs:
            project = args.project.lower() if args.project else project_of(os.getcwd())
            import_dirs(r, [d.expanduser().resolve() for d in args.dirs], project)
    except RuntimeError as e:
        sys.exit(str(e))
    return 0


if __name__ == "__main__":
    sys.exit(main())
