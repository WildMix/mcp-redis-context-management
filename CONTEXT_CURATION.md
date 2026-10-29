# Curated context and isolated migration

A curated corpus is a maintained architectural reference. Preserve purpose, responsibilities,
data flow, extension points, relationships and useful run/test instructions. Keep machine paths,
method inventories, dependency pins and historical incidents outside its embedded text. An
unchanged Markdown checksum does not prove that the underlying design remains current.

## Markdown contract for retrieval

Use one H1 subject and uniquely named H2 topics. Each H2 must answer a useful question independently:
a responsibility, design rule, integration boundary or execution task. Keep a command's working
directory, prerequisites and effects in the same topic. Avoid generic paragraphs repeated across
repositories. Put shared workflows in a common guide.

The production chunker limits bodies to 1,000 characters and fits the complete embedding input to
the configured model's token limit (256 for the default MiniLM model). Filename and headings count
toward that limit. The curated validator rejects automatic topic splitting, preambles, nested
headings, duplicate topic identities and unclosed code fences. Revalidate after a model change.

Keep provenance, review dates and source hashes in separate metadata. Navigation indexes and
link-only lists need not enter the semantic index. Preserve original source evidence separately.

## Explicit selection and offline preview

The manifest is an explicit allowlist. Unknown Markdown is reported but never silently imported.
Use the same stable document identity after moving a corpus to another machine:

```json
{
  "schema": 1,
  "domain": "reference",
  "corpus_root": "reference",
  "documents": [
    {
      "document_id": "reference/library/overview",
      "file": "library.md",
      "project": "platform",
      "content_sha256": "<SHA-256 of the reviewed file bytes>"
    }
  ]
}
```

`corpus_root` resolves relative to the manifest. A domain is a user-defined corpus label (1–128 lowercase letters, digits, dots, underscores or
hyphens, starting with a letter or digit); projects can use
one shared scope for interconnected repositories or individual scopes for independent projects.
Document IDs start with their domain. Files must resolve inside the corpus root. An edited file
requires review and an updated hash. This is an editorial gate, not automated source-code review.
Search citations use the file path relative to that corpus root, such as `library.md`, without a
corpus-label prefix. Domain metadata remains separate for routing and import safeguards.

```sh
python scripts/preview_curated.py manifest.json --output preview.json
```

Preview uses the locally cached model and production tokenizer without Redis or downloads. It lists
exact topic content, embedding input, token counts and portable source references. Topic IDs combine
the document ID and heading slug; renaming a heading retires the old identity on the next publication.

## Publish to independent instances

```sh
docker compose -p project-memory-isolated -f docker-compose.isolated.yml up -d --wait
python import_curated.py reference-manifest.json --redis-url redis://127.0.0.1:6380/0
python import_curated.py projects-manifest.json --redis-url redis://127.0.0.1:6381/0
```

The supplied Compose file creates distinct containers and volumes on localhost ports 6380 and 6381.
Both use database 0. Numbered databases in one instance share its persistence/export boundary.
The existing `docker-compose.yml` remains available for the original single-instance workflow;
do not start it when intending to use the isolated setup.

The importer revalidates the manifest, binds the destination to its domain, embeds selected topics
and atomically replaces the complete curated selection and registry. Unchanged publications skip
embedding. Replaced/deleted topics remain as labeled historical records; moving the directory does not create duplicates.
Agent notes are preserved. The operation is a complete corpus publication, not a partial patch:
retain every desired document in the manifest. Avoid concurrent publishers.

An existing unclassified memory or different-domain destination is rejected. The ordinary directory
importer and its rebuild operation reject curated destinations. To change embedding models, validate
and publish to a fresh instance, then switch the route after testing. Automatic code-derived summary
refresh and scheduled curated publication are not implemented.

## Route the shared MCP server

Copy `memory_routes.example.json` to the ignored `memory_routes.local.json`, adjust its absolute
directory roots and project names, and start the server with `MEMORY_ROUTES` pointing to that file.
The Windows MCP sign-in worker reads this local file automatically. Restart the worker after edits.

Routes provide automatic selection by directory. Linked worktrees inherit their main checkout's route.
Without explicit context selection, conflicting or unmapped directories fail; the server never falls back to another domain or searches
other contexts. All targets share one loaded model but use separate Redis connections. Context names
and corpus labels have no built-in meaning. Each target independently sets `read_only` (default true)
and an optional fixed `project`. Set `read_only: false` to permit explicit agent notes. Routes do not
collect source files. The optional `domain` field verifies a curated database’s stored corpus label;
omit it for an ordinary database without curated metadata.

A colleague's single-context installation may use a single target with `default_target` set to that
target's name. It must have a fixed project. This intentionally searches the one shared corpus from
any agent working directory. Multi-target configurations cannot have a default.

The routed server verifies the embedding model and any configured corpus label before serving.
This protects against accidental target swaps; it is local application policy, not a hostile-client
security boundary. Services remain bound to loopback. Do not combine routing with `MEMORY_PROJECT`.

For Windows Redis sign-in startup, create ignored `redis-containers.local.json` containing an array
of the existing container names. The keeper starts those containers and holds WSL open. Without this
file it keeps its original single-container behavior. Do not enable the broad legacy sync job for
curated destinations. Review any future automation separately.

### Select context from any directory

The MCP tools accept optional `context` and `project` arguments. `context` is a target name from
`MEMORY_ROUTES`. Call `list_memory_contexts()` to discover targets, their read-only status and project
IDs, including stored projects with no checkout or route. With the example target names:

```text
search_project_memory(query="How does the platform work?", context="reference")
search_project_memory(query="Why was this design chosen?", context="projects", project="game")
save_to_project_memory(title="Design decision", content="...", context="projects", project="game")
```

Discovery also returns a `resources` map of project IDs to document source names, plus search guidance.
Source names are grouped and deduplicated from indexed document metadata, without retrieving text or
vectors; notes-only projects have an empty source list. Fixed targets expose only their fixed project's
sources. No extra index, corpus publication or filesystem scan is needed.

For a question spanning contexts or projects, the agent maps each topic to these resources, issues
separate focused queries in every relevant context/project, then combines the findings with their
scope and source citations. Recognize common spelling/acronym variants of resource names. Comparisons
need evidence for each side and may need further queries within the same shared project. A miss in
one scope does not imply that other scopes lack the information. This guidance appears in the MCP
handshake, tool descriptions and discovery response so both Claude and Codex can follow it. It guides
agent query planning; the server does not classify natural language or silently search other scopes.

Explicit selection takes precedence over `workdir` and MCP roots; neither is consulted or required.
This lets agents retrieve archived or not-yet-cloned projects from anywhere without pretending to
be in another checkout. Selection requires a project ID unless its target has a fixed project. IDs are normalized to lowercase and
validated; explicit saves in writable targets can create a new project. Those notes omit branch/worktree
provenance because the current checkout may be unrelated.

Unknown contexts, a project without a context, missing required project IDs and conflicting fixed
project IDs fail rather than guessing. Each request selects exactly one database and one project;
selectors preserve stored-domain/model validation. Directory routing remains the default when
neither selector is supplied. Restart clients after updating the server to discover the new tool
and arguments. The single-instance server without `MEMORY_ROUTES` still uses directory selection.

## Export and test a colleague bundle

```sh
python scripts/export_curated.py reference-manifest.json --redis-url redis://127.0.0.1:6380/0 --container project-memory-reference-redis --project platform --output exports/reference-v1
```

Add `--wsl-distro Ubuntu` if Docker is inside WSL. Output is a new directory plus its ZIP. Export
checks that Redis URL and container identify the same server, only database 0 has data, the full
instance contains only the allowed corpus records, and current stored topics match the selected Markdown. Saved agent notes and historical documents require explicit `--include-notes`;
`--project` optionally restricts the bundle to one project and must match every document and note.
The bundle includes the RDB, selected sources, portable manifest, runtime, checksums and restore/use
instructions. Model weights, Docker images and Python dependencies are downloaded on first setup.

`restore.py` refuses existing containers/volumes, verifies checksums, restores with AOF disabled,
enables AOF, recreates the container and checks data/index counts again. Test each export in a fresh
volume before distributing it. Restored routes default to read-only; pass `--writable` to allow
MCP saves and optionally `--context <name>` to choose the context name. See [bundle instructions](docs/BUNDLE_README.md).

Keep the private corpus, machine routing, audit material and exports out of the generic repository.
The Markdown remains the source of truth; Redis is its searchable publication. Curated content changes
after editorial review of architecture changes. Adding automatic regeneration later requires a
separate freshness policy.

### Implementation depth

Durable context includes runtime topology, control and data flow, persistence and
extension boundaries, failure semantics, and how a published artifact reaches an
execution. Describe stable mechanisms and contracts rather than copying method
signatures, line numbers, machine paths or release pins. Keep exact file/revision
evidence in manifest provenance outside retrieval text. Reconcile conflicting
older statements and remove retired application topics when a corpus is revised.

Evaluate realistic questions for the required architectural facts, not merely a
matching document filename. Preserve prior product knowledge and test a sample of
earlier queries alongside new questions. Report the scope of these checks without
claiming exhaustive recall or application runtime validation.
