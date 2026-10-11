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
import stat
import sys
import time
import uuid
from pathlib import Path

import redis
from redis.commands.search.query import Query

from memory_store import (FILES_KEY, INDEX_NAME, META_KEY, MODEL_NAME, CHUNKER_VERSION, Chunk, Namespace,
                          check_model, chunk_markdown, connect, create_index, embedding_text, encode, ensure_index,
                          fit_chunks, git, index_exists, load_model, model_dimension, mutate, namespace_of,
                          project_of, tag)

# Used only outside git checkouts, where .gitignore cannot say what to skip
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "external", ".tools"}


def markdown_files(root: Path) -> list[Path]:
    """Every .md file under root; inside a git checkout only those git does not ignore."""
    has_git = any((parent / ".git").exists() for parent in (root, *root.parents))
    if git(root, "rev-parse", "--is-inside-work-tree", strict=has_git) == "true":
        listed = git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md", strict=True)
        files = set()
        for rel in listed.split("\0"):
            if not rel:
                continue
            path = root / rel
            try:
                if stat.S_ISREG(path.stat().st_mode):
                    files.add(path)
            except FileNotFoundError:  # a tracked file can have been deleted in the worktree
                continue
        return sorted(files)
    def failed(error):
        raise error
    paths = []
    for directory, dirs, files in os.walk(root, onerror=failed):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        paths.extend(Path(directory) / name for name in files if name.endswith(".md"))
    return sorted(paths)


def file_id(path: Path | str) -> str:
    return hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:16]


def is_under(path: str, root: Path) -> bool:
    return os.path.normcase(path).startswith(os.path.normcase(str(root)).rstrip("\\/") + os.sep)


def drop_chunks(r, pipe, namespace: Namespace, path: Path | str) -> None:
    """Queue deletion in the SAME transaction as replacement records/fingerprints."""
    keys = list(r.scan_iter(match=f"{namespace.docs}{file_id(path)}:*", count=1000))
    if keys:
        pipe.delete(*keys)


def import_dirs(r, roots: list[Path], project: str) -> None:
    check_model(r)
    namespace = namespace_of(r)
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
            digest = hashlib.sha256(f"{CHUNKER_VERSION}\0{MODEL_NAME}\0{project}\0".encode() + data).hexdigest()
            seen.add(str(path))
            if known.get(str(path)) != digest:
                todo.append((path, data.decode("utf-8", errors="replace"), digest))

    removed = [src for src in known if src not in seen and any(is_under(src, root) for root in scanned_roots)]
    total_chunks = 0
    if todo:
        print(f"Loading {MODEL_NAME} ...")
        model = load_model()
        namespace = ensure_index(r, model_dimension(model))

        for path, text, digest in todo:
            chunks = fit_chunks(model, "doc", str(path), chunk_markdown(text))
            texts = [embedding_text("doc", str(path), c.heading, c.text) for c in chunks]
            vectors = encode(model, texts) if chunks else []
            fid = file_id(path)
            records = []
            created = int(path.stat().st_mtime)
            for n, (chunk, text_embedded, vector) in enumerate(zip(chunks, texts, vectors)):
                records.append((f"{namespace.docs}{fid}:{n:04d}", {
                    "kind": "doc",
                    "project": project,
                    "content": chunk.text,
                    "heading": chunk.heading,
                    "source": str(path),
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "created": created,
                    # identical chunks in several worktrees share this, so a search shows them once
                    "content_sha": hashlib.sha1(text_embedded.encode("utf-8")).hexdigest(),
                    "embedding": vector.tobytes(),
                }))
            def replace(pipe):
                drop_chunks(r, pipe, namespace, path)
                for key, fields in records:
                    pipe.hset(key, mapping=fields)
                pipe.hset(FILES_KEY, str(path), digest)
            mutate(r, namespace, replace)
            total_chunks += len(chunks)
            print(f"  imported {path} ({len(chunks)} chunks)")

    for src in removed:
        def remove(pipe):
            drop_chunks(r, pipe, namespace, src)
            pipe.hdel(FILES_KEY, src)
        mutate(r, namespace, remove)
        print(f"  removed  {src}")

    in_project = (r.ft(INDEX_NAME).search(Query(f"@project:{{{tag(project)}}}").paging(0, 0)).total
                  if index_exists(r) else 0)
    print(f"{len(seen)} files in {len(scanned_roots)} directories: {len(todo)} imported ({total_chunks} chunks), "
          f"{len(seen) - len(todo)} unchanged, {len(removed)} removed. Project '{project}' holds {in_project} "
          f"entries (notes and doc chunks).")


def rebuild(r) -> None:
    """Stage a complete generation; publish only if no writer changed the source snapshot."""
    model = load_model()
    with r.lock(f"{INDEX_NAME}:initialize", timeout=30, blocking_timeout=30):
        before = r.hgetall(META_KEY)
        old = Namespace(before.get("generation", ""))
        had_index = index_exists(r, old.index)
    staged = Namespace(uuid.uuid4().hex)
    try:
        records = rebuilt_records(r, old, model)
        print(f"Staging {len(records)} chunks with {MODEL_NAME} ...")
        for i in range(0, len(records), 64):
            batch = records[i:i + 64]
            vectors = encode(model, [embedding_text(f["kind"], f["source"], f["heading"], f["content"])
                                     for _, f in batch])
            with r.pipeline(transaction=True) as pipe:
                for (suffix, fields), vector in zip(batch, vectors):
                    pipe.hset(f"{staged.prefix}:{suffix}", mapping={**fields, "embedding": vector.tobytes()})
                pipe.execute()
        create_index(r, model_dimension(model), staged)
        deadline = time.monotonic() + 120
        while True:
            info = r.ft(staged.index).info()
            if not int(info.get("indexing", 0)):
                if int(info.get("hash_indexing_failures", 0)) or int(info["num_docs"]) != len(records):
                    raise RuntimeError("The staged index failed validation; the active memory is unchanged.")
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("The staged index did not finish indexing; the active memory is unchanged.")
            time.sleep(0.1)

        with r.lock(f"{INDEX_NAME}:initialize", timeout=30, blocking_timeout=30):
            with r.pipeline(transaction=True) as pipe:
                pipe.watch(META_KEY)
                if pipe.hgetall(META_KEY) != before:
                    raise RuntimeError("Memory changed during rebuild; stop writers and retry. The active memory is unchanged.")
                pipe.multi()
                if had_index and not old.generation:
                    pipe.execute_command("FT.DROPINDEX", old.index)  # keep legacy records until cutover succeeds
                if old.generation and had_index:
                    pipe.execute_command("FT.ALIASUPDATE", INDEX_NAME, staged.index)
                else:
                    pipe.execute_command("FT.ALIASADD", INDEX_NAME, staged.index)
                pipe.hset(META_KEY, mapping={"model": MODEL_NAME, "dim": model_dimension(model),
                                            "generation": staged.generation,
                                            "revision": int(before.get("revision", 0)) + 1})
                try:
                    pipe.execute()
                except redis.WatchError as e:
                    raise RuntimeError("Memory changed during rebuild; retry. The active memory is unchanged.") from e
        cleanup_generation(r, old, drop_index=bool(old.generation))
        print(f"Published rebuilt index '{INDEX_NAME}' ({staged.generation})")
    finally:
        # A lost reply after EXEC is ambiguous. Never delete a generation until Redis confirms it
        # isn't active. If Redis is unavailable, leave the staging records for manual cleanup.
        try:
            if namespace_of(r) != staged:
                cleanup_generation(r, staged)
        except redis.RedisError:
            print(f"Could not clean up staging generation {staged.generation}; check Redis before removing it.", file=sys.stderr)


def rebuilt_records(r, old: Namespace, model) -> list[tuple[str, dict]]:
    """Re-chunk notes and stored doc paragraphs, preserving their provenance without source files."""
    fields = ["kind", "source", "heading", "content", "project", "note_id", "parts", "join_mode",
              "created", "agent", "branch", "worktree", "start_line", "end_line"]
    notes: dict[str, list[tuple[int, dict]]] = {}
    docs: dict[str, list[tuple[str, dict]]] = {}
    for prefix in (old.notes, old.docs):
        for key in r.scan_iter(match=f"{prefix}*", count=1000):
            values = r.hmget(key, fields)
            if values[0] is None:
                raise RuntimeError("A record disappeared during rebuild; retry after stopping writers.")
            record = {field: value for field, value in zip(fields, values) if value is not None}
            if record["kind"] == "note":
                notes.setdefault(record["note_id"], []).append((int(key.rsplit(":", 1)[1]), record))
            else:
                docs.setdefault(record["source"], []).append((key, record))
    out = []
    for note_id, pieces in notes.items():
        pieces.sort(key=lambda pair: pair[0])
        record = pieces[0][1]
        if [n for n, _ in pieces] != list(range(int(record["parts"]))):
            raise RuntimeError(f"Note {note_id} has missing chunks; repair it before rebuilding.")
        separator = "" if record.get("join_mode") == "exact" else "\n\n"
        content = separator.join(piece["content"] for _, piece in pieces)
        chunks = fit_chunks(model, "note", "", [Chunk(record["heading"], content, 1, 1)])
        for n, chunk in enumerate(chunks):
            out.append((f"note:{note_id}:{n}", {**record, "content": chunk.text,
                                              "parts": len(chunks), "join_mode": "exact"}))
    for source, pieces in docs.items():
        n = 0
        for _, record in sorted(pieces):
            original = Chunk(record["heading"], record["content"], int(record["start_line"]), int(record["end_line"]))
            for chunk in fit_chunks(model, "doc", source, [original]):
                text = embedding_text("doc", source, chunk.heading, chunk.text)
                out.append((f"doc:{file_id(source)}:{n:04d}", {**record, "content": chunk.text,
                            "content_sha": hashlib.sha1(text.encode("utf-8")).hexdigest()}))
                n += 1
    return out


def cleanup_generation(r, namespace: Namespace, drop_index: bool = True) -> None:
    if drop_index and index_exists(r, namespace.index):
        r.ft(namespace.index).dropindex(delete_documents=False)
    for prefix in (namespace.notes, namespace.docs):
        keys = list(r.scan_iter(match=f"{prefix}*", count=1000))
        for i in range(0, len(keys), 256):
            r.unlink(*keys[i:i + 256])


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
