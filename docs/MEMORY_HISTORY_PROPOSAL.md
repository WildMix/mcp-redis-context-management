# Memory history

Implemented design, updated 2026-11-04. Agents explicitly identify semantic
relationships; the service validates and stores revisions/conflicts. This replaces
the automatic NLI comparison introduced on 2026-10-29. There are no corpus snapshot
objects, agent-managed commits, semantic clustering, or additional databases.

## Agent interface

Agents save useful information using `save_to_project_memory(title, content, ...)`.
An ordinary save creates an independent memory; exact duplicate current content in
that context/project is a no-op. Similarity and words such as "supersedes" in the
body never decide relationships automatically.

For an update or disagreement, first read the complete current note with
`get_memory_history(memory_id, limit=1, context=..., project=...)`. A note the agent
just saved is already known in full. Then pass three optional save arguments:

```python
save_to_project_memory(
    title="Consumer owns change detection",
    content="The accepted design is ... Its reason and evidence are ...",
    context="projects", project="application",
    relationship="supersedes",
    related_memory_id="n-<ID returned by search/save>",
    expected_revision=1,  # the target revision that was read
)
```

- `supersedes`: a self-contained replacement/extension advances the same memory
  ID and archives the old note. Preserve still-valid knowledge, explain the changed
  fact/decision, and cite its evidence. A prior workaround can remain technically
  valid while the accepted architectural choice changes.
- `contradicts`: an unresolved disagreement creates a separate current note and
  links both memories. It does not choose which statement is correct.
- Any session/agent may target a note created by another session/agent in the same
  writable context/project. Original authorship/text remain on each revision.
- Both relationships require the target's observed revision. If another writer
  changed it, the request fails with the current revision/text; the agent must
  reassess rather than blindly increment the revision and retry.
- Agent saves cannot supersede curated documents. They may link a contradictory
  note; the source document remains current until its normal publication changes.

The shell client supports the same workflow with `memory history ID -c CONTEXT
-p PROJECT` and `memory save TITLE --relationship supersedes --related-memory-id
ID --expected-revision 1 -c CONTEXT -p PROJECT` (body from stdin).

Save durable technical knowledge, not review progress: PR approval/merge status,
build results, pending tasks and refreshed timestamps alone do not justify a save
or revision. PR/commit references support technical conclusions. Retain necessary
scope/evidence qualifications without turning a note into a review handoff.

Search uses the existing embeddings, initially retrieves 30 chunks, and groups
matches into three logical memories. Candidate expansion is bounded at 960 chunks.
Current and historical records participate in the same search. Historical results
are explicitly labeled and include their current replacement even if that version
was outside the similarity results. Retired sources have no current replacement.

Results expose a stable memory ID, revision, status, older-version count and
unresolved conflict links. `get_memory_history(memory_id, context=..., project=...)`
is a read-only tool. It returns five revisions by default (maximum ten),
with `before_revision` pagination. Ordinary document revisions are reconstructed
from their chunks; output beyond 12,000 characters per document is explicitly
truncated. Notes retain their exact original text and existing 6,000-character cap.
Context/project isolation and configured read-only permissions apply as before.

## Relationship validation

The local all-MiniLM-L6-v2 model still embeds saves and searches. The server no
longer runs an NLI classifier, applies an acronym/environment equality test, or
fits whole-note comparisons into a 512-token model window. Agents already have
the discussion and evidence needed to choose the meaning of a relationship.
Provision embedding weights before offline startup:

```sh
.venv/bin/python scripts/provision-models.py
```

Validation is deterministic: accepted relation name, complete target/revision
arguments, current target, same context/project, write permissions and optimistic
concurrency. No author ownership restriction applies. This does not prove an
agent's semantic judgment correct; original texts and evidence remain available.

Exact retries of a supersession are idempotent while the resulting revision is
still current. If the memory advanced again, a stale retry is rejected. An exact
contradiction retry is a no-op when its conflict link already exists and the target
revision still matches. A new relationship is never silently dropped by duplicate
detection: an existing identical note without the requested contradiction yields
an error identifying that memory. This API does not merge two existing families.
Conflicts are retained across later revisions; a new revision does not implicitly
settle every earlier disagreement. The old `MEMORY_RELATIONS` switch is obsolete;
explicit relationships are always available subject to context write permissions.

## Redis representation

The existing note and document hash prefixes and HNSW index remain. Each chunk
carries additional fields:

| Field | Meaning |
| --- | --- |
| `memory_id` | Stable logical note, curated topic, or ordinary Markdown file identity |
| `revision` | Version number starting at one |
| `state` | `current`, `historical`, or `retired` |
| `part` | Chunk order inside the version |
| `relationship` | Agent-declared `supersedes`, `contradicts`, or empty |
| `related_memory_id` | Target memory ID in the same context/project |
| `related_revision` | Target revision the agent observed |
| `conflicts` | Other memory IDs with unresolved conflicting evidence |
| `content_digest` | Whole-note content hash for exact duplicate detection |

Each saved note revision retains its unique `note_id`, so the original chunk keys
and exact-note reconstruction continue to work. A logical memory's current
revision is found through indexed metadata; no separate head-pointer database is
needed. Current text is appended as new hashes; only lifecycle/link metadata on
older hashes changes. No corpus copy or Redis Stream is required.

Curated publication retains its existing canonical document keys. Before a changed
or removed topic is replaced, its previous hashes are copied under
`<namespace>:doc:history:<archive-id>:<part>`. Ordinary Markdown imports preserve
file revisions the same way. Unchanged documents do not create artificial versions.
Publication deletion retires the source while retaining its searchable history.
There is no new purge or retention policy in this change.

Saving watches the store's mutation revision before reading the target/duplicates.
Concurrent changes cause validation to retry; a changed target rejects stale intent.
New chunks, retirement flags,
conflict links and the store revision are committed in one transaction. Exact
retry deduplication is scoped to current content; the API does not claim a general
cross-system exactly-once protocol. Curated/directory publication follows existing
writer coordination; stop writers during migration and maintenance.

Rebuilds preserve revision metadata and all historical text. Curated bundles
include the updated runtime; `--include-notes` explicitly opts into exporting saved
notes and historical documents. Restore validates historical chunk counts too.
Existing RDB/AOF persistence remains in use; semantic history does not replace
backup/restore.

## Existing installations

Back up Redis and stop writers, then initialize metadata on each selected instance:

```sh
.venv/bin/python memory_history.py --redis-url redis://127.0.0.1:6380/0
.venv/bin/python memory_history.py --redis-url redis://127.0.0.1:6381/0
```

Use your configured destinations. The migration adds index fields and version-one
metadata in place, preserving keys, text, embeddings and curated manifests. It is
repeatable. Existing notes are not retrospectively reconciled and missing older
versions cannot be recovered. New stores create the extended schema automatically.

After provisioning embeddings and migrating, restart the existing MCP process. On this
Mac the LaunchAgent is `local.project-memory.mcp`; `/health` reports
`memory_history: 1`, `memory_relationships: "explicit"` and `relation_model: "off"`.
Already versioned installations need no Redis migration for explicit relationships:
the new provenance fields are optional on old hashes. Existing unrelated notes are
not automatically regrouped. Restart the server and reconnect clients to discover
the new optional save arguments. Existing plain-save clients remain compatible.

## Verification

Tests cover exact retry deduplication, stale concurrent updates, failed writes, multiple
chunks, historical matches with current replacements, conflict links, project
isolation, paginated history, legacy metadata initialization, document retirement,
rebuilds, cross-author relationships, explicit intent without semantic guessing,
and existing import/export/HTTP behavior. Real-model tests use cached embeddings.

`tests/run_agent_relationships.py` is an opt-in real Codex-session evaluation with
synthetic technical evidence. It requires an empty disposable Redis container with
the label `project-memory-test=explicit-relationships`, a loopback port other than
6379/6380/6381 and no persistent mounts. It starts a separate MCP process/context;
test agents ignore normal Codex MCP configuration and have shell/apps/web disabled.
It checks supersession and unresolved contradiction both within one resumed session
and through a fresh session reading another session's notes, plus a status-only
no-save case and a fresh reader's interpretation. JSONL transcripts and persisted
relationship assertions are retained in the selected output directory. These cases
exercise the workflow, not a general guarantee of semantic accuracy.

```sh
docker run -d --name project-memory-relationships-test \
  --label project-memory-test=explicit-relationships \
  -p 127.0.0.1:6392:6379 --tmpfs /data:rw,size=128m \
  -e 'REDIS_ARGS=--save "" --appendonly no' \
  redis/redis-stack@sha256:5d5154123426693540ea6c1d9e638eae2bf2024879c839db6fcfdd7717e817c7
.venv/bin/python tests/run_agent_relationships.py \
  --redis-url redis://127.0.0.1:6392/0 --container project-memory-relationships-test \
  --output .migration/agent-relationships/run
docker rm -f project-memory-relationships-test
```
