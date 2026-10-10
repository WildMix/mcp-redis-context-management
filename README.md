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
.venv/Scripts/python -m pip install -r requirements.txt    # Windows; .venv/bin/python elsewhere
```

The first run downloads the model (about 90 MB, cached by Hugging Face).

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
- **When it asks.** If that part edited files (Claude's Edit/Write tools or Codex's patches), or ran
  a shell command that changes files (`git commit`, `sed -i`, `>` redirects, `rm`, `Set-Content`,
  ...), and did not save to the memory, the hook blocks the stop. The agent gets one more
  instruction: call `save_to_project_memory` with what changed, why, and what the next agent must
  know, or reply "Nothing to save to project memory." when the change isn't worth keeping.
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
## Project memory: search before you answer

This project has a shared long-term memory, the `project-memory` MCP server, which every agent and
worktree reads and writes. It holds the notes earlier sessions saved about what they changed and
decided, and the project's docs and handoffs.

**Zero-guessing policy.** You do not hold all of this project's context. When you are asked about a
design pattern, feature, past implementation, earlier session or decision that you don't
immediately know, or can't see in the files you have open, you must not guess, and you must not
answer that you lack context. First call `search_project_memory`, then answer from what it returns.
If a search misses, rephrase it before concluding nothing is recorded. A note is history: check it
against the current code, and when they disagree, the code wins.

**Saving.** When a turn changes files, the Stop hook asks you to call `save_to_project_memory`:
record what changed and why, the decisions taken and the alternatives rejected, and what the next
agent must know.
```

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
| `project_memory:note:<note id>:<n>` | One piece of a note saved by an agent. Fields: title, text, project, agent, branch, worktree, time, and the text's embedding (vector). A long note is split into pieces of at most 1000 characters (the model reads no more), numbered `0, 1, ...`. The note id is the save time plus a random suffix, e.g. `20261010-184708-0fcaa8`. | By `save_to_project_memory`, once per note. Notes are never edited afterwards. | By every search, through the index. When one piece matches, all pieces are read so the whole note is shown. |
| `project_memory:doc:<file id>:<n>` | One paragraph-sized piece of an imported Markdown file. Fields: text, section path, source file, line range, project, embedding. The file id is a hash of the file's path. | By `migrate_memory.py`. A re-run replaces a file's pieces if the file changed since the last import, and deletes them if the file disappeared. | By every search, through the index. |
| `project_memory:files` | One field per imported file: its path, mapped to a fingerprint of its content (and of the project and chunking settings). | By `migrate_memory.py`, after importing each file. | By `migrate_memory.py` only, to skip files that haven't changed. Delete this key to force a full re-import. |
| `project_memory:meta` | The embedding model and vector size the index was built with. | When the index is created (by the first save or import), and again by `--rebuild`. | By every search and save. Vectors from different models can't be compared, so both refuse to run with another model. |
| index `project_memory` | Not a key: RediSearch's index over every `note:` and `doc:` key. It covers the vector (HNSW, cosine), the project and kind tags, and the text. | Created by the first save or import; recreated by `--rebuild`. | By `search_project_memory`: a nearest-neighbour search limited to one project. |

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
| Read a note | `HGETALL project_memory:note:<note id>:0` (fields `content` and `heading`) |
| Start over: delete everything | `FT.DROPINDEX project_memory DD` then `DEL project_memory:files project_memory:meta` |

In these queries, escape non-letter characters in project names: `@project:{my\-game}`. A note split
into several pieces is listed once per piece.

To delete one note (every piece), run this from the container's host shell:

```sh
docker exec project-memory-redis sh -c "redis-cli --scan --pattern 'project_memory:note:<note id>:*' | xargs redis-cli DEL"
```

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
