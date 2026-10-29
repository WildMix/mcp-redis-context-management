"""Version metadata on existing chunk hashes; no snapshots or separate database.

    python memory_history.py --redis-url redis://127.0.0.1:6380/0

Backfill existing records in place while writers are stopped. Text, embeddings,
keys and curated publication manifests remain intact. New writes are versioned
automatically. Imported reference documents are never overwritten by agent saves.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
from types import SimpleNamespace

from redis.commands.search.query import Query
import redis

import memory_store as store
import memory_relations

FIELDS = ("kind", "project", "content", "heading", "source", "start_line", "end_line",
          "content_sha", "note_id", "parts", "join_mode", "created", "updated_at",
          "source_modified_at", "agent", "branch", "worktree", "topic_id", "document_id",
          "domain", "memory_id", "revision", "state", "conflicts", "content_digest", "part")


def field(doc, name, default=""):
    return getattr(doc, name, "") or default


def identity(doc):
    if field(doc, "memory_id"):
        return doc.memory_id
    if field(doc, "note_id"):
        return "n-" + doc.note_id
    source = field(doc, "topic_id") or field(doc, "source") or doc.id
    return "d-" + hashlib.sha256((field(doc, "project") + "\0" + source).encode()).hexdigest()[:32]


def digest(content):
    return hashlib.sha256(content.strip().encode()).hexdigest()


def records(r, namespace, query, limit=1000, sort=None):
    q = Query(query).return_fields(*FIELDS).paging(0, limit).dialect(2)
    if sort:
        q = q.sort_by(sort, asc=False)
    return r.ft(namespace.index).search(q).docs


def scope(project, memory_id):
    return f"@project:{{{store.tag(project)}}} @memory_id:{{{store.tag(memory_id)}}}"


def current(r, namespace, project, memory_id):
    docs = records(r, namespace, scope(project, memory_id) + " @state:{current}", limit=1)
    return docs[0] if docs else None


def text(r, namespace, doc):
    if field(doc, "kind") != "note":
        if not field(doc, "memory_id"):
            return doc.content
        chunks = records(r, namespace, scope(doc.project, doc.memory_id) +
                         f" @revision:[{doc.revision} {doc.revision}]", limit=1001)
        if len(chunks) > 1000:
            raise RuntimeError("Document revision is too large; read its cited source instead")
        chunks.sort(key=lambda c: (int(field(c, "part", "0")), c.id))
        pieces, previous = [], None
        for chunk in chunks:
            section = (field(chunk, "heading"), field(chunk, "start_line"), field(chunk, "end_line"))
            if pieces and section != previous:
                pieces.append("\n\n")
            pieces.append(chunk.content)
            previous = section
        return "".join(pieces)
    count = int(field(doc, "parts", "1"))
    if not 1 <= count <= 1000:
        raise RuntimeError("Invalid stored note chunk count")
    pipe = r.pipeline(transaction=False)
    for n in range(count):
        pipe.hget(f"{namespace.notes}{doc.note_id}:{n}", "content")
    pieces = pipe.execute()
    if any(piece is None for piece in pieces):
        raise RuntimeError("A stored note is incomplete; retry after checking its chunks")
    return ("" if field(doc, "join_mode") == "exact" else "\n\n").join(pieces)


def note_keys(namespace, doc):
    return [f"{namespace.notes}{doc.note_id}:{i}" for i in range(int(field(doc, "parts", "1")))]


def select_relation(r, namespace, project, title, content, vectors):
    """Called inside a WATCH transaction; the caller retries if any writer intervenes."""
    exact = records(r, namespace, f"@project:{{{store.tag(project)}}} @state:{{current}} "
                    f"@content_digest:{{{digest(content)}}}", limit=1)
    if exact:
        return "duplicate", exact[0], []
    candidates = {}
    # A bounded pool per chunk lets long notes find relevant memories too.
    for vector in vectors[:8]:
        q = (Query(f"(@project:{{{store.tag(project)}}} -@state:{{historical|retired}})"
                   "=>[KNN 30 @embedding $vector AS distance]")
             .return_fields(*FIELDS, "distance").sort_by("distance").paging(0, 30).dialect(2))
        for doc in r.ft(namespace.index).search(q, query_params={"vector": vector.tobytes()}).docs:
            if 1 - float(doc.distance) >= .55:
                key = identity(doc)
                if key not in candidates or float(doc.distance) < float(candidates[key].distance):
                    candidates[key] = doc
    replacements, conflicts = [], []
    for doc in sorted(candidates.values(), key=lambda d: float(d.distance))[:8]:
        old = text(r, namespace, doc)
        if old.strip() == content.strip():
            return "duplicate", doc, []
        relation = memory_relations.compare(old, content)
        # Keep semantic equivalents as revisions too: only exact text is discarded.
        # A false-positive equivalence must never irreversibly lose the original.
        if relation in ("updates", "extends", "equivalent") and field(doc, "kind") == "note":
            replacements.append(doc)
        elif relation in ("contradicts", "updates"):
            conflicts.append(doc)
    if len(replacements) == 1 and not conflicts:
        return "update", replacements[0], []
    return "new", None, [*conflicts, *replacements]


def document_revisions(r, namespace, incoming, old_keys):
    """Archive changed/removed published records; preserve curated document keys.

    Curated topics are logical records. Ordinary Markdown files are logical records,
    so re-chunking a file cannot splice together incompatible revisions.
    """
    old_groups, new_groups = {}, {}
    for key in old_keys:
        values = dict(zip(FIELDS, r.hmget(key, FIELDS)))
        doc = SimpleNamespace(id=key, **values)
        if not field(doc, "kind"):
            raise RuntimeError("A document disappeared during publication; retry")
        old_groups.setdefault(identity(doc), []).append((key, doc))
    for key, values in incoming:
        doc = SimpleNamespace(id=key, **values)
        new_groups.setdefault(identity(doc), []).append((key, values))
    archives = []
    for memory_id in old_groups.keys() | new_groups.keys():
        old = old_groups.get(memory_id, [])
        new = new_groups.get(memory_id, [])
        previous = max((int(field(doc, "revision", "1")) for _, doc in old), default=0)
        if not previous:
            found = records(r, namespace, scope(new[0][1]["project"], memory_id), limit=1, sort="revision")
            previous = int(found[0].revision) if found else 0
        same = bool(old and new) and [(doc.heading, doc.content) for _, doc in old] == [
            (values["heading"], values["content"]) for _, values in new]
        revision = previous if same else previous + 1
        for part, (key, values) in enumerate(new):
            values.update(memory_id=memory_id, revision=revision, state="current", part=part)
        if old and not same:
            archive_id = secrets.token_hex(16)
            for part, (key, doc) in enumerate(old):
                payload = r.execute_command("DUMP", key, NEVER_DECODE=True)
                archives.append((f"{namespace.docs}history:{archive_id}:{part}", payload,
                                 {"memory_id": memory_id, "revision": previous, "part": part,
                                  "state": "historical" if new else "retired"}))
    return archives


def queue_archives(pipe, archives):
    for key, payload, metadata in archives:
        pipe.restore(key, 0, payload)
        pipe.hset(key, mapping=metadata)


def commit_note(r, namespace, title, content, project, chunks, vectors, metadata):
    """Atomically reconcile + append. WATCH spans candidate reads and model inference."""
    for _ in range(8):
        with r.pipeline(transaction=True) as pipe:
            try:
                pipe.watch(store.META_KEY)
                if store.namespace_of(pipe) != namespace:
                    raise RuntimeError("Memory rebuilt during save; retry")
                operation, old, conflicts = select_relation(r, namespace, project, title, content, vectors)
                if operation == "duplicate":
                    # Even a no-op must check the watched revision before returning success.
                    pipe.multi()
                    pipe.execute()
                    return f'Already remembered as memory {identity(old)} in project "{project}"; no duplicate saved.'
                note_id = secrets.token_hex(16)
                memory_id = identity(old) if old else "n-" + note_id
                revision = int(field(old, "revision", "1")) + 1 if old else 1
                conflict_ids = {identity(doc) for doc in conflicts}
                if old:
                    conflict_ids.update(json.loads(field(old, "conflicts", "[]")))
                conflict_ids = sorted(conflict_ids)
                pipe.multi()
                if old:
                    for key in note_keys(namespace, old):
                        pipe.hset(key, mapping={"memory_id": memory_id,
                                               "revision": revision - 1, "state": "historical"})
                for n, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
                    pipe.hset(f"{namespace.notes}{note_id}:{n}", mapping={
                        **metadata, "kind": "note", "project": project, "heading": title,
                        "source": "note " + note_id, "note_id": note_id, "parts": len(chunks),
                        "part": n, "join_mode": "exact", "content": chunk,
                        "memory_id": memory_id, "revision": revision, "state": "current",
                        "conflicts": json.dumps(conflict_ids), "content_digest": digest(content),
                        "embedding": vector.tobytes()})
                for doc in conflicts:
                    ids = sorted(set(json.loads(field(doc, "conflicts", "[]"))) | {memory_id})
                    keys = note_keys(namespace, doc) if doc.kind == "note" else [doc.id]
                    for key in keys:
                        pipe.hset(key, "conflicts", json.dumps(ids))
                pipe.hincrby(store.META_KEY, "revision", 1)
                pipe.execute()
                suffix = f"; {len(conflict_ids)} unresolved related conflict(s) retained" if conflict_ids else ""
                return (f'Saved note {note_id} to the memory of project "{project}". '
                        f'Memory {memory_id}, revision {revision}{suffix}.')
            except redis.WatchError:
                continue
    raise RuntimeError("Memory changed repeatedly during save; retry")


def describe(r, namespace, project, doc):
    """Metadata and current replacement for a matched historical revision."""
    memory_id = identity(doc)
    revision = int(field(doc, "revision", "1"))
    state = field(doc, "state", "current")
    head = current(r, namespace, project, memory_id) if field(doc, "memory_id") else doc
    latest = int(field(head, "revision", str(revision))) if head else revision
    if revision < latest:
        state = "historical"
    status = f"memory {memory_id}; revision {revision} ({state}); {max(latest - 1, 0)} older version(s)"
    conflicts = json.loads(field(doc, "conflicts", "[]"))
    if conflicts:
        status += "; unresolved conflicts: " + ", ".join(conflicts)
    if state != "current":
        if head:
            status += (f"\nCurrent replacement (revision {head.revision}, {field(head, 'updated_at')}):\n"
                       f"{bounded_text(text(r, namespace, head))}\n--- matched historical evidence below ---")
        else:
            status += "\nThis source was retired; no current replacement is recorded."
    return status


def bounded_text(value, limit=12000):
    return value if len(value) <= limit else value[:limit] + "\n[Long document truncated; consult its cited source.]"


def history(r, namespace, project, memory_id, before_revision=0, limit=5):
    if not re.fullmatch(r"[nd]-[A-Za-z0-9._-]{1,128}", memory_id):
        raise ValueError("Use a memory_id returned by search_project_memory")
    if not 1 <= limit <= 10 or before_revision < 0:
        raise ValueError("limit must be 1-10 and before_revision must be nonnegative")
    query = scope(project, memory_id)
    if before_revision:
        query += f" @revision:[-inf ({before_revision}]"
    # Fetch one representative per revision, then reconstruct notes by their chunk IDs.
    versions = []
    while len(versions) < limit:
        docs = records(r, namespace, query, limit=1, sort="revision")
        if not docs:
            break
        doc = docs[0]
        revision = int(doc.revision)
        versions.append(f"Revision {revision} ({doc.state}), {field(doc, 'updated_at')}; "
                        f"source: {field(doc, 'source', 'unknown')}\n{field(doc, 'heading')}\n\n"
                        f"{bounded_text(text(r, namespace, doc))}")
        query = scope(project, memory_id) + f" @revision:[-inf ({revision}]"
    if not versions:
        return f'No matching history for memory {memory_id} in project "{project}".'
    return (f'History of {memory_id} in project "{project}". '
            f'For older entries pass before_revision={revision}.\n\n' + "\n\n---\n\n".join(versions))


def backfill(r):
    """Add version 1 metadata without changing a single byte of corpus text."""
    namespace = store.namespace_of(r)
    if not store.index_exists(r, namespace.index):
        return 0
    store.ensure_index(r, int(r.hget(store.META_KEY, "dim")))
    updates = []
    for prefix in (namespace.notes, namespace.docs):
        for key in r.scan_iter(match=prefix + "*", count=1000):
            values = r.hmget(key, FIELDS)
            doc = SimpleNamespace(id=key, **dict(zip(FIELDS, values)))
            if field(doc, "memory_id"):
                continue
            values = {"memory_id": identity(doc), "revision": 1, "state": "current"}
            if doc.kind == "doc":
                try:
                    values["part"] = int(key.rsplit(":", 1)[1])
                except ValueError:
                    values["part"] = 0
            if doc.kind == "note":
                values["content_digest"] = digest(text(r, namespace, doc))
            updates.append((key, values))
    def queue(pipe):
        for key, values in updates:
            pipe.hset(key, mapping=values)
    if updates:
        store.mutate(r, namespace, queue)
    return len(updates)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--redis-url", required=True)
    args = parser.parse_args()
    with redis.Redis.from_url(args.redis_url, decode_responses=True) as client:
        print(f"Initialized history metadata on {backfill(client)} chunks")
