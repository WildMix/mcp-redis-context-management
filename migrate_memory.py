"""Index Markdown files into a Redis Stack vector index for the project-memory MCP server.

    python migrate_memory.py [DIR ...] [--reset]

With no DIR, the directories listed in sources.txt (next to this script) are indexed. Inside a git
checkout only the .md files git does not ignore are read, so fetched dependencies, build output and
packaged copies stay out of the index.

Each file is split into paragraphs (blank-line separated, fenced code kept whole, headings tracked as
the section path), small paragraphs are merged with their neighbours in the same section, and each
chunk is embedded locally with sentence-transformers. Re-running is incremental: unchanged files are
skipped without loading the model, changed files are re-chunked, and files that disappeared from an
indexed directory are dropped from the index.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import redis
from redis.commands.search.field import TagField, TextField, VectorField

try:  # redis-py >= 6
    from redis.commands.search.index_definition import IndexDefinition, IndexType
except ImportError:  # redis-py 5
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType

from memory_redis import CHUNK_PREFIX, FILES_KEY, INDEX_NAME, META_KEY, MODEL_NAME, REDIS_URL, connect

SOURCES_FILE = Path(__file__).with_name("sources.txt")

# all-MiniLM-L6-v2 reads at most 256 word pieces (about 1000 characters of English); longer text is
# silently truncated, so no chunk may exceed MAX_CHARS. Paragraphs under MIN_CHARS carry too little
# meaning on their own and are merged with a neighbour from the same section.
MAX_CHARS = 1000
MIN_CHARS = 300
CHUNKER_VERSION = f"1:{MAX_CHARS}:{MIN_CHARS}"  # part of each file's digest: a chunker change re-indexes

# Used only outside git checkouts, where .gitignore cannot say what to skip
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "build", "dist", "external", ".tools"}

HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^(`{3,}|~{3,})")
THEMATIC_BREAK = re.compile(r"^([-*_])(\s*\1){2,}$")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
FRONT_MATTER_KEEP = re.compile(r"^\s*(name|title|description|type):")  # session ids and timestamps only add noise


@dataclass
class Chunk:
    heading: str  # section path, e.g. "Bodhidharma — GDD > Combat > Blows"
    text: str
    start_line: int  # 1-based, inclusive
    end_line: int


def paragraphs(text: str) -> list[tuple[str, list[tuple[int, str]]]]:
    """Split a Markdown document into (section path, [(line number, line), ...]) paragraphs."""
    lines = list(enumerate(text.splitlines(), 1))
    if lines and lines[0][1].strip() == "---":  # YAML front matter: keep the descriptive fields as a paragraph
        end = next((i for i in range(1, min(len(lines), 50)) if lines[i][1].strip() == "---"), 0)
        if end:
            lines = [ln for ln in lines[1:end] if FRONT_MATTER_KEEP.match(ln[1])] + [(end + 1, "")] + lines[end + 1:]

    out: list[tuple[str, list[tuple[int, str]]]] = []
    headings: list[str] = []
    para: list[tuple[int, str]] = []
    fence = ""

    def flush() -> None:
        if para:
            out.append((" > ".join(headings), para.copy()))
            para.clear()

    for no, line in lines:
        stripped = line.strip()
        if fence:  # inside fenced code: blank lines and '#' lines belong to the code
            para.append((no, line.rstrip()))
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = ""
            continue
        if m := FENCE.match(stripped):
            fence = m.group(1)
            para.append((no, line.rstrip()))
            continue
        if m := HEADING.match(line):
            flush()
            headings[len(m.group(1)) - 1:] = [m.group(2)]
            continue
        if not stripped or THEMATIC_BREAK.match(stripped):
            flush()
            continue
        para.append((no, line.rstrip()))
    flush()
    return out


def split_long(lines: list[tuple[int, str]]) -> list[tuple[int, int, str]]:
    """Cut one paragraph into (first line, last line, text) pieces of at most MAX_CHARS: on line
    boundaries first, then on sentence ends, then hard."""
    pieces: list[tuple[int, str]] = []
    for no, line in lines:
        if len(line) <= MAX_CHARS:
            pieces.append((no, line))
            continue
        for sentence in SENTENCE_END.split(line):
            pieces.extend((no, sentence[i:i + MAX_CHARS]) for i in range(0, len(sentence), MAX_CHARS))

    out: list[tuple[int, int, str]] = []
    buf: list[str] = []
    start = end = size = 0
    for no, text in pieces:
        if buf and size + len(text) > MAX_CHARS:
            out.append((start, end, "\n".join(buf)))
            buf, size = [], 0
        if not buf:
            start = no
        buf.append(text)
        size += len(text) + 1
        end = no
    if buf:
        out.append((start, end, "\n".join(buf)))
    return out


def chunk_markdown(text: str) -> list[Chunk]:
    chunks: list[Chunk] = []
    for heading, lines in paragraphs(text):
        for start, end, body in split_long(lines):
            last = chunks[-1] if chunks else None
            if (last and last.heading == heading
                    and min(len(last.text), len(body)) < MIN_CHARS
                    and len(last.text) + 2 + len(body) <= MAX_CHARS):
                last.text += "\n\n" + body
                last.end_line = end
            else:
                chunks.append(Chunk(heading, body, start, end))
    return chunks


def embedding_text(path: Path, chunk: Chunk) -> str:
    """What gets embedded: the file name and section path give a paragraph the context it lacks alone."""
    title = path.stem.replace("_", " ").replace("-", " ")
    return f"{title}: {chunk.heading}\n\n{chunk.text}" if chunk.heading else f"{title}\n\n{chunk.text}"


def markdown_files(root: Path) -> list[Path]:
    """Every .md file under root; inside a git checkout only those git does not ignore."""
    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
            capture_output=True, check=True).stdout.decode("utf-8")
        return sorted(p for p in (root / rel for rel in listed.split("\0") if rel) if p.is_file())
    except (OSError, subprocess.CalledProcessError):  # not a git checkout, or no git
        return sorted(p for p in root.rglob("*.md")
                      if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts[:-1]))


def read_sources(path: Path) -> list[Path]:
    if not path.is_file():
        sys.exit(f"No directories given and no {path.name} next to the script.")
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [Path(os.path.expandvars(os.path.expanduser(line))) for line in lines if line and not line.startswith("#")]


def file_id(path: Path) -> str:
    return hashlib.sha1(os.path.normcase(str(path)).encode("utf-8")).hexdigest()[:16]


def is_under(path: str, root: Path) -> bool:
    root_str = os.path.normcase(str(root)).rstrip("\\/") + os.sep
    return os.path.normcase(path).startswith(root_str)


def index_exists(r: redis.Redis) -> bool:
    try:
        r.ft(INDEX_NAME).info()
        return True
    except redis.ResponseError as e:
        if "unknown command" in str(e).lower():
            sys.exit(f"{REDIS_URL} has no search module (FT.* commands): run redis/redis-stack, see docker-compose.yml.")
        return False


def create_index(r: redis.Redis, dim: int) -> None:
    r.ft(INDEX_NAME).create_index(
        [
            TextField("content"),
            TextField("heading"),
            TagField("source", separator="|"),
            VectorField("embedding", "HNSW", {"TYPE": "FLOAT32", "DIM": dim, "DISTANCE_METRIC": "COSINE"}),
        ],
        definition=IndexDefinition(prefix=[CHUNK_PREFIX], index_type=IndexType.HASH),
    )
    r.hset(META_KEY, mapping={"model": MODEL_NAME, "dim": dim})
    print(f"Created index '{INDEX_NAME}' ({dim}-dim vectors, cosine, HNSW)")


def drop_chunks(r: redis.Redis, path: Path | str) -> None:
    keys = list(r.scan_iter(match=f"{CHUNK_PREFIX}{file_id(Path(path))}:*", count=1000))
    if keys:
        r.delete(*keys)


def main() -> int:
    parser = argparse.ArgumentParser(description="Index Markdown files into the Redis project-memory vector index.")
    parser.add_argument("dirs", nargs="*", type=Path, help="directories to index (default: the ones in sources.txt)")
    parser.add_argument("--reset", action="store_true",
                        help="drop the index and every chunk first (needed after changing EMBEDDING_MODEL)")
    args = parser.parse_args()

    roots = [d.expanduser().resolve() for d in (args.dirs or read_sources(SOURCES_FILE))]

    try:
        r = connect()
    except RuntimeError as e:
        sys.exit(str(e))

    if args.reset and index_exists(r):
        r.ft(INDEX_NAME).dropindex(delete_documents=True)
        print(f"Dropped index '{INDEX_NAME}' and its chunks")
    if args.reset or not index_exists(r):
        r.delete(FILES_KEY, META_KEY)  # bookkeeping for an index that is gone would skip every file

    built_with = r.hget(META_KEY, "model")
    if built_with and built_with != MODEL_NAME:
        sys.exit(f"Index '{INDEX_NAME}' was built with {built_with}, not {MODEL_NAME}: re-run with --reset.")

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
            digest = hashlib.sha256(CHUNKER_VERSION.encode() + b"\0" + data).hexdigest()
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
        from sentence_transformers import SentenceTransformer  # slow import (torch): only when needed

        model = SentenceTransformer(MODEL_NAME)
        if not index_exists(r):
            dimension = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
            create_index(r, dimension())  # renamed in sentence-transformers 6

        for path, text, digest in todo:
            chunks = chunk_markdown(text)
            vectors = model.encode([embedding_text(path, c) for c in chunks], batch_size=64,
                                   normalize_embeddings=True, show_progress_bar=False).astype(np.float32)
            fid = file_id(path)
            drop_chunks(r, path)
            pipe = r.pipeline(transaction=True)
            for n, (chunk, vector) in enumerate(zip(chunks, vectors)):
                pipe.hset(f"{CHUNK_PREFIX}{fid}:{n:04d}", mapping={
                    "content": chunk.text,
                    "heading": chunk.heading,
                    "source": str(path),
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    # identical chunks in several worktrees share this, so a search shows them once
                    "content_sha": hashlib.sha1(embedding_text(path, chunk).encode("utf-8")).hexdigest(),
                    "embedding": vector.tobytes(),
                })
            pipe.hset(FILES_KEY, str(path), digest)
            pipe.execute()
            total_chunks += len(chunks)
            print(f"  indexed  {path} ({len(chunks)} chunks)")

    in_index = r.ft(INDEX_NAME).info()["num_docs"] if index_exists(r) else 0
    print(f"{len(seen)} files in {len(scanned_roots)} directories: {len(todo)} indexed ({total_chunks} chunks), "
          f"{len(seen) - len(todo)} unchanged, {len(removed)} removed. Index '{INDEX_NAME}' holds {in_index} chunks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
