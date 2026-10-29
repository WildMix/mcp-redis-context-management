# Project memory over MCP (Redis vector search)

A long-term memory shared by every coding agent on a project. Claude Code, Codex, parallel sessions
and every git worktree all read and write the same memory. A background synchronizer imports project
documentation and saved agent context while Claude or Codex is open. Agents search that knowledge
before starting work and can still save notes explicitly.

- **`mcp_server.py`** serves MCP over stdio or a shared local HTTP connection, with these tools:
  - `list_memory_contexts()` lists configured contexts, their projects, document names and read-only status.
  - `search_project_memory(query)` returns the 3 entries of the current project most similar to the
    query, plus live reference material from any configured external MCP sources.
  - `save_to_project_memory(title, content)` stores a note.
  - `get_memory_history(memory_id)` reads previous revisions.
  - `get_context_document(source, id)` reads a longer external source page.
- **`sync_memory.py`** watches local Claude/Codex clients and synchronizes configured projects on
  startup, every 15 minutes while a client is open, and once after the last client closes.
- **`migrate_memory.py`** performs the incremental imports used by the synchronizer and can also be
  run manually. `--agent-context` includes project-scoped Claude/Codex memories and summaries.

Entries live in a Redis Stack vector index (RediSearch, HNSW, cosine). They are embedded locally with
`sentence-transformers/all-MiniLM-L6-v2` (384 dimensions), so embedding inference stays local. When external sources are configured, scoped search
queries and document IDs are sent to those sources.

## How projects are told apart

With `MEMORY_ROUTES` configured, select memory from **any directory**, without a local checkout.
Call `list_memory_contexts()` to discover context and project names, then:

```text
search_project_memory(query="How are builds executed?", context="reference")
search_project_memory(query="Why was this design chosen?", context="projects", project="game")
save_to_project_memory(title="A decision", content="...", context="projects", project="game")
```

`context` names a user-defined Redis target. Each target independently sets `read_only` (default true)
and may optionally set a fixed `project`. Targets without a fixed project require `project`; writable
targets accept notes for a valid new project ID. Notes can be saved
without a directory route. Explicit selection overrides `workdir` and client roots and never
searches another database on a miss. Unknown contexts, missing required project IDs and conflicting
fixed project IDs fail. These saves omit branch/worktree provenance because the caller may be in an
unrelated checkout. Discovery lists both stored and routed project IDs, with a `resources` map
from each project to its source documents. This exposes components grouped under a shared corpus
instead of requiring agents to recognize only its umbrella project name.

For a question spanning contexts or projects, use that catalog to split it into focused searches
in each relevant context/project, then combine the cited findings. Comparisons
need evidence for each side, with additional searches when three results do not cover every part.
Do not send the entire mixed question to one project, infer that a miss applies to other contexts,
or ask the user to choose one scope when the question clearly requests several. Single-topic
questions still use just the relevant scope; no cross-context database or source fallback occurs.

Without explicit selectors, the existing directory-based behavior below applies.

Each entry carries a project name: the folder name of the repository's main checkout. All worktrees
of a repository therefore share one memory (`~/game`, `~/game-claude` and `~/game-codex` are all
`game`). Outside git it's the directory's own name, and `MEMORY_PROJECT` overrides it.

For repositories with the same folder name, assign different explicit IDs once in each repository:

```sh
git config --local project-memory.id team-one-api
```

The setting lives in the shared Git configuration, so linked worktrees use the same ID. It is read
on every call; no server restart is needed. IDs accept 1–128 letters, digits, dots, underscores or
hyphens and are normalized to lowercase. Precedence is `MEMORY_PROJECT`, then `project-memory.id`,
then the existing folder-name default. Independent clones need the same ID configured separately
if they should share memory. Changing an ID selects another project's memory; it does not move or
delete existing notes. Import docs with the new ID if needed; old notes remain under their old name.

The server works out the directory each call is about from:
1. the tool's optional `workdir` argument;
2. else the client's MCP roots;
3. in **stdio mode only**, else its own working directory. Claude Code and Codex both start stdio servers in the session's
   directory, so one global registration serves every project.

For shared HTTP without explicit context selection, pass the absolute project directory as `workdir`. Without it, the
server accepts exactly one existing local MCP root; missing or ambiguous roots produce an error.
Relative and nonexistent directories are rejected. Each request is resolved independently, so
concurrent sessions can work in different projects. Shared HTTP rejects `MEMORY_PROJECT`, since it
would pin every client to one project; use repository-local `project-memory.id` instead.

## Files

| File | Role |
| --- | --- |
| `docker-compose.yml` | Redis Stack on `127.0.0.1:6379` (RedisInsight on `:8001`), data in a volume |
| `requirements.txt` | `mcp`, `redis`, `sentence-transformers` (+ torch), `numpy` |
| `mcp_server.py` | The MCP server; `--query "..."` runs one search in the terminal |
| `memory_cli.py`, `bin/memory` | Shell client of the shared HTTP server, for agents without MCP support |
| `memory_store.py` | Shared by both scripts: settings, Redis connection, index schema, project names, chunking |
| `migrate_memory.py` | One-off import of existing Markdown; `--rebuild` re-embeds everything |
| `agent_context.py` | Project-scoped Claude/Codex memory and summary extraction for `--agent-context` |
| `sync_memory.py` | Lightweight client watcher, active synchronization worker, manual sync and status CLI |
| `process_lock.py` | OS-backed locks shared by manual imports, rebuilds and background synchronization |
| `scripts/install-sync-autostart.ps1` | Windows sign-in task for the client watcher |
| `scripts/remove-memory-hooks.py` | Back up client configuration and remove only the retired memory hooks |
| `scripts/install-redis-autostart.ps1` | Windows task installer for shared Redis startup at sign-in, or optionally boot |
| `scripts/keep-redis-running.ps1` | Hidden task worker: start the existing Redis container and keep WSL alive |
| `scripts/install-mcp-autostart.ps1` | Windows sign-in task installer for the shared HTTP MCP server |
| `scripts/keep-mcp-running.ps1` | Hidden worker: keep one model loaded, log failures and restart the HTTP process |
| `scripts/run-hidden.py` | Console-free task launcher; owns the worker process tree and logs startup failures |
| `sync_projects.example.json` | Template for the local `sync_projects.json` project scope and timing settings |
| `tests/` | Unit regressions, disposable Redis integration tests, and an optional real-model smoke test |
| `requirements-lock.txt` | Complete dependency snapshot of the tested environment |

## 1. Start Redis

For a Mac restoring multiple isolated corpus exports, follow
[the macOS setup](docs/MACOS_SETUP.md). It runs both Redis instances and one shared MCP/model
process at sign-in, with project synchronization disabled.

```sh
docker compose up -d --wait
```

The container restarts with Docker and keeps its data in the `redis-data` volume (append-only
file). RedisInsight, for browsing the index, is at <http://localhost:8001>.

**Windows with Docker inside WSL** (Docker Engine in a WSL distro, no Docker Desktop): run compose
inside the distro.

```powershell
wsl -d Ubuntu --cd /mnt/c/path/to/mcp-redis-context-management -- docker compose up -d --wait
```

- **Reaching Redis from Windows.** Windows reaches `127.0.0.1:6379` through WSL's localhost
  forwarding. This was tested with `networkingMode=mirrored`.
- **WSL stops idle distros.** WSL shuts a distro down seconds after its last session ends, and Redis
  stops with it. Set `REDIS_WSL_DISTRO=Ubuntu` for the server and the scripts, as the configurations
  below do. Every process using the memory then holds the distro open while it runs, and waits up to
  60 s for Docker to bring Redis back.
- **Git Bash** rewrites `/mnt/...` arguments to `wsl.exe`. Run the command from PowerShell, or
  prefix it with `MSYS_NO_PATHCONV=1`.
- **VPN address pools.** The compose file uses Docker's default bridge network. A dedicated compose
  network fails with "all predefined address pools have been fully subnetted" when VPN routes cover
  Docker's address pools.

### Start shared Redis automatically on Windows

All projects and both MCP clients already use the same Redis database at `localhost:6379/0`.
Project tags separate their memories. To keep that database ready before the first MCP call, install
one background task after creating the Redis container with compose and the Python virtual
environment from section 2 (the task uses its `pythonw.exe` launcher):

```powershell
.\scripts\install-redis-autostart.ps1 -Distro Ubuntu
```

The default is **startup at sign-in**, under the Windows account that owns the WSL distribution.
The task starts immediately too. It waits for Docker, starts the existing `project-memory-redis`
container, checks `PONG`, and holds WSL open with a hidden process. It runs on battery, has no time
limit, and retries WSL exits after 10 seconds. Task Scheduler also has a one-minute retry policy for
failure of the worker itself. It preserves the existing database volume and does not
create another Redis instance. Docker must already be enabled in the distro (`systemctl is-enabled
docker`); the distro's user must be allowed to run Docker.

```powershell
Get-ScheduledTask -TaskName ProjectMemory-Redis
Get-ScheduledTaskInfo -TaskName ProjectMemory-Redis
Get-Content "$env:LOCALAPPDATA\project-memory\redis-autostart.log" -Tail 10
```

`Running` and task result `267009` (`0x41301`, task still running) are expected. The log should say
`Redis ready; keeping WSL alive`. No console window opens. Keep `REDIS_WSL_DISTRO=Ubuntu` in
the MCP configuration as a fallback if the task is unavailable. Keeping Redis up removes the WSL
and database startup delay; each new stdio MCP process still loads its embedding model on first use.

For startup **before sign-in**, an administrator can instead run the installer with `-Mode Boot
-UserId 'DOMAIN\user'`, using the Windows owner of the WSL distribution. This selects a boot trigger
and an S4U logon without storing a password. Verify it after a reboot on the target machine: account
policy can reject batch logons. On the machine where this was implemented, Windows denied boot task
registration; sign-in startup was installed and verified instead.

To remove automatic startup (without deleting Redis or its data):

```powershell
Stop-ScheduledTask -TaskName ProjectMemory-Redis
Unregister-ScheduledTask -TaskName ProjectMemory-Redis -Confirm:$false
```

## 2. Install

```sh
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-lock.txt    # Windows; .venv/bin/python elsewhere
```

The first run downloads the model (about 90 MB, cached by Hugging Face).

The dependency snapshot was tested with Python 3.14.5 on Windows. `requirements.txt` pins the direct
dependencies and embedding runtime; `requirements-lock.txt` also pins their installed dependencies.
The compose file pins the tested Redis Stack image by registry digest. Update these pins deliberately
and run the regression and integration tests before distributing a new environment.

## 3. Register the server

The examples use `<repo>` for this repository's absolute path, and `<python>` for its venv
interpreter (`<repo>\.venv\Scripts\python.exe` on Windows, `<repo>/.venv/bin/python` elsewhere).
Leave out `REDIS_WSL_DISTRO` when Docker runs natively.

### Shared HTTP at sign-in (Windows)

After installing the dependencies and Redis startup task above, install the MCP task:

```powershell
.\scripts\install-mcp-autostart.ps1 -Distro Ubuntu
```

This starts `ProjectMemory-MCP` immediately and at each sign-in, without administrator rights or a
visible window. One Python process serves all clients at `http://127.0.0.1:8765/mcp`, keeping one
embedding model in memory between sessions. It loads the model, runs a warmup embedding and checks
Redis before accepting connections. Startup can take 5–30 seconds or longer on a cold machine;
subsequent clients reuse the loaded model. Simultaneous model/tokenizer operations are serialized.
The worker retries failed processes after 10 seconds; Task Scheduler also retries worker failures.
The server monitors its worker's Windows process handle and exits when the worker stops, so stopping
or replacing the task cannot leave its model process running and occupying the port.
It runs on battery with no execution time limit. Keep the repository and virtual environment at
the installed path; reinstall the task if they move. Use `-Distro ''` for native Docker.

Use `-Port 8766` if port 8765 is already occupied, and use that same port in both clients. The host
is fixed to `127.0.0.1`. The SDK's Host/Origin checks protect against DNS rebinding; there is no
authentication between local processes. This is a trusted-machine service, not a network service.
Environment settings such as `REDIS_URL`, `MEMORY_INDEX` and `EMBEDDING_MODEL` now belong to the
scheduled server process, not the clients. Both transports use the same database by default.

**Claude Code** (user scope):

```sh
claude mcp add --transport http --scope user project-memory http://127.0.0.1:8765/mcp
```

If that name already exists, back up `~/.claude.json`, remove only the previous registration with
`claude mcp remove --scope user project-memory`, then add it again. Keep the tool permission entries
in `~/.claude/settings.json` shown below.

**Codex**: back up `~/.codex/config.toml` and replace the existing project-memory table, including
its stdio `env` subtable, with:

```toml
[mcp_servers.project-memory]
url = 'http://127.0.0.1:8765/mcp'
startup_timeout_sec = 60
tool_timeout_sec = 120
default_tools_approval_mode = "approve"
```

These settings use the clients' documented Streamable HTTP transport:
[Codex](https://learn.chatgpt.com/docs/extend/mcp),
[Claude Code](https://code.claude.com/docs/en/mcp). Restart existing clients to pick up the change;
old sessions may retain their stdio process until they exit. The tool names and permissions stay the same. The task installer does not rewrite client configuration.

Check the running service after warmup:

```powershell
Get-ScheduledTask -TaskName ProjectMemory-MCP
Invoke-RestMethod http://127.0.0.1:8765/health
Get-Content "$env:LOCALAPPDATA\project-memory\mcp-autostart.log" -Tail 10
Get-Content "$env:LOCALAPPDATA\project-memory\mcp-server.stderr.log" -Tail 20
```

`/health` returns `status: ready`, `model_loaded: true` and the shared process ID. It checks process
and model readiness; actual searches/saves also check Redis. The stdout log records warmup time.
Server stdout/stderr logs describe the current run; large logs rotate to `.previous` on restart.
No health response during warmup is expected. If startup fails, inspect stderr and confirm the
Redis task is running. The worker retries once its dependency or port problem is corrected.

After updating server code or dependencies, rerun the installer to replace the running task and
load the new code/model. A server restart closes its HTTP sessions; reconnect clients if needed.
To disable automatic startup:

```powershell
Stop-ScheduledTask -TaskName ProjectMemory-MCP
Unregister-ScheduledTask -TaskName ProjectMemory-MCP -Confirm:$false
```

Redis and its data remain available. To return to per-client processes, use the stdio configuration
below and restart clients.

### Per-client stdio (alternative)

**Claude Code**: register at user scope, so every project and worktree gets the tools.

```sh
claude mcp add --scope user project-memory -e REDIS_WSL_DISTRO=Ubuntu -- <python> <repo>/mcp_server.py
```

To let agents use both tools without a permission prompt each time, add these to
`permissions.allow` in `~/.claude/settings.json`:

```json
"mcp__project-memory__search_project_memory",
"mcp__project-memory__list_memory_contexts",
"mcp__project-memory__save_to_project_memory"
```

**Codex**: add this to `~/.codex/config.toml`. `default_tools_approval_mode = "approve"` runs the
tools without asking. The longer timeouts cover a cold start: the first call waits for the model to
load and, with WSL, for Redis to come back.

```toml
[mcp_servers.project-memory]
command = '<python>'
args = ['<repo>/mcp_server.py']
startup_timeout_sec = 30
tool_timeout_sec = 120
default_tools_approval_mode = "approve"

[mcp_servers.project-memory.env]
REDIS_WSL_DISTRO = "Ubuntu"
```

Section 6 shows how to check that both clients reach the server.

## 4. Enable background synchronization

Copy `sync_projects.example.json` to the ignored `sync_projects.json`. List explicit project IDs
and their checkout directories; use the same IDs as the MCP server (`project-memory.id` or the
main checkout folder name). Group all checkouts of one project in one entry. Linked worktrees are
also discovered for agent-context extraction. The configured directories are scanned for docs;
add each checkout whose docs should contribute. Do not put unrelated projects in the same entry.

```json
{
  "interval_seconds": 900,
  "poll_seconds": 5,
  "retry_seconds": 60,
  "client_process_names": ["codex.exe", "claude.exe"],
  "projects": [
    { "project": "game", "directories": ["C:/projects/game", "C:/projects/game-feature"] }
  ]
}
```

Install the Windows sign-in watcher after setting up Redis:

```powershell
.\scripts\install-sync-autostart.ps1
```

The task `ProjectMemory-Sync` starts immediately and at sign-in, without administrator rights or a
visible console. It polls native Windows client process names in the same Windows logon session.
An open desktop app or CLI counts as active, including an idle conversation. It does not detect
closing individual conversations inside a still-open app. The default poll interval gives about
five seconds of detection latency; processes that open and close entirely between polls can be
missed. WSL-only clients and older Node-only clients require another detector and are not currently
covered. Process names can be adjusted in the configuration; avoid broad names such as `node.exe`.

The watcher itself uses only the standard library and loads no embedding model. When the first
client is detected, it starts one worker. That worker imports all configured projects immediately,
then every 15 minutes after each completed cycle. It performs a final import when the last client
closes, even if less than 15 minutes have elapsed, then exits and releases its model. The lightweight
watcher remains available for the next client. A client opening during the final import keeps the
worker alive. The worker loads the embedding model only when changed documents need it and reuses
it across projects and subsequent cycles; unchanged documents are not re-embedded.

Failures are logged and retried after 60 seconds. A failed project does not prevent the others
from synchronizing. A pending-final-scan marker survives worker crashes or sign-out; the next
watcher startup retries it even if no client is open. Stopping the scheduled watcher also stops
its worker, leaving that marker for recovery. Abrupt shutdown cannot guarantee a final import
before power-off. Configure the existing Redis autostart task so Redis is available to the worker.

Automatic runs use `--agent-context` with transcripts disabled. Optional `claude_home` and
`codex_home` properties override the default agent storage locations. The configuration is re-read
while watching and before each scan. A per-configuration worker lock prevents duplicate workers;
a separate lock per Redis URL/index serializes manual imports, background imports and rebuilds
on this machine. These OS locks are released if a process crashes. They do not coordinate separate
computers using the same Redis database.

### Retire the old save hooks

Preview and remove only this repository's Stop/compaction hook registrations:

```powershell
.venv/Scripts/python scripts/remove-memory-hooks.py
.venv/Scripts/python scripts/remove-memory-hooks.py --apply
```

The removal script backs up `~/.codex/hooks.json` and `~/.claude/settings.json` before changing them,
retaining unrelated hooks and settings. `hooks/stop_hook.py` is an inert compatibility file for
clients that cached old registrations. It never reads transcripts, blocks a turn or requests a
save. There are no new session or turn hooks. `save_to_project_memory` remains available for
explicit notes; agents should record durable decisions and their reasons in project documentation
so the synchronizer can discover them.

### Check freshness

```powershell
.venv/Scripts/python sync_memory.py --status
.venv/Scripts/python sync_memory.py --once
Get-ScheduledTask -TaskName ProjectMemory-Sync
Get-Content "$env:LOCALAPPDATA\project-memory\sync.log" -Tail 20
Get-Content "$env:LOCALAPPDATA\project-memory\sync-watch.log" -Tail 10
```

`--status` shows detected client PIDs and each project's last successful synchronization. The
worker log includes import counts and failures; the watcher log records worker starts and exits.
Both rotate. `--once` requests a manual sync using the configured scope, even without open clients;
it fails with a lock message if the scheduled worker already owns that configuration. Direct
`migrate_memory.py` remains available and is serialized against individual active imports.

These timestamps describe file synchronization, not verification of the claims in those files.
The synchronizer does not regenerate a Markdown summary when underlying code changes. Before
importing a gathered corpus, follow [Curated context and freshness](CONTEXT_CURATION.md): select
durable knowledge, retain implementation dumps as evidence, and keep machine paths and revisions
separate from embedded content. Curated context explains shared architecture, technical behavior and
source-checked run/test workflows; review it when the design or documented entry points change.
Curated pages use one complete H2 topic per retrieval chunk. The offline
[curated preview workflow](CONTEXT_CURATION.md#explicit-selection-and-offline-preview) checks an explicit
reviewed-file selection with the production chunker and cached model tokenizer before any Redis import.
It does not need per-commit summary regeneration. Other corpora can use a separate,
more automated maintenance policy. Portable source reconciliation and any future code-derived
summary refresh are not implemented by the current sync job.

For multiple user-defined Redis instances and reviewed, portable imports, use the
[isolated migration workflow](CONTEXT_CURATION.md#publish-to-independent-instances). It includes
explicit MCP routing, per-context write permissions and a ZIP exporter with a tested restore workflow.
This is an alternative to the broad automatic source collector described above.

## 5. Import or synchronize context manually

Context written before the save tool existed can be imported in one go. Directories inside a git
checkout contribute only the `.md` files git does not ignore.

```sh
<python> migrate_memory.py --project game ~/game ~/game-claude ~/game-codex ~/.claude/projects/<id>/memory
```

Without `--project`, files go to the project of the current directory. Re-running is incremental:
unchanged files are skipped, edited ones re-chunked, deleted ones dropped. Notes saved by agents are
never touched. Identical paragraphs found in several worktrees show up once in search results, with
the other copies listed.

Git enumeration errors, timeouts, and filesystem traversal/read errors abort the scan before any
pruning. Each changed file's old chunks, new chunks and fingerprint are updated in one Redis
transaction. A failed transaction leaves the previous import intact. Deleted files' chunks and
fingerprints are removed together as well.

### Include Claude and Codex context

Add `--agent-context` to discover saved agent knowledge for the supplied project directories and
their linked Git worktrees. Start with a preview; it lists source paths and sizes without connecting
to Redis, loading the embedding model, or writing files:

```powershell
.venv/Scripts/python migrate_memory.py --project game C:/projects/game --agent-context --dry-run
.venv/Scripts/python migrate_memory.py --project game C:/projects/game --agent-context
```

With no directories, `--agent-context` uses the current directory. The destination project still
defaults to the current directory's project, as for a normal import; use `--project` when running
from this tool's checkout to import another project. Supply other existing independent checkouts
explicitly if they should be included; only linked worktrees are discovered automatically.
Use the same set of project directories when refreshing an import.

| Source | Imported by default with `--agent-context` |
| --- | --- |
| Each checkout's `.claude/` and `.codex/` | Markdown knowledge files, including Git-ignored ones; caches and plugins are skipped |
| `~/.claude/projects/<project>/memory/` | Markdown memories, matched through session working directories or the encoded checkout path |
| `~/.codex/memories/rollout_summaries/` | Markdown summaries whose `cwd` belongs to the project |
| Codex `MEMORY.md` and `raw_memories.md` | Project-specific task groups and thread sections, not the complete files containing many projects |
| Claude project JSONL and Codex `sessions/` / `archived_sessions/` JSONL | Readable compaction summaries associated with the project |

Add `--include-transcripts` to also extract user and assistant conversation text. Tool calls,
tool results, reasoning blocks, system/developer instructions, credentials/configuration files,
and binary databases are not imported. This is text extraction, not an LLM distillation or a check
that historical statements are still true. Encrypted compaction records cannot be recovered by
this script. Unsupported layouts and global knowledge without project attribution remain out of
the automatic import; project-specific Markdown in custom locations can still be supplied as an
ordinary directory argument. Selected memories or messages can themselves contain sensitive text.

Global agent data is matched to absolute checkout paths, not just project folder names. Ambiguous
mixed-project sessions are skipped. Codex task groups are selected only when all their referenced
rollout summaries belong to the selected project. Override storage locations with `--claude-home`
and `--codex-home`; their defaults respect `CLAUDE_CONFIG_DIR` and `CODEX_HOME`, respectively.

The extraction writes readable Markdown under this tool's ignored
`.memory-imports/<project-hash>/` directory, with original source paths and summary/message line
references. These files then use the existing chunking, embedding and incremental import process.
Search citations point to the extracted Markdown; its source header links it back to the original
agent file. No Claude or Codex source files are changed. Extraction reads finish before generated files or document records are changed; synchronization
status records are updated when an attempt starts or fails. Invalid complete JSONL records abort; an unfinished final record in an
active session is ignored until the next run.

Re-running updates changed extracts and removes vanished extracts from the selected categories.
An import without `--include-transcripts` preserves any previously imported transcripts. A normal
docs-only import also leaves the extraction cache's database entries alone. Agent-saved notes are
unaffected. Keep the cache local: it contains copies of the selected project context.

### Token-aware chunks and search diversity

Both notes and documents are split using the loaded model's tokenizer. The budget includes special
tokens and the title, filename and section path; no chunk's embedding input exceeds the model's
sequence limit (256 tokens for the default model). The 1,000-character limit is an additional size
cap, not an estimate of that token budget. Oversized titles or headings produce an explicit error
so they can be shortened. New notes reconstruct exactly after trimming outer whitespace. Document pieces
retain the original paragraph's source-line range, which can overlap across pieces. Markdown heading
paths stay correct even when levels are skipped, such as `#` followed by `###`.

Search starts with 30 nearest candidates and doubles that number while duplicates leave fewer than
three distinct results. It stops when it has three results or exhausts the project's candidates.
Copies are folded by embedded content and chunks of one note count as one result. First-time saves
are synchronized across processes, so parallel agents can both initialize and save successfully.

### Rebuilding safely

```sh
<python> migrate_memory.py --rebuild
```

Rebuilds load the model first, re-chunk stored text to its token budget, write a separate generation
of records, and validate its index before switching the active memory in one Redis transaction.
The original text and index remain available while preparation runs. A model, encoding or index
validation failure leaves the active memory intact. Source Markdown files are not required.

Every application write increments a revision. If notes or imports change during preparation, the
rebuild aborts with a retry message instead of losing concurrent changes. Pause writers and rerun it.
An operation that attempts to write into a retired generation also receives a retry message. Searches
retry if a generation changes underneath them. After a successful switch, the old generation is
removed; the public index name remains usable as a Redis search alias.

Existing records remain readable without a rebuild. To fix embeddings of previously truncated text,
run a rebuild; reimport source docs to recalculate old Markdown heading paths. **Before the first
rebuild after upgrading, restart all MCP server processes** so every writer uses generation and
revision checks. Avoid direct Redis edits during a rebuild because they bypass those checks.

If the process is killed, or Redis becomes unreachable during cleanup, an unused staging or retired
generation may remain. Inspect `HGETALL project_memory:meta` and `FT._LIST` before cleaning it up;
never delete the active generation. Retained staging records do not participate in normal searches.
Rebuilding temporarily needs space for both generations.

## 6. Verify the setup

Go through these checks in order, since each relies on the ones before. `<project>` stands for a
directory listed in `sync_projects.json`.

**1. Redis is up.** With Docker in WSL, prefix the command with `wsl -d Ubuntu --`.

```sh
docker exec project-memory-redis redis-cli ping
```

Expected: `PONG`.

**2. The server works on its own.** This runs the server's own search code from the terminal,
without any client: it loads the model, connects to Redis and searches the project.

```sh
<python> <repo>/mcp_server.py --query "setup check" --workdir <project>
```

Expected: a header naming the project, such as `3 closest entries of project "game" for "setup
check"`, followed by the results. Before anything has been saved or imported, it prints `The project
memory is empty: nothing has been saved or imported yet.` instead. Any other output is an error
message saying what's wrong (Redis unreachable and how to start it, or a model mismatch).

**3. Claude Code connects.**

```sh
claude mcp get project-memory
```

Expected: `Status: ✔ Connected`, with the command, arguments and environment you configured
(`claude mcp list` shows the same status for every server). In a session, `/mcp` lists the server
with its three tools.

**4. Codex connects.** `codex mcp list` only shows the configuration (`enabled`). To check the
connection itself, make one headless call from `<project>`:

```sh
codex exec "Call the search_project_memory tool with the query 'setup check' and print its output verbatim."
```

Expected: the same output as check 2. In the Codex TUI, `/mcp` lists the tools.

**5. Saving and searching work end to end.** In an agent session in `<project>`, ask:

> Save a note to the project memory titled "Setup check" with the content "Verifying the memory
> setup.", then search the project memory for "setup check".

Expected: `Saved note <note id> to the memory of project "<name>".`, and the note as the first
search result: `note of <date> by claude-code` (or `codex-mcp-client`), with branch and worktree.
Delete the note afterwards (see "Inspecting and maintaining").

**6. Background synchronization is running.** Run `sync_memory.py --status`. While a native Claude
or Codex client is open, the client PID list should be nonempty and configured projects should
show `status: ok` after their initial imports. Edit a small project Markdown file and verify that
the next periodic scan, or the final scan after closing all clients, updates its searchable text
and the timestamps below. No agent turn should be interrupted for a memory review.

### Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Check 3 shows `✘ Failed to connect` | The client can't start the server. If check 2 works, the command or paths in the client's configuration are wrong. If check 2 fails too, its error says why (a missing package, Redis, ...). |
| A tool answers `Redis is not reachable` | Start Redis (step 1). With Docker in WSL, set `REDIS_WSL_DISTRO` in the server's configuration. |
| A tool answers `was built with ..., not ...` | The index was built with another embedding model. Set `EMBEDDING_MODEL` back, or run `migrate_memory.py --rebuild`. |
| Searches show another project, or miss what you saved | Check the project named in the result header and pass the absolute project directory as `workdir`. Check repository-local `project-memory.id` if folder names collide. `MEMORY_PROJECT` is only supported for per-client stdio/CLI. |
| Shared HTTP cannot connect | Check `ProjectMemory-MCP`, `/health` and the stderr log in section 3; allow startup warmup time. Restart clients after changing their transport or restarting the server. |
| Shared HTTP asks for `workdir` | Select `context` and `project`, or pass an existing absolute project directory. The shared process cannot use its own directory as the client's project. |
| `No memory route for this directory` | Call `list_memory_contexts`, then select `context` (and `project` when required) to use memory from anywhere. Directory routes remain an optional convenience; see [context selection](CONTEXT_CURATION.md#select-context-from-any-directory). |
| Documents are stale | Check `sync_memory.py --status`, the project scope in `sync_projects.json`, and the sync logs. An `error` status preserves the previous success time. |

## Using it

The background job synchronizes project sources. Searching depends on the agent deciding to look things up. The
tool's description and the server's instructions already tell agents to search before answering
about design, past work or code they can't see. A guardrail in the project's own instruction files
makes it stick, because agents read those at the start of every session. Put this in `AGENTS.md`
(Codex and others) and in `CLAUDE.md` (Claude Code). In `CLAUDE.md`, write the tool names in full:
`mcp__project-memory__search_project_memory` and `mcp__project-memory__save_to_project_memory`.

```markdown
# Project memory

Use the shared `project-memory` MCP server:

- Call `search_project_memory` before answering, planning or editing in an unfamiliar project area.
  Rephrase missed searches, read cited docs, and check historical notes against current code.
- Record lasting project decisions and their reasons in project documentation, which is
  synchronized automatically. `save_to_project_memory` remains optional for lasting knowledge
  missing from those sources; search for duplicates first. No end-of-turn review is required.

See [README.md](README.md#using-it) for details.
```

Keep these instruction files short and limited to the MCP guidance above. Adjust the README link
when copying the block into another project; keep detailed setup and usage instructions in docs.

Commit both files on every branch agents work on: a session reads them from its own checkout. If
they are identical everywhere, merges between branches stay conflict-free.

From a terminal:

```sh
<python> mcp_server.py --query "why did the build switch to clang?" --workdir ~/game
```

### Agents without MCP

Agents that only have a shell tool, such as DwarfStar's `ds4-agent`, call `bin/memory`, a thin
client of the shared HTTP server (POSIX shells; it runs this checkout's `.venv`):

```sh
memory search "why did the build switch to clang?"   # routed by the current directory
memory search -c projects -p game "..."              # explicit context/project
memory save "Title" <<'EOF'                         # note content comes from stdin
...
EOF
memory contexts                                     # contexts, projects and document topics
```

It needs the warm server: a standalone process would load the model on every call (5-30 s).
Like MCP clients, it works from any directory. When a call without `-c`/`-p` fails, typically
because the directory has no route, the error also lists the contexts, so the agent can retry
with an explicit selection without spending a turn on `memory contexts`.
Search output is bounded to `MEMORY_MAX_CHARS` (default 3000) characters, shared equally between
the results, because a small local model pays for every result token in prefill; `--max-chars 0`
prints everything. `MEMORY_URL` selects the server, `MEMORY_CONTEXT` and `MEMORY_PROJECT` are
defaults when no `-c`/`-p` is passed, and `MEMORY_AGENT` names the agent recorded on saved notes.
Put `bin` on the agent's `PATH` and describe the command in its system prompt. Include the output
of `memory contexts` there: without it, a small model cannot tell that a question such as "what
is DUC?" belongs to a memory topic, and answers without searching. For `ds4-agent`, a launcher
does all of this and passes the instructions with `-sys`.

## Settings (environment variables)

| Variable | Default | |
| --- | --- | --- |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `MEMORY_INDEX` | `project_memory` | index name and key prefix |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | after changing it, run `migrate_memory.py --rebuild` |
| `MEMORY_PROJECT` | unset | pins every call to one project name |
| `REDIS_WSL_DISTRO` | unset | WSL distro running Docker, held open while a process uses Redis |

## Timestamps and synchronization status

New or refreshed document chunks have UTC ISO-8601 fields `updated_at` (when that content was
written to Redis) and `source_modified_at` (the imported file's modification time). For extracted
agent context, the source file is the generated Markdown in `.memory-imports`. Unchanged content
keeps its entry timestamp. New notes have `updated_at` equal to their save time; existing notes
get that field backfilled from their original `created` time during a project sync. Rebuilds
preserve these timestamps because re-embedding does not change the stored knowledge.

Each project's `project_memory:sync:<project>` hash records `last_attempt_started_at`, `status`,
`last_success_started_at`, `last_success_completed_at`, directory scope, and the latest successful
file/chunk counts. A successful scan advances its success timestamps even when every file is
unchanged: only this small status record is written. Failures retain the previous success times
and set `status: error` with an error type; full diagnostics are in the log. Search results also
show the last successful sync and returned documents' update times.

The first import after this upgrade refreshes existing documents once to populate the new fields.
Subsequent imports are incremental. Timestamps describe a scan interval and individual document
updates, not a transactional snapshot of the entire repository: files can change during a scan,
and an error can leave some files already updated. A file detected changing while it is read
aborts that scan for retry. Git commits remain the way to identify an exact source revision.

## How the memory is stored

Redis stores values under names called *keys*. Every entry of the memory is a Redis *hash* (a small
record of named fields) under its own key. RediSearch keeps an index over those hashes, so that
`search_project_memory` can find the nearest vectors within one project. All keys start with the
index name, `project_memory` by default (`MEMORY_INDEX`). You never have to touch them, but knowing
them helps when you inspect the memory, delete a wrong note, or start over.

| Key | What it holds | Written | Read |
| --- | --- | --- | --- |
| `project_memory:note:<note id>:<n>` | One piece of a note: title, text, project, agent, branch, worktree, time, and embedding. Pieces fit both the model's token budget and a 1000-character cap, numbered `0, 1, ...`. New notes carry `join_mode=exact`; older notes retain their original paragraph-joining behavior until rebuilt. | By `save_to_project_memory`, once per note; re-chunked by rebuild. | Search reads all pieces when one matches. |
| `project_memory:doc:<file id>:<n>` | One paragraph-sized piece of an imported Markdown file. Fields: text, section path, source file, line range, project, embedding. The file id is a hash of the file's path. | By `migrate_memory.py`. A re-run replaces a file's pieces if the file changed since the last import, and deletes them if the file disappeared. | By every search, through the index. |
| `project_memory:files` | One field per imported file: its path, mapped to a fingerprint of its content (and of the project and chunking settings). | By `migrate_memory.py`, after importing each file. | By `migrate_memory.py` only, to skip files that haven't changed. Delete this key to force a full re-import. |
| `project_memory:sync:<project>` | Latest attempt status and last successful scan timestamps/counts for one project. | By imports, including unchanged scans. | By `sync_memory.py --status` and MCP search. |
| `project_memory:meta` | Embedding model, vector size, active `generation`, and write `revision`. An absent/empty generation selects the original layout. | At initialization, each successful write, and rebuild cutover. | Search, saves, imports and rebuilds validate this metadata. |
| index/alias `project_memory` | Initially the original physical index; after rebuilding, an alias of the active generation's physical index. It covers vectors, project and kind tags, and text. | Created by the first save or import; switched by rebuild. | Search and manual `FT.SEARCH`/`FT.INFO` commands. |

After a rebuild, note/doc keys have the prefix `project_memory:generation:<generation>:` instead of
`project_memory:`. The physical index is `project_memory:generation:<generation>:index`. The files
and metadata keys keep their original names. `project_memory:initialize` is a short-lived Redis
lock for initialization/cutover; its lease expires if the process dies.

### Inspecting and maintaining

RedisInsight (<http://localhost:8001>) lets you browse the keys and run commands. From a terminal,
use `redis-cli` inside the container. With Docker in WSL, open a shell first with `wsl -d Ubuntu`;
the commands are the same.

```sh
docker exec -it project-memory-redis redis-cli
```

| What | Command (in `redis-cli`) |
| --- | --- |
| Count a project's entries | `FT.SEARCH project_memory "@project:{game}" LIMIT 0 0` |
| List its latest notes | `FT.SEARCH project_memory "@project:{game} @kind:{note}" RETURN 2 heading created SORTBY created DESC LIMIT 0 10` |
| Read a note | `HGETALL <active-prefix>:note:<note id>:0` (fields `content` and `heading`; see generation layout above) |
| Find the active physical index | `FT.INFO project_memory` (`index_name`), and `HGETALL project_memory:meta` |

In these queries, escape non-letter characters in project names: `@project:{my\-game}`. A note split
into several pieces is listed once per piece.

To delete one note (every piece), run this from the container's host shell:

```sh
docker exec project-memory-redis sh -c "redis-cli --scan --pattern '<active-prefix>:note:<note id>:*' | xargs redis-cli DEL"
```

Replace `<active-prefix>` using the generation layout above. Manual edits bypass application revision
tracking, so stop writers/rebuilds first. To start over, first export a backup, stop all clients, use
`FT.INFO project_memory` to identify the active physical index, then drop that physical index with
`DD` and delete `project_memory:files` and `project_memory:meta`. Check for unused generations too.

## Exporting the database and using it on another computer

The database lives in Docker's persistent `redis-data` volume, mounted at `/data` inside
`project-memory-redis`, not in this Git repository. With the default checkout name, the volume is
`mcp-redis-context-management_redis-data`. With Docker Engine in Ubuntu WSL, its files live inside
Ubuntu, normally at `/var/lib/docker/volumes/mcp-redis-context-management_redis-data/_data`.
Existing installations retain their original Docker volume names when a checkout is renamed.
Use the volume reported by Docker rather than creating a new empty volume.
Cloning or pushing this repository does not transfer the database.

To check the actual volume and location on your machine:

```sh
docker inspect project-memory-redis --format '{{json .Mounts}}'
```

For Docker in WSL, run Docker commands in an Ubuntu shell (`wsl -d Ubuntu`), or prefix them from
PowerShell with `wsl -d Ubuntu --`. Windows paths then use `/mnt/c/...` inside Ubuntu.

### Export a complete snapshot

Run these commands on the computer containing the memory, from a directory where you want to
save the export:

```sh
docker exec project-memory-redis redis-cli SAVE
docker cp project-memory-redis:/data/dump.rdb ./project-memory.rdb
docker inspect project-memory-redis --format '{{.Image}}'
docker exec project-memory-redis redis-cli HGETALL project_memory:meta
```

Wait for `SAVE` to return `OK` before copying. It briefly blocks Redis while writing a consistent
snapshot; the running database remains intact. Give colleagues `project-memory.rdb`, the image
ID printed above, and the embedding model metadata. Use the same Redis Stack image version for
restoring; `latest` may resolve to a different version on their computer. A registry digest can be
found with `docker image inspect <image-id> --format '{{json .RepoDigests}}'` and used to pin the
`image:` in their compose file.

The snapshot contains **every Redis database and every project's memory in this instance**,
including notes, imported document text, embeddings and search indexes. It is not a single-project
export. Share it only with colleagues who should receive that content, and keep it out of Git.
Each restored copy is independent: later edits do not synchronise between computers.

### Restore into a fresh local instance

On the receiving computer, clone this repository and place `project-memory.rdb` in its directory.
Use a **fresh, empty Docker volume**; these instructions do not merge with an existing database.
If this computer already has memory, export it first and restore the received snapshot in a
separate instance instead. Do not delete an existing volume to follow these steps.

1. Pin the sender's Redis Stack image in `docker-compose.yml`. Temporarily change its
   `REDIS_ARGS` to `"--appendonly no --save 60 1"`.
2. Create the container without starting Redis, copy in the snapshot, then start it:

   ```sh
   docker compose create redis
   docker cp ./project-memory.rdb project-memory-redis:/data/dump.rdb
   docker compose up -d --wait redis
   docker exec project-memory-redis redis-cli DBSIZE
   docker exec project-memory-redis redis-cli FT.INFO project_memory
   ```

   `DBSIZE` should show the restored keys in database 0 and `FT.INFO` should show the search index
   (replace `project_memory` if the sender used a different `MEMORY_INDEX`). If startup fails,
   inspect `docker compose logs redis` before proceeding.
3. Enable append-only persistence **while the restored data is loaded**:

   ```sh
   docker exec project-memory-redis redis-cli CONFIG SET appendonly yes
   docker exec project-memory-redis redis-cli INFO persistence
   ```

   Wait until `aof_rewrite_in_progress:0`, `aof_rewrite_scheduled:0`, and
   `aof_last_bgrewrite_status:ok` appear, with `aof_enabled:1`.
4. Restore `REDIS_ARGS` in the compose file to `"--appendonly yes --save 60 1"`, then run
   `docker compose up -d --wait redis`. Repeat `DBSIZE` and `FT.INFO` to confirm the data survived
   recreation.

Disabling AOF for the initial load matters: when both persistence modes are enabled, Redis loads
the append-only log in preference to `dump.rdb`. Enabling it afterwards creates an AOF from the
restored data. See the [Redis persistence documentation](https://redis.io/docs/latest/management/persistence/).

### Connect colleagues' agents to the restored memory

Follow sections 2–4 to install Python dependencies, register the MCP server and configure background
synchronization. Keep the sender's `EMBEDDING_MODEL` and `MEMORY_INDEX`; the defaults work if the sender
used the defaults. Use `REDIS_WSL_DISTRO=Ubuntu` only when Docker runs in that WSL distribution.
The local connection remains `REDIS_URL=redis://localhost:6379/0`.

Check out the relevant project under the same main folder name as the sender, or set
`MEMORY_PROJECT` in the MCP server's environment to the stored project name. This override pins
all calls to that one project. Then verify a search:

```sh
<python> mcp_server.py --query "a decision recorded in the shared memory" --workdir <project-checkout>
```

Stored document text is searchable immediately, but source paths still point to the sender's
files. Import documents from colleagues' own checkouts with `migrate_memory.py --project <name>
<directory>` if local source links are needed. Different absolute paths create new document
records; reimporting does not automatically remove records pointing to the sender's paths.

For a shared live database instead of independent copies, `REDIS_URL` can point to a team Redis
Stack server. The supplied compose file binds only to localhost and has no password; remote use
requires authentication, appropriate network access controls and transport protection.

## Running the regression tests

Unit tests need the pinned Python dependencies but no Redis or downloaded embedding model:

```sh
<python> -m unittest discover -s tests -v
```

To also run the Redis fault-injection, concurrency, generation-switch and retrieval tests, start
the supplied container and set `MEMORY_RUN_INTEGRATION=1`. To include the actual embedding model and
its tokenizer, also set `MEMORY_TEST_REAL_MODEL=1`. For example, in PowerShell:

```powershell
$env:MEMORY_RUN_INTEGRATION = '1'
$env:MEMORY_TEST_REAL_MODEL = '1'
.venv/Scripts/python -m unittest discover -s tests -v
```

On POSIX shells:

```sh
MEMORY_RUN_INTEGRATION=1 MEMORY_TEST_REAL_MODEL=1 .venv/bin/python -m unittest discover -s tests -v
```

Integration tests use `REDIS_URL` and create uniquely named `test_project_memory_<uuid>` indexes.
They remove their indexes and keys afterward and never use the application's configured memory
namespace. They fail if Redis is unavailable. Only the real-model test loads the embedding model;
set `HF_HUB_OFFLINE=1` to require the local cache. Temporary filesystem fixtures are also removed.

Coverage includes failed Git scans, interrupted document replacement, failed and concurrent rebuilds,
legacy records, concurrent first saves, exact note reconstruction, token budgets, skipped Markdown
heading levels, project IDs, search deduplication, source timestamps, sync freshness, client lifecycle, worker retries,
process locks, and selective removal of old hook registrations. HTTP tests
also cover concurrent clients, project isolation, client roots, missing workdir, reconnects, shared
model lifetime, and Host/Origin rejection. Real HTTP tests use the same real-model opt-in above.

## Startup time and a Windows trap

Windows startup tasks use `pythonw.exe`, which has no console. The Redis and MCP launchers start
PowerShell with `CREATE_NO_WINDOW`; passing `-WindowStyle Hidden` to PowerShell alone can still
flash a console before PowerShell processes its arguments. Git discovery commands also use
`CREATE_NO_WINDOW`, including when the synchronizer runs from `pythonw.exe`. The MCP keeper
launches its server with `pythonw.exe` and explicit output files. A Windows job object ties the
launcher's descendants to its lifetime, so stopping or replacing a task cannot leave its worker
running. These choices preserve interactive sign-in tasks and the existing WSL user account.

Logs stay in `%LOCALAPPDATA%\project-memory`: `redis-autostart.log`, `mcp-autostart.log`,
`mcp-server.stdout.log`, `mcp-server.stderr.log`, `sync-watch.log`, and `sync.log`.
Early launcher/PowerShell errors go to `redis-launcher.log` or `mcp-launcher.log`.
After updating an existing installation, rerun all three `install-*-autostart.ps1` scripts to
replace the registered task actions and restart their processes; editing the installer alone
does not change a task already registered with Windows.

In stdio mode, the server answers the MCP handshake in about a second, then loads the model in the background,
which takes 5–30 s (mostly importing torch; slower on a cold start). The first search or save waits
for it.

Shared HTTP instead warms the model once at sign-in, before listening. New client sessions reuse
that process and skip model loading; the first call after a sign-in still waits if warmup is unfinished.

On Windows that background loading needs a workaround. While a read is pending on a pipe, every
other operation on that pipe blocks, and scipy's Fortran runtime (pulled in through scikit-learn)
inspects stdin while it loads. With the MCP transport waiting on stdin, loading the model would
deadlock. So on Windows the server gives the transport a private pipe, fed by a thread that reads
stdin only when `PeekNamedPipe` reports data waiting. No read is ever left pending on stdin. For the
same reason, every subprocess the server starts gets `stdin=DEVNULL`.

## Local embedding comparison (12 October 2026)

The current corpus does not demonstrate a reason to replace MiniLM. A local comparison used a
read-only snapshot of **4,238 records** from `bodhidharma` and this repository, representing
**2,157 distinct embedding inputs**. Forty English questions covered twenty intents, each with
direct and paraphrased wording, balanced equally between the two projects.

| Model | Useful evidence in first 3 results | Query encoding median / p95 | Encode 2,157 unique chunks |
| --- | ---: | ---: | ---: |
| `sentence-transformers/all-MiniLM-L6-v2` | **34/40 (85%)** | **20.1 / 25.1 ms** | **95.3 s** |
| `Snowflake/snowflake-arctic-embed-xs` | 33/40 (82.5%) | 20.6 / 25.9 ms | 103.2 s |
| `BAAI/bge-small-en-v1.5` | 30/40 (75%) | 37.7 / 44.7 ms | 212.8 s |

MiniLM found useful evidence for 17/20 direct and 17/20 paraphrased questions. Arctic scored
17/20 and 16/20; BGE scored **19/20 direct but 11/20 paraphrased**. All three put useful evidence
first for 23/40 questions. Arctic gained one top-three success over MiniLM and lost two; BGE
gained three and lost seven. This small, assistant-authored and assistant-judged set does not
establish general model superiority. In particular, MiniLM and Arctic are effectively tied here.
For the current mix of natural-language project-memory requests, retain MiniLM unless a larger
set of actual user queries establishes an improvement.

Tests ran on an Intel Core i7-1270P with 32 GiB RAM, Windows, CPU PyTorch float32, and the
runtime's default 12 inference threads. The same stored chunks, filename/title/heading prefixes,
project filtering and duplicate/note grouping were used for every model. All inputs fit 256
tokens. Arctic and BGE used their documented query-only instruction; documents had no instruction
([Arctic usage](https://huggingface.co/Snowflake/snowflake-arctic-embed-xs#usage),
[BGE usage](https://huggingface.co/BAAI/bge-small-en-v1.5#usage)).

Quality uses exact cosine ranking over the frozen corpus. MiniLM's top three matched the live
Redis HNSW results, in order, for **all 40 queries**; 43 sampled stored embeddings also matched
the freshly encoded baseline within 7.5e-8 per component. The benchmark performs no Redis writes.
The alternative models were evaluated offline; no alternative Redis index or production model
configuration was installed.

Questions and expected answers were defined before retrieval. All 206 pooled top-three
candidate passages were then reviewed with model names and ranks hidden. Relevance means useful
evidence for the central answer, not necessarily every requested detail; generic topic mentions
and contradictory descriptions were rejected. For example, old instantaneous Stillness entry,
natural-pitch ambience, and pre-fix wind descriptions can outrank current implementation notes.
Changing the embedding model alone did not resolve this document-authority problem.

Headline latency uses 200 warm single-query measurements per model, with randomized model order
for each question and serial inference in one process. This final interleaved test was added
because two earlier sequential passes showed timing drift. It measures encoding only, excluding
MCP/HTTP and Redis round trips. Chunk throughput uses one batch-size-64 pass per model and excludes
file discovery, chunk validation and Redis writes. Desktop load, power and temperature can change
these times. This comparison does not cover multilingual queries, raw source-code retrieval,
model-specific rechunking, quantization, or independent human judgments.

Reviewable artifacts:

- [Results, environment, model revisions and per-question outcomes](benchmarks/embedding-comparison-2026-10-12.json)
- [Questions and expected answers](benchmarks/embedding_cases.json)
- [Pooled relevance judgments](benchmarks/embedding_judgments.json)
- [Read-only benchmark script](scripts/benchmark_embeddings.py)

Raw corpus text, downloaded weights, vectors, timings and retrieved passages stay in the ignored
`.embedding-benchmark/` directory. The snapshot hash in the report identifies this exact corpus.
To repeat against a new snapshot, run the commands below, then review `review-pool.json` and update
the relevance judgments before generating a new report. Existing judgments refer to the recorded
snapshot and are not automatically valid for changed content.

```powershell
.venv/Scripts/python scripts/benchmark_embeddings.py snapshot
.venv/Scripts/python scripts/benchmark_embeddings.py download
.venv/Scripts/python scripts/benchmark_embeddings.py labels
.venv/Scripts/python scripts/benchmark_embeddings.py run --model minilm
.venv/Scripts/python scripts/benchmark_embeddings.py run --model arctic
.venv/Scripts/python scripts/benchmark_embeddings.py run --model bge
.venv/Scripts/python scripts/benchmark_embeddings.py pool
.venv/Scripts/python scripts/benchmark_embeddings.py latency
.venv/Scripts/python scripts/benchmark_embeddings.py validate
# Review pooled evidence and update benchmarks/embedding_judgments.json, then:
.venv/Scripts/python scripts/benchmark_embeddings.py report
```

## Licences

These are all fine for commercial use:
- **all-MiniLM-L6-v2**: Apache-2.0.
- **sentence-transformers, transformers**: Apache-2.0.
- **torch**: BSD.
- **mcp, redis-py**: MIT.
- **Redis Stack server**: RSALv2/SSPLv1. You may use it for your own work, but you may not offer
  it to others as a hosted database service.

## Memory history

Saves preserve earlier versions of accepted updates. Search groups versions of the
same memory, labels historical matches, and includes their current replacement.
The optional `get_memory_history` tool reads older revisions. Agents keep using the
existing save/search interface; comparison runs locally in the shared server.

Before offline startup, run `.venv/bin/python scripts/provision-models.py`. Existing
installations should back up Redis, stop writers and initialize version metadata
with `memory_history.py --redis-url <configured-url>`, then restart their MCP server.
See [memory history](docs/MEMORY_HISTORY_PROPOSAL.md) for behavior, limitations and
migration instructions.

## External context sources

Set `MEMORY_SOURCES` to a source configuration to augment Redis searches with live,
read-only MCP context. Sources are scoped to contexts/projects; their search/fetch
tools follow a strict contract and explicit allowlist. External reads do not save
pages or create memory revisions. Direct agent connections retain their own permissions.
See [the source contract, configuration and Confluence adapter](docs/CONTEXT_SOURCES.md)
and [memory_sources.example.json](memory_sources.example.json).
