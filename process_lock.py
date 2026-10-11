"""Small OS-backed locks; a crashed process automatically releases its lock."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile


@contextmanager
def process_lock(name: str, directory: Path | None = None):
    directory = directory or Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir())) / "project-memory" / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(name.encode()).hexdigest() + ".lock")
    with path.open("a+b") as stream:
        if not path.stat().st_size:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Another project-memory process holds this lock; retry later.") from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
