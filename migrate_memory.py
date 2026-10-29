"""Import existing Markdown into the project memory: the context written before agents saved their own
notes with save_to_project_memory (docs, design notes, handoffs, earlier agent memory files).

    python migrate_memory.py DIR [DIR ...] [--project NAME]
    python migrate_memory.py DIR --agent-context [--include-transcripts] [--dry-run]
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
from contextlib import nullcontext
import hashlib
import json
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
                          project_of, tag, utc_timestamp, sync_status_key, REDIS_URL)
from process_lock import process_lock

# Used only outside git checkouts, where .gitignore cannot say what to skip
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "external", ".tools", ".memory-imports"}
AGENT_CACHE_ROOT = Path(__file__).resolve().parent / ".memory-imports"


def markdown_files(root: Path, *, honor_gitignore: bool = True) -> list[Path]:
    """Every .md file under root; inside a git checkout only those git does not ignore."""
    has_git = any((parent / ".git").exists() for parent in (root, *root.parents))
    if honor_gitignore and git(root, "rev-parse", "--is-inside-work-tree", strict=has_git) == "true":
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


def import_dirs(r, roots: list[Path], project: str, *, literal_roots: tuple[Path, ...] = (), model_loader=None) -> dict:
    if r.hget(META_KEY, "corpus_domain"):
        raise ValueError("This is a curated destination; use import_curated.py with its reviewed manifest")
    check_model(r)
    namespace = namespace_of(r)
    print(f"Importing into project '{project}'")

    # Find what changed before loading the model, so an up-to-date run takes a second, not ten.
    known = r.hgetall(FILES_KEY)
    seen: set[str] = set()
    todo: list[tuple[Path, str, str, float]] = []
    scanned_roots: list[Path] = []
    for root in [*roots, *literal_roots]:
        if not root.is_dir():
            print(f"skipping {root}: not a directory", file=sys.stderr)
            continue
        scanned_roots.append(root)
        paths = markdown_files(root, honor_gitignore=False) if root in literal_roots else markdown_files(root)
        for path in paths:
            if str(path) in seen:
                continue
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
                raise RuntimeError(f"Source changed during the scan; retry: {path}")
            digest = hashlib.sha256(f"timestamps-v1\0{CHUNKER_VERSION}\0{MODEL_NAME}\0{project}\0".encode() + data).hexdigest()
            seen.add(str(path))
            if known.get(str(path)) != digest:
                todo.append((path, data.decode("utf-8", errors="replace"), digest, before.st_mtime))

    # Ordinary scans must not prune the ignored extraction cache (including another project's
    # exports or transcripts omitted on this run) when importing this tool's own repository.
    removed = [src for src in known if src not in seen and any(
        is_under(src, root) and (root in literal_roots or not is_under(src, AGENT_CACHE_ROOT))
        for root in scanned_roots)]
    total_chunks = 0
    if todo:
        print(f"Loading {MODEL_NAME} ...")
        model = (model_loader or load_model)()
        namespace = ensure_index(r, model_dimension(model))

        for path, text, digest, modified in todo:
            chunks = fit_chunks(model, "doc", str(path), chunk_markdown(text))
            texts = [embedding_text("doc", str(path), c.heading, c.text) for c in chunks]
            vectors = encode(model, texts) if chunks else []
            fid = file_id(path)
            records = []
            updated_at = utc_timestamp()
            for n, (chunk, text_embedded, vector) in enumerate(zip(chunks, texts, vectors)):
                records.append((f"{namespace.docs}{fid}:{n:04d}", {
                    "kind": "doc",
                    "project": project,
                    "content": chunk.text,
                    "heading": chunk.heading,
                    "source": str(path),
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "created": int(modified),
                    "source_modified_at": utc_timestamp(modified),
                    "updated_at": updated_at,
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
    return {"files": len(seen), "imported": len(todo), "unchanged": len(seen) - len(todo),
            "removed": len(removed), "chunks": total_chunks}


def rebuild(r) -> None:
    """Stage a complete generation; publish only if no writer changed the source snapshot."""
    if r.hget(META_KEY, "corpus_domain"):
        raise ValueError("Revalidate the curated manifest and import into a fresh instance for a model change")
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
              "created", "updated_at", "source_modified_at", "agent", "branch", "worktree", "start_line", "end_line"]
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


def main(argv=None, *, model_loader=None) -> int:
    parser = argparse.ArgumentParser(description="Import existing Markdown into the project memory.")
    parser.add_argument("dirs", nargs="*", type=Path, help="directories whose .md files to import")
    parser.add_argument("--project", help="project to file them under (default: the current directory's)")
    parser.add_argument("--rebuild", action="store_true",
                        help="re-embed every stored note and doc with EMBEDDING_MODEL and recreate the index")
    parser.add_argument("--agent-context", action="store_true",
                        help="also discover project-local and global Claude/Codex memories and summaries")
    parser.add_argument("--include-transcripts", action="store_true",
                        help="with --agent-context, also import user/assistant conversation text")
    parser.add_argument("--dry-run", action="store_true",
                        help="list selected sources without writing files, connecting to Redis or loading a model")
    parser.add_argument("--claude-home", type=Path,
                        default=Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))),
                        help="Claude data directory (default: CLAUDE_CONFIG_DIR or ~/.claude)")
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))),
                        help="Codex data directory (default: CODEX_HOME or ~/.codex)")
    args = parser.parse_args(argv)
    if not args.dirs and not args.rebuild and not args.agent_context:
        parser.error("give the directories to import, --agent-context, or --rebuild")
    if args.include_transcripts and not args.agent_context:
        parser.error("--include-transcripts requires --agent-context")
    if args.dry_run and args.rebuild:
        parser.error("--dry-run cannot be combined with --rebuild")

    try:
        # Cover discovery, generated files, document transactions and rebuilds with the same
        # process lock. Redis's short transactions alone cannot serialize complete import runs.
        with nullcontext() if args.dry_run else process_lock(f"migration:{REDIS_URL}:{INDEX_NAME}"):
            return run(args, model_loader=model_loader)
    except (RuntimeError, OSError, ValueError, redis.RedisError) as error:
        print(str(error), file=sys.stderr)
        return 1


def run(args, *, model_loader=None) -> int:
    r = None
    project = args.project.lower() if args.project else project_of(os.getcwd())
    started = utc_timestamp()
    status_key = sync_status_key(project)
    if not args.dry_run:
        r = connect()
        if args.dirs or args.agent_context:
            r.hset(status_key, mapping={"last_attempt_started_at": started, "status": "running"})
    try:
        roots = [d.expanduser().resolve() for d in args.dirs]
        if args.agent_context and not roots:
            roots = [Path.cwd()]
        extracts = []
        if args.agent_context:
            import agent_context
            if any(not root.is_dir() for root in roots):
                raise RuntimeError("Agent context roots must be existing project directories.")
            worktrees = agent_context.project_roots(roots)
            print("Agent context scope: " + ", ".join(map(str, worktrees)))
            extracts = agent_context.discover(worktrees, args.claude_home.expanduser().resolve(),
                                              args.codex_home.expanduser().resolve(), args.include_transcripts)
            for item in extracts:
                print(f"  {item.category}: {item.source} ({len(item.text)} characters)")
            print(f"Selected {len(extracts)} agent context extracts for project '{project}'.")
        if args.dry_run:
            for path in sorted({path for root in roots for path in markdown_files(root)}):
                print(f"  document: {path}")
            print("Dry run: no files or database entries changed.")
            return 0

        literal_roots = ()
        if args.agent_context:
            # Generated Markdown is private local input, deliberately ignored by Git.
            project_key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:20]
            destination = AGENT_CACHE_ROOT / project_key
            literal_roots = tuple(agent_context.export(extracts, destination, args.include_transcripts))
            print(f"Extracted context: {destination}")
        if args.rebuild:
            rebuild(r)
        if roots:
            counts = import_dirs(r, roots, project, literal_roots=literal_roots, model_loader=model_loader)
            # Saved notes already have a trustworthy original save time, unlike old imports.
            namespace = namespace_of(r)
            for key in r.scan_iter(match=namespace.notes + "*", count=1000):
                note_project, updated, created = r.hmget(key, "project", "updated_at", "created")
                if note_project == project and not updated and created:
                    mutate(r, namespace, lambda pipe: pipe.hsetnx(key, "updated_at", utc_timestamp(float(created))))
            r.hset(status_key, mapping={"status": "ok", "last_success_started_at": started,
                "last_success_completed_at": utc_timestamp(), "directories": json.dumps([str(root) for root in roots]),
                **counts})
            r.hdel(status_key, "last_error_type")
    except Exception as error:
        if r is not None and (args.dirs or args.agent_context):
            try:
                r.hset(status_key, mapping={"status": "error", "last_error_type": type(error).__name__})
            except redis.RedisError:
                pass
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
