# Memory history

Implemented design, 2026-10-29. The agreed scope is version history behind the
existing save/search interface. There are no corpus snapshot objects, agent-managed
commits, semantic clustering, or additional databases.

## Agent interface

Agents save useful information using `save_to_project_memory(title, content, ...)`.
The service checks for exact duplicates, compares relevant current memories, and
retains old versions when it accepts an update. Include scope, dates and evidence
in ordinary prose when known; no version identifiers or special update syntax are
required from the agent. Agents still decide what knowledge is worth retaining.

Search uses the existing embeddings, initially retrieves 30 chunks, and groups
matches into three logical memories. Candidate expansion is bounded at 960 chunks.
Current and historical records participate in the same search. Historical results
are explicitly labeled and include their current replacement even if that version
was outside the similarity results. Retired sources have no current replacement.

Results expose a stable memory ID, revision, status, older-version count and
unresolved conflict links. `get_memory_history(memory_id, context=..., project=...)`
is an optional read-only tool. It returns five revisions by default (maximum ten),
with `before_revision` pagination. Ordinary document revisions are reconstructed
from their chunks; output beyond 12,000 characters per document is explicitly
truncated. Notes retain their exact original text and existing 6,000-character cap.
Context/project isolation and configured read-only permissions apply as before.

## Local semantic comparison

The existing all-MiniLM-L6-v2 embedding model remains unchanged. A second local
model, `cross-encoder/nli-deberta-v3-base`, compares retrieved statements. It is pinned
at `6c749ce3425cd33b46d187e45b92bbf96ee12ec7`, loaded once in the shared MCP process,
and warmed at HTTP startup. Neither model sends corpus text to an external service.
Provision weights before offline startup:

```sh
.venv/bin/python scripts/provision-models.py
```

The save path considers at most eight distinct related current memories, using up
to eight incoming chunk vectors and a cosine floor of 0.55. These are bounded
retrieval heuristics, not guarantees of exhaustive matching. Comparison evaluates
entailment/contradiction in both directions and refuses to truncate oversized
inputs. Classification thresholds and conservative checks are in
`memory_relations.py`; they are heuristics, not calibrated truth probabilities.

- Exact duplicate current content is a no-op, including retries after a lost reply.
- An unambiguous paraphrase or extension becomes the next revision when the
  classifier judges that it preserves the old information. Both original texts
  remain stored; equivalent wording does not crowd search as a separate memory.
- Contradictory replacements require a single-claim old note and an explicit,
  non-speculative transition in ordinary prose, such as "now" or "no longer".
- Multiple competing replacement candidates, ordinary contradictions, and
  disagreements with curated reference documents are retained with conflict links.
  Agent saves do not overwrite curated reference documents.
- Uncertain comparisons, long inputs and detected differences in environment,
  component acronym or older dated evidence remain additive. Semantically similar
  text is not discarded merely because a model calls it equivalent.

The classifier is trained for English. Arbitrary scope differences, temporal
meaning and multilingual text are not reliably solved by NLI. False negatives
can create additional memories; false positives remain possible, but superseded
text stays available and source evidence remains important. No semantic accuracy
beyond the exercised cases is claimed. `MEMORY_RELATIONS=off` disables inference;
exact duplicate checks and versioned document publication still work, while note
saves remain additive. Missing enabled model weights fail explicitly.

## Redis representation

The existing note and document hash prefixes and HNSW index remain. Each chunk
carries additional fields:

| Field | Meaning |
| --- | --- |
| `memory_id` | Stable logical note, curated topic, or ordinary Markdown file identity |
| `revision` | Version number starting at one |
| `state` | `current`, `historical`, or `retired` |
| `part` | Chunk order inside the version |
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

Save comparison watches the store's mutation revision before reading candidates.
Concurrent changes cause reconciliation to retry. New chunks, retirement flags,
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

After provisioning models and migrating, restart the existing MCP process. On this
Mac the LaunchAgent is `local.project-memory.mcp`; `/health` reports
`memory_history: 1` and the local comparison model. Client sessions may need to
reconnect to discover the optional history tool; save/search signatures are unchanged.

## Verification

Tests cover exact retry deduplication, concurrent updates, failed writes, multiple
chunks, historical matches with current replacements, conflict links, project
isolation, paginated history, legacy metadata initialization, document retirement,
rebuilds, and existing import/export/HTTP behavior. Real-model tests include a
simple asserted update and distinct production/staging scope. Production rollout
also compares corpus text/vector checksums before and after initialization.
