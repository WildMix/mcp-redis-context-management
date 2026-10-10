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
| `hooks/stop_hook.py` | The Stop hook for Claude Code and Codex (standard library only) |
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
tools without asking. Codex's 10 s default startup timeout is too short (see "Startup time" below).

```toml
[mcp_servers.project-memory]
command = '<python>'
args = ['<repo>/mcp_server.py']
startup_timeout_sec = 60
tool_timeout_sec = 120
default_tools_approval_mode = "approve"

[mcp_servers.project-memory.env]
REDIS_WSL_DISTRO = "Ubuntu"
```

Check it with `claude mcp list` / `codex mcp list`, or with `/mcp` in a session.

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

## Using it

Agents call the tools themselves; the hook covers saving. To nudge searching too, add a line to the
project's `CLAUDE.md` / `AGENTS.md`, such as: *"Before working on a topic, call
`search_project_memory` to recover earlier decisions and constraints."*

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

Keys:
- `project_memory:note:<id>:<n>`: one chunk of a saved note.
- `project_memory:doc:<file id>:<n>`: one chunk of an imported file.
- `project_memory:files`: imported file → digest.
- `project_memory:meta`: model and dimension.

Each chunk is a hash with its text, project, kind, title or section, source, time, embedding and, for
notes, the agent, branch and worktree.

## Startup time and a Windows trap

The server imports sentence-transformers (and torch) and loads the model *before* it starts reading
stdin, so its MCP handshake takes 5–15 s. Loading in the background would deadlock on Windows:
scipy's Fortran runtime, pulled in through scikit-learn, probes stdin while it loads, and that probe
blocks while the MCP transport has a read pending on the same pipe. For the same reason, every
subprocess the server starts gets `stdin=DEVNULL`. If a client gives up connecting, raise its
startup timeout (`MCP_TIMEOUT=60000` for Claude Code, `startup_timeout_sec` for Codex).

## Licences

These are all fine for commercial use:
- **all-MiniLM-L6-v2**: Apache-2.0.
- **sentence-transformers, transformers**: Apache-2.0.
- **torch**: BSD.
- **mcp, redis-py**: MIT.
- **Redis Stack server**: RSALv2/SSPLv1. You may use it for your own work, but you may not offer
  it to others as a hosted database service.
