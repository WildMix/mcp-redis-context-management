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

FIELDS = ("kind", "project", "content", "heading", "source", "start_line", "end_line",
          "content_sha", "note_id", "parts", "join_mode", "created", "updated_at",
          "source_modified_at", "agent", "branch", "worktree", "topic_id", "document_id",
          "domain", "memory_id", "revision", "state", "conflicts", "content_digest", "part",
          "relationship", "related_memory_id", "related_revision")


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


def validate_relationship(relationship, related_memory_id, expected_revision):
    if relationship not in ("", "supersedes", "contradicts"):
        raise ValueError("relationship must be supersedes or contradicts, or omitted for an independent note")
    if type(expected_revision) is not int:
        raise ValueError("expected_revision must be an integer")
    if not relationship:
        if related_memory_id or expected_revision:
            raise ValueError("related_memory_id and expected_revision require a relationship")
    elif not re.fullmatch(r"[nd]-[A-Za-z0-9._-]{1,128}", related_memory_id) or expected_revision < 1:
        raise ValueError("A relationship requires related_memory_id and a positive expected_revision from the note you read")


def select_relation(r, namespace, project, content, relationship, related_memory_id, expected_revision):
    """Validate explicit intent inside WATCH; similarity never chooses a relationship."""
    target = None
    if relationship:
        target = current(r, namespace, project, related_memory_id)
        if not target:
            raise ValueError("No current target memory in this context/project; search and read it before saving")
        if relationship == "supersedes" and target.kind != "note":
            raise ValueError("Agent notes cannot supersede curated documents; edit their source or record a contradiction")
        # A retry after a lost acknowledgement must not create another revision.
        if (relationship == "supersedes" and int(target.revision) == expected_revision + 1
                and field(target, "relationship") == relationship
                and field(target, "related_revision") == str(expected_revision)
                and field(target, "content_digest") == digest(content)):
            return "duplicate", target, []
        if int(target.revision) != expected_revision:
            raise ValueError(f"Stale memory {related_memory_id}: expected revision {expected_revision}, "
                             f"current revision is {target.revision}. Read and reassess before saving.\n"
                             f"{target.heading}\n{bounded_text(text(r, namespace, target))}")
        if relationship == "supersedes":
            # Scope the duplicate check to the explicit target, never another memory.
            if text(r, namespace, target).strip() == content.strip():
                return "duplicate", target, []
            return "update", target, []
        if text(r, namespace, target).strip() == content.strip():
            raise ValueError("A memory cannot contradict identical content; no relationship was saved")
    exact = records(r, namespace, f"@project:{{{store.tag(project)}}} @state:{{current}} "
                    f"@content_digest:{{{digest(content)}}}", limit=1)
    if exact:
        existing = exact[0]
        if relationship and related_memory_id not in json.loads(field(existing, "conflicts", "[]")):
            raise ValueError(f"This content already exists as {identity(existing)} without the requested relationship. "
                             "No changes made; read both memories and reassess the note and its evidence.")
        return "duplicate", existing, []
    return "new", None, [target] if target else []


def revision_keys(r, namespace, doc):
    if doc.kind == "note":
        return note_keys(namespace, doc)
    chunks = records(r, namespace, scope(doc.project, doc.memory_id) +
                     f" @revision:[{doc.revision} {doc.revision}]", limit=1001)
    if len(chunks) > 1000:
        raise ValueError("Target document has too many chunks to link atomically")
    return [c.id for c in chunks]


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


def commit_note(r, namespace, title, content, project, chunks, vectors, metadata,
                relationship="", related_memory_id="", expected_revision=0):
    """Atomically apply an agent-declared relationship, including cross-session saves."""
    validate_relationship(relationship, related_memory_id, expected_revision)
    for _ in range(8):
        with r.pipeline(transaction=True) as pipe:
            try:
                pipe.watch(store.META_KEY)
                if store.namespace_of(pipe) != namespace:
                    raise RuntimeError("Memory rebuilt during save; retry")
                operation, old, conflicts = select_relation(r, namespace, project, content,
                                                            relationship, related_memory_id, expected_revision)
                if operation == "duplicate":
                    # Even a no-op must check the watched revision before returning success.
                    pipe.multi()
                    pipe.execute()
                    return (f'Already remembered as memory {identity(old)} in project "{project}", '
                            f'revision {old.revision}; no duplicate saved.')
                note_id = secrets.token_hex(16)
                memory_id = identity(old) if old else "n-" + note_id
                revision = int(field(old, "revision", "1")) + 1 if old else 1
                conflict_ids = {identity(doc) for doc in conflicts}
                if old:
                    conflict_ids.update(json.loads(field(old, "conflicts", "[]")))
                conflict_ids = sorted(conflict_ids)
                # Read every linked revision's keys before entering MULTI.
                linked = [(doc, revision_keys(r, namespace, doc)) for doc in conflicts]
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
                        "relationship": relationship, "related_memory_id": related_memory_id,
                        "related_revision": expected_revision if relationship else "",
                        "embedding": vector.tobytes()})
                for doc, keys in linked:
                    ids = sorted(set(json.loads(field(doc, "conflicts", "[]"))) | {memory_id})
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
                        f"source: {field(doc, 'source', 'unknown')}; author: {field(doc, 'agent', 'unknown')}\n"
                        f"{relationship_description(doc)}{field(doc, 'heading')}\n\n"
                        f"{bounded_text(text(r, namespace, doc))}")
        query = scope(project, memory_id) + f" @revision:[-inf ({revision}]"
    if not versions:
        return f'No matching history for memory {memory_id} in project "{project}".'
    return (f'History of {memory_id} in project "{project}". '
            f'For older entries pass before_revision={revision}.\n\n' + "\n\n---\n\n".join(versions))


def relationship_description(doc):
    details = ""
    if field(doc, "relationship"):
        details += (f"Relationship: {doc.relationship} {doc.related_memory_id}, "
                    f"observed revision {doc.related_revision}.\n")
    if json.loads(field(doc, "conflicts", "[]")):
        details += f"Unresolved conflicts: {', '.join(json.loads(doc.conflicts))}.\n"
    return details


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
