# Project memory over MCP (Redis vector search)

A shared long-term memory for every agent working on a project. `migrate_memory.py` chunks the
project's Markdown (docs, handoffs, agent memory files) and stores the chunks with local
`all-MiniLM-L6-v2` embeddings in a Redis Stack vector index. `mcp_server.py` is a stdio MCP server
with one tool, `search_project_memory(query)`, that returns the 3 closest chunks with file, line
range and section. Claude Code and Codex both launch it, so every session in every worktree
searches the same memory.

| File | Role |
| --- | --- |
| `docker-compose.yml` | Redis Stack (Redis + RediSearch + RedisInsight) on `127.0.0.1:6379`, data in a volume |
| `requirements.txt` | `mcp`, `redis`, `sentence-transformers` (+ torch), `numpy` |
| `migrate_memory.py` | Index the `.md` files of the given directories (default: `sources.txt`); incremental |
| `mcp_server.py` | The MCP server; `--query "..."` runs one search in the terminal |
| `memory_redis.py` | Settings and the Redis connection both scripts share (keeps WSL up, see below) |
| `sources.txt` | The directories to index: the three bodhidharma worktrees and their Claude memory |

## 1. Start Redis

Docker on this machine runs inside WSL (Ubuntu), so compose runs there. Port 6379 still reaches
Windows (`networkingMode=mirrored`).

```powershell
wsl -d Ubuntu --cd /mnt/c/Users/e562225/mpc-redis-context-management -- docker compose up -d --wait
```

(With Docker Desktop it is just `docker compose up -d --wait`.) RedisInsight, to browse the
index, is at <http://localhost:8001>. The container restarts with Docker
(`restart: unless-stopped`), and the data survives in the `redis-data` volume (append-only file).

**WSL stops an idle distro a few seconds after its last session, and Redis stops with it.** Set
`REDIS_WSL_DISTRO=Ubuntu` (as the configs below do) and each script holds the distro open while it
runs, waiting up to 60 s for Redis to come back. Without it, start Redis by hand before using the
memory.

## 2. Install and index

```powershell
cd C:\Users\e562225\mpc-redis-context-management
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
$env:REDIS_WSL_DISTRO = "Ubuntu"
.venv\Scripts\python migrate_memory.py            # everything in sources.txt
.venv\Scripts\python migrate_memory.py C:\some\docs  # or chosen directories
```

The first run downloads the model (about 90 MB, cached by Hugging Face) and embeds about 3,000
chunks in 2–3 minutes on CPU. After that, runs are incremental:
- unchanged files are skipped (a second or two, without loading the model);
- edited files are re-chunked;
- files deleted from an indexed directory leave the index.

Inside a git checkout only files git does not ignore are read, so `external/`, `build/`, `dist/`
and `.tools/` stay out. Use `--reset` to rebuild from scratch, which is required after changing
`EMBEDDING_MODEL`.

Try a search without any client:

```powershell
.venv\Scripts\python mcp_server.py --query "how is the fight stance animated?"
```

The three worktrees hold copies of the same docs. A result that appears in several of them is shown
once, with `same text in:` listing the other copies. A doc that exists only on one branch shows up
under that worktree's path.

## 3. Connect Claude Code

Register it at user scope, so it is available in all three worktrees (and every other project):

```powershell
claude mcp add --scope user project-memory -e REDIS_WSL_DISTRO=Ubuntu -- C:\Users\e562225\mpc-redis-context-management\.venv\Scripts\python.exe C:\Users\e562225\mpc-redis-context-management\mcp_server.py
claude mcp list
```

In a session, `/mcp` shows it connected; the tool is `mcp__project-memory__search_project_memory`.
The server loads the model before it answers the handshake, which takes 5–15 s (see the comment in
`embedding_model()`). If Claude Code times out connecting, start it with `MCP_TIMEOUT=60000`.

## 4. Connect Codex

Add this to `C:\Users\e562225\.codex\config.toml`. `codex mcp add` works too, but it cannot set the
timeouts, and Codex's 10 s startup default is too short for loading torch.

```toml
[mcp_servers.project-memory]
command = 'C:\Users\e562225\mpc-redis-context-management\.venv\Scripts\python.exe'
args = ['C:\Users\e562225\mpc-redis-context-management\mcp_server.py']
startup_timeout_sec = 60
tool_timeout_sec = 120

[mcp_servers.project-memory.env]
REDIS_WSL_DISTRO = "Ubuntu"
```

`codex mcp list` should show it. In the TUI, `/mcp` lists its tool.

## 5. Make agents use it

A tool is only called when the agent thinks of it. Add a line like this to the project's
`CLAUDE.md` and `AGENTS.md`:

> Before working on a topic, call `search_project_memory` to recover earlier decisions, constraints
> and handoffs. After adding or editing docs or memory files, run
> `C:\Users\e562225\mpc-redis-context-management\.venv\Scripts\python.exe C:\Users\e562225\mpc-redis-context-management\migrate_memory.py`
> (with `REDIS_WSL_DISTRO=Ubuntu`) so the other agents see them.

## Settings (environment variables)

| Variable | Default | |
| --- | --- | --- |
| `REDIS_URL` | `redis://localhost:6379/0` | |
| `MEMORY_INDEX` | `project_memory` | index name and key prefix: one index per project if you add more |
| `EMBEDDING_MODEL` | `sentence-transformers/all-MiniLM-L6-v2` | 384 dims; the server refuses an index built with another model |
| `REDIS_WSL_DISTRO` | unset | WSL distro running Docker, held open while a script runs |

Keys: `project_memory:chunk:<file id>:<n>` holds one chunk (content, heading, source, lines,
embedding). `project_memory:files` maps each source to its digest. `project_memory:meta` records
the model and dimension.

## Licences

These are all fine for internal commercial use:
- **all-MiniLM-L6-v2**: Apache-2.0.
- **sentence-transformers, transformers**: Apache-2.0.
- **torch**: BSD.
- **mcp, redis-py**: MIT.
- **Redis Stack server**: RSALv2/SSPLv1. You may use it for your own work, but you may not offer
  it to others as a hosted database service.
