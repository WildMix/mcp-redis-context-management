import contextlib
import io
import uuid
from unittest.mock import patch

import numpy as np

import memory_store as store
import mcp_server as server
import migrate_memory as migrate


class TinyModel:
    """Predictable local tokenizer/embeddings; exercises storage without downloading a model."""
    max_seq_length = 256

    def tokenizer(self, text, **kwargs):
        return {"input_ids": [0, *text.encode("utf-8"), 1]}

    def get_embedding_dimension(self):
        return 3

    def encode(self, texts, **kwargs):
        return np.array([[1.0, 0.0, 0.0] for _ in texts], dtype=np.float32)


@contextlib.contextmanager
def isolated_store(r, model=None):
    """Patch only this process to a unique namespace; never alter the configured memory."""
    prefix = "test_project_memory_" + uuid.uuid4().hex
    model = model or TinyModel()
    values = {"INDEX_NAME": prefix, "NOTE_PREFIX": prefix + ":note:", "DOC_PREFIX": prefix + ":doc:",
              "FILES_KEY": prefix + ":files", "META_KEY": prefix + ":meta"}
    with contextlib.ExitStack() as stack:
        for module in (store, server, migrate):
            stack.enter_context(patch.multiple(module, **{k: v for k, v in values.items() if hasattr(module, k)}))
        stack.enter_context(patch.object(server, "connect", return_value=r))
        stack.enter_context(patch.object(server, "embedding_model", return_value=model))
        stack.enter_context(patch.object(migrate, "load_model", return_value=model))
        stack.enter_context(patch.object(store, "PROJECT_OVERRIDE", ""))
        try:
            yield model
        finally:
            assert prefix.startswith("test_project_memory_") and len(prefix.rsplit("_", 1)[1]) == 32
            for index in r.execute_command("FT._LIST"):
                if index == prefix or index.startswith(prefix + ":"):
                    r.ft(index).dropindex(delete_documents=False)
            keys = list(r.scan_iter(match=prefix + ":*"))
            for start in range(0, len(keys), 256):
                r.unlink(*keys[start:start + 256])
            assert not list(r.scan_iter(match=prefix + ":*"))


def quiet_import(r, roots, project):
    with contextlib.redirect_stdout(io.StringIO()):
        migrate.import_dirs(r, roots, project)


def quiet_rebuild(r):
    with contextlib.redirect_stdout(io.StringIO()):
        migrate.rebuild(r)
