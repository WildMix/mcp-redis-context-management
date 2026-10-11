# Project memory over MCP (Redis vector search)

A long-term memory shared by every coding agent on a project. Claude Code, Codex, parallel sessions
and every git worktree all read and write the same memory. Agents save notes on what they changed and
why, and search them before starting work, instead of re-deriving earlier decisions.

- **`mcp_server.py`** is a stdio MCP server with two tools:
  - `search_project_memory(query)` returns the 3 entries of the current project most similar to the
    query.
  - `save_to_project_memory(title, content)` stores a note.
- **`hooks/stop_hook.py`** runs when an agent finishes a turn. If the turn changed files in a
  configured directory and nothing was saved yet, it asks the agent to call
  `save_to_project_memory` with what it changed, so memory gets written without anyone remembering
  to.
- **`migrate_memory.py`** imports context written *before* the save tool existed (docs, handoffs,
  older agent memory files). You need it once per project, not day to day.

Entries live in a Redis Stack vector index (RediSearch, HNSW, cosine). They are embedded locally with
`sentence-transformers/all-MiniLM-L6-v2` (384 dimensions), so nothing leaves the machine.

## How projects are told apart

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
1. the tool's optional `workdir` argument (which the hook always passes);
2. else the client's MCP roots;
3. else its own working directory. Claude Code and Codex both start stdio servers in the session's
   directory, so one global registration serves every project.

## Files

| File | Role |
| --- | --- |
| `docker-compose.yml` | Redis Stack on `127.0.0.1:6379` (RedisInsight on `:8001`), data in a volume |
| `requirements.txt` | `mcp`, `redis`, `sentence-transformers` (+ torch), `numpy` |
| `mcp_server.py` | The MCP server; `--query "..."` runs one search in the terminal |
| `memory_store.py` | Shared by both scripts: settings, Redis connection, index schema, project names, chunking |
| `migrate_memory.py` | One-off import of existing Markdown; `--rebuild` re-embeds everything |
| `hooks/stop_hook.py` | The Stop hook for Claude Code and Codex (standard library only); `--check DIR` says whether it acts in `DIR` |
| `hook_directories.example.txt` | Template for `hook_directories.txt`, the directories where the hook is active |
| `tests/` | Unit regressions, disposable Redis integration tests, and an optional real-model smoke test |
| `requirements-lock.txt` | Complete dependency snapshot of the tested environment |

## 1. Start Redis

```sh
docker compose up -d --wait
```

The container restarts with Docker and keeps its data in the `redis-data` volume (append-only
file). RedisInsight, for browsing the index, is at <http://localhost:8001>.

**Windows with Docker inside WSL** (Docker Engine in a WSL distro, no Docker Desktop): run compose
inside the distro.

```powershell
wsl -d Ubuntu --cd /mnt/c/path/to/mpc-redis-context-management -- docker compose up -d --wait
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

**Claude Code**: register at user scope, so every project and worktree gets the tools.

```sh
claude mcp add --scope user project-memory -e REDIS_WSL_DISTRO=Ubuntu -- <python> <repo>/mcp_server.py
```

To let agents use both tools without a permission prompt each time, add these to
`permissions.allow` in `~/.claude/settings.json`:

```json
"mcp__project-memory__search_project_memory",
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

## 4. Turn on the hook

List the directories where agents should be asked to save their changes:

```sh
cp hook_directories.example.txt hook_directories.txt   # then edit: one directory per line
```

The hook is installed once per client. Without a `hook_directories.txt` entry, a directory gets
nothing from it, so it never runs in unrelated projects.

**Claude Code**: in `~/.claude/settings.json`:

```json
"hooks": {
  "Stop": [
    { "hooks": [ { "type": "command", "command": "<python> <repo>/hooks/stop_hook.py", "timeout": 30 } ] }
  ]
}
```

**Codex**: in `~/.codex/hooks.json`, same format:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command", "command": "<python> <repo>/hooks/stop_hook.py", "timeout": 30 } ] }
    ]
  }
}
```

Codex runs a new or changed hook only after you trust it: open `/hooks` in Codex once and approve
it.

On Windows, write paths in hook commands with forward slashes and without quotes (`C:/...`). The
clients may run hooks through Git Bash, PowerShell or cmd, and that form works in all three.

### What the hook does

At the end of each turn it reads only the part of the transcript written since its previous run.
- **When it asks.** It asks when that part changed the project's files and saved nothing to the
  memory. A save counts only after a successful result for this project's directory. Failed calls,
  tool discovery, mentions of the tool in JavaScript, and saves to other repositories do not suppress
  the reminder. "The project's files" means files in any worktree of the session's repository:
  - edits from Claude's Edit/Write tools or Codex's patches;
  - shell commands that change files (`git commit`, `sed -i`, `>` redirects, `rm`, `Set-Content`,
    ...).

  Edits to other repositories or to configuration elsewhere don't count. Neither does a command that
  first moves to another directory (`cd`, `git -C`), or that only names paths outside the project.
- **What it asks.** The hook keeps the turn going with one more instruction: decide whether the turn
  taught something worth keeping. A save must be all of:
  - **about this project**, not about other repositories, the agent's tools or this memory system;
  - **new**, not already in the memory and not plain from the code, diff, commits or docs;
  - **lasting**, such as a decision and its reasons, a rejected alternative, a constraint or a
    gotcha.

  When in doubt the agent replies "Nothing to save to project memory." The save tool's own
  description states the same rule, so it also covers saves an agent makes without being asked.
- **No loops.** The continuation ends with `stop_hook_active` set, which lets the turn finish.
- **Never stuck.** Any error lets the turn finish too.

## 5. Import existing context (optional, once)

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
directory listed in `hook_directories.txt`.

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
with its two tools.

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

**6. The hook is set up for the directory.**

```sh
<python> <repo>/hooks/stop_hook.py --check <project>
```

Expected: `<project> is listed. After a turn that edits a file, the hook blocks the stop and tells
the agent:`, followed by the instruction the agent will get. If it says `is not listed`,
`hook_directories.txt` doesn't cover that directory. In Claude Code, `/hooks` should list the Stop
hook. In Codex, `/hooks` should show it as trusted.

**7. The hook fires in a real turn.** In a session in `<project>`, ask for a small real change, for
example *"Create hook-check.txt containing 'ok'."* When the agent has done it:
- **Claude Code** shows `Stop hook feedback` with the save instruction, then the agent calls
  `save_to_project_memory`. Claude Code labels this a "blocking error", which is how it shows any
  hook that keeps a turn going.
- **Codex** keeps the turn going and the agent calls `save_to_project_memory`.

Then delete `hook-check.txt` and the note. A turn that only reads files or runs read-only commands
ends without a save request; that is by design.

### Troubleshooting

| Symptom | Likely cause and fix |
| --- | --- |
| Check 3 shows `✘ Failed to connect` | The client can't start the server. If check 2 works, the command or paths in the client's configuration are wrong. If check 2 fails too, its error says why (a missing package, Redis, ...). |
| A tool answers `Redis is not reachable` | Start Redis (step 1). With Docker in WSL, set `REDIS_WSL_DISTRO` in the server's configuration. |
| A tool answers `was built with ..., not ...` | The index was built with another embedding model. Set `EMBEDDING_MODEL` back, or run `migrate_memory.py --rebuild`. |
| Searches show another project, or miss what you saved | Check the project named in the result header. If a client starts the server outside the project, the agent must pass `workdir` (the hook's instruction already does), or set `MEMORY_PROJECT`. |
| The hook never asks | Check 6 says the directory is not listed; or Codex hasn't trusted the hook (`/hooks`); or the turn changed no files. |
| After a real change the agent replies "Nothing to save to project memory." | It judged the change temporary, or it couldn't load the tool: check the server is connected (`/mcp`). |

## Using it

The hook makes sure agents save. Searching depends on the agent deciding to look things up. The
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
- Call `save_to_project_memory` only for new, lasting project knowledge not already in memory,
  code, commits or docs. Otherwise reply "Nothing to save to project memory."

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

## Settings (environment variables)

| Variable | Default | |
| --- | --- | --- |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `MEMORY_INDEX` | `project_memory` | index name and key prefix |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | after changing it, run `migrate_memory.py --rebuild` |
| `MEMORY_PROJECT` | unset | pins every call to one project name |
| `REDIS_WSL_DISTRO` | unset | WSL distro running Docker, held open while a process uses Redis |

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
`mpc-redis-context-management_redis-data`. With Docker Engine in Ubuntu WSL, its files live inside
Ubuntu, normally at `/var/lib/docker/volumes/mpc-redis-context-management_redis-data/_data`.
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

Follow sections 2–4 to install Python dependencies, register the MCP server and configure the
hooks. Keep the sender's `EMBEDDING_MODEL` and `MEMORY_INDEX`; the defaults work if the sender
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
heading levels, project IDs, search deduplication, and Claude/Codex save-result detection.

## Startup time and a Windows trap

The server answers the MCP handshake in about a second, then loads the model in the background,
which takes 5–30 s (mostly importing torch; slower on a cold start). The first search or save waits
for it.

On Windows that background loading needs a workaround. While a read is pending on a pipe, every
other operation on that pipe blocks, and scipy's Fortran runtime (pulled in through scikit-learn)
inspects stdin while it loads. With the MCP transport waiting on stdin, loading the model would
deadlock. So on Windows the server gives the transport a private pipe, fed by a thread that reads
stdin only when `PeekNamedPipe` reports data waiting. No read is ever left pending on stdin. For the
same reason, every subprocess the server starts gets `stdin=DEVNULL`.

## Licences

These are all fine for commercial use:
- **all-MiniLM-L6-v2**: Apache-2.0.
- **sentence-transformers, transformers**: Apache-2.0.
- **torch**: BSD.
- **mcp, redis-py**: MIT.
- **Redis Stack server**: RSALv2/SSPLv1. You may use it for your own work, but you may not offer
  it to others as a hosted database service.
