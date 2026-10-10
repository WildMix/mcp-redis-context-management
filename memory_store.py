"""Storage layer shared by mcp_server.py and migrate_memory.py: settings, the Redis connection, the
index schema, project names, and how Markdown is cut into embeddable chunks.

Two kinds of entries share one vector index, each tagged with its project:
- notes, saved by agents through the save_to_project_memory tool ({NOTE_PREFIX}{note id}:{n});
- docs, Markdown imported by migrate_memory.py ({DOC_PREFIX}{file id}:{n}).

When Docker runs inside a WSL distro, WSL stops that distro a few seconds after its last session
ends, and Redis stops with it. With REDIS_WSL_DISTRO set (e.g. "Ubuntu"), every process that uses
Redis holds the distro open for as long as it lives, and waits for Docker to bring the container
back up (restart: unless-stopped in docker-compose.yml).
"""
from __future__ import annotations

import functools
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import redis
from redis.commands.search.field import NumericField, TagField, TextField, VectorField

try:  # redis-py >= 6
    from redis.commands.search.index_definition import IndexDefinition, IndexType
except ImportError:  # redis-py 5
    from redis.commands.search.indexDefinition import IndexDefinition, IndexType

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
INDEX_NAME = os.environ.get("MEMORY_INDEX", "project_memory")
MODEL_NAME = os.environ.get("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
WSL_DISTRO = os.environ.get("REDIS_WSL_DISTRO", "")
PROJECT_OVERRIDE = os.environ.get("MEMORY_PROJECT", "")

NOTE_PREFIX = f"{INDEX_NAME}:note:"  # one hash per chunk of a saved note
DOC_PREFIX = f"{INDEX_NAME}:doc:"    # one hash per chunk of an imported Markdown file
FILES_KEY = f"{INDEX_NAME}:files"    # imported file path -> digest of the indexed content
META_KEY = f"{INDEX_NAME}:meta"      # model and dimension the index was built with

HERE = Path(__file__).resolve().parent


# --- Redis -------------------------------------------------------------------------------------

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


def index_exists(r: redis.Redis) -> bool:
    try:
        r.ft(INDEX_NAME).info()
        return True
    except redis.ResponseError as e:
        if "unknown command" in str(e).lower():
            raise RuntimeError(f"{REDIS_URL} has no search module (FT.* commands): run redis/redis-stack, "
                               f"see docker-compose.yml.") from e
        return False


def create_index(r: redis.Redis, dim: int) -> None:
    r.ft(INDEX_NAME).create_index(
        [
            TextField("content"),
            TextField("heading"),
            TagField("project"),
            TagField("kind"),  # note | doc
            TagField("source", separator="|"),
            NumericField("created", sortable=True),
            VectorField("embedding", "HNSW", {"TYPE": "FLOAT32", "DIM": dim, "DISTANCE_METRIC": "COSINE"}),
        ],
        definition=IndexDefinition(prefix=[NOTE_PREFIX, DOC_PREFIX], index_type=IndexType.HASH),
    )
    r.hset(META_KEY, mapping={"model": MODEL_NAME, "dim": dim})


def check_model(r: redis.Redis) -> None:
    built_with = r.hget(META_KEY, "model")
    if built_with and built_with != MODEL_NAME:
        raise RuntimeError(f"Index '{INDEX_NAME}' was built with {built_with}, not {MODEL_NAME}: set EMBEDDING_MODEL "
                           f"to match, or re-embed everything with `migrate_memory.py --rebuild`.")


def tag(value: str) -> str:
    """value escaped for a TAG filter, as in @project:{...}"""
    return re.sub(r"(\W)", r"\\\1", value)


# --- Embeddings --------------------------------------------------------------------------------

def load_model():
    """The sentence-transformers model; imported here because torch takes seconds to import."""
    from sentence_transformers import SentenceTransformer

    try:  # from the local cache, without asking the Hugging Face Hub for updates
        return SentenceTransformer(MODEL_NAME, local_files_only=True)
    except Exception:  # not downloaded yet
        return SentenceTransformer(MODEL_NAME)


def model_dimension(model) -> int:
    dimension = getattr(model, "get_embedding_dimension", None) or model.get_sentence_embedding_dimension
    return dimension()  # renamed in sentence-transformers 6


def encode(model, texts: list[str]) -> np.ndarray:
    return model.encode(texts, batch_size=64, normalize_embeddings=True, show_progress_bar=False).astype(np.float32)


def embedding_text(kind: str, source: str, heading: str, content: str) -> str:
    """What gets embedded: a note's title, or a doc's file name and section path, give a paragraph the
    context it lacks alone. Built from stored fields only, so --rebuild can re-embed without the files."""
    if kind == "note":  # heading holds the note's title
        return f"{heading}\n\n{content}"
    title = Path(source).stem.replace("_", " ").replace("-", " ")
    return f"{title}: {heading}\n\n{content}" if heading else f"{title}\n\n{content}"


# --- Projects ----------------------------------------------------------------------------------

def git(directory: Path | str, *args: str) -> str:
    """Output of a git command run in directory; "" outside a repository or without git.
    stdin is closed: in the MCP server it is the protocol pipe, and a child sharing it can deadlock."""
    try:
        return subprocess.run(["git", "-C", str(directory), *args], capture_output=True, encoding="utf-8",
                              stdin=subprocess.DEVNULL, check=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


@functools.lru_cache(maxsize=64)
def project_of(directory: str) -> str:
    """The memory a directory belongs to: its repository's main checkout folder name, so every worktree
    of a repository shares one memory; outside git, the folder's own name. MEMORY_PROJECT overrides it."""
    if PROJECT_OVERRIDE:
        return PROJECT_OVERRIDE.lower()
    common = git(directory, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common:  # <main checkout>/.git, or a bare repository
        common_dir = Path(common)
        name = common_dir.parent.name if common_dir.name == ".git" else common_dir.name.removesuffix(".git")
    else:
        name = Path(directory).resolve().name
    return name.lower()


# --- Chunking ----------------------------------------------------------------------------------

# all-MiniLM-L6-v2 reads at most 256 word pieces (about 1000 characters of English); longer text is
# silently truncated, so no chunk may exceed MAX_CHARS. Paragraphs under MIN_CHARS carry too little
# meaning on their own and are merged with a neighbour from the same section.
MAX_CHARS = 1000
MIN_CHARS = 300
CHUNKER_VERSION = f"1:{MAX_CHARS}:{MIN_CHARS}"  # part of each imported file's digest: a change re-imports

HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^(`{3,}|~{3,})")
THEMATIC_BREAK = re.compile(r"^([-*_])(\s*\1){2,}$")
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
FRONT_MATTER_KEEP = re.compile(r"^\s*(name|title|description|type):")  # session ids and timestamps only add noise


@dataclass
class Chunk:
    heading: str  # section path, e.g. "Design > Combat > Blows"
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
