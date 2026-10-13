# Shared memory on macOS

Use two independent Redis containers and one native Python MCP process. All clients connect to
`http://127.0.0.1:8765/mcp`; the embedding model loads once and stays in that process. The databases
and HTTP endpoint bind only to loopback. Colima supplies Docker on Apple Silicon without a desktop UI.

Automatic startup is at **user sign-in after boot**, using LaunchAgents. It does not run before login
or while the Mac is asleep. This matches the former Windows sign-in setup. The MCP agent waits for
Docker and Redis and retries failed startup; container volumes survive service and VM restarts.

## Install and restore

```sh
brew install colima docker docker-compose python@3.14
colima start --vm-type vz --cpu 2 --memory 4 --disk 30
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
```

Homebrew's Compose plugin needs `/opt/homebrew/lib/docker/cli-plugins` in the
`cliPluginsExtraDirs` array in `~/.docker/config.json` (`/usr/local/lib/docker/cli-plugins`
on Intel). Preserve other Docker settings when adding this array.

Restore each export with the repository's restore script, giving it a distinct container name:

```sh
.venv/bin/python scripts/restore_curated.py --bundle /path/to/work-export --name project-memory-work-redis --port 6380
.venv/bin/python scripts/restore_curated.py --bundle /path/to/personal-export --name project-memory-personal-redis --port 6381
```

The restore verifies checksums and refuses existing containers or volumes. It loads the snapshot
before enabling append-only persistence, then recreates the container and verifies the data again.
Do not run the restore on every startup. Do not start either Compose stack over restored containers.

Copy `memory_routes.example.json` to `memory_routes.local.json`, then combine both targets and
set their routes to this Mac's directories. Give the work target its exported shared project ID.
For automatic directory selection, personal routes need one project ID per checkout; use each ID
from its manifest. Include this tool's checkout as a personal route for automatic setup-note routing. Routes may be
prepared before a checkout exists. Directory selection requires an existing absolute `workdir`;
explicit `context`/`project` selection needs no checkout or route.
Linked worktrees inherit their main checkout's route. Without explicit context selection, unmapped
directories fail without searching another project or database. Restart the MCP agent after editing routes.

Keep the work target `read_only: true`, as in the exports. Personal targets may use
`read_only: false` for explicit MCP saves. Work notes remain blocked even if a client tries to save.

Create the ignored `redis-containers.local.json`:

```json
["project-memory-work-redis", "project-memory-personal-redis"]
```

Download and warm the same model used by the exports, then install automatic startup:

```sh
.venv/bin/python -c 'from memory_store import load_model, encode; encode(load_model(), ["Project memory startup"])'
colima stop
brew services start colima
.venv/bin/python scripts/install-macos-autostart.py --dry-run
.venv/bin/python scripts/install-macos-autostart.py
```

The installer validates routing and existing container names, creates
`~/Library/LaunchAgents/local.project-memory.mcp.plist`, and starts it immediately. It preserves the
venv interpreter path, uses an explicit Docker context (`colima` by default), and loads the cached
model offline at sign-in. Reinstall after moving the checkout or changing the interpreter/port.
`--docker-context` can select another already-running Docker provider.
Stopping the manually started VM before registering Colima lets its foreground service own the
running VM. Wait for Docker to become ready before running the MCP installer.

## Connect clients

Register the same HTTP server globally in each client:

```sh
codex mcp add project-memory --url http://127.0.0.1:8765/mcp
claude mcp add --transport http --scope user project-memory http://127.0.0.1:8765/mcp
```

Set `startup_timeout_sec = 60` and `tool_timeout_sec = 120` in Codex's
`[mcp_servers.project-memory]` table. Existing registrations should be backed up and replaced
selectively. Restart existing client sessions after registration. These are the supported
[Codex HTTP settings](https://learn.chatgpt.com/docs/extend/mcp) and
[Claude Code user-scope settings](https://code.claude.com/docs/en/mcp).

Agent instructions should require searching project memory before unfamiliar work. From any directory,
call `list_memory_contexts()` and pass `context="work"`, or `context="personal"` with the desired
`project`. No `workdir` is needed. For automatic directory selection, pass the current absolute project
directory as `workdir`. Record decisions in project documentation and save lasting personal notes
explicitly when appropriate. Allow `mcp__project-memory__list_memory_contexts` in Claude's permissions
alongside the search/save tools if those are explicitly allowed.
For mixed work/personal questions, use the discovery tool's document names to map topics, then query
each relevant scope separately with focused questions and combine the cited results. A multi-topic
request is not a reason to force the user to choose only one context.

## Synchronization is disabled

This setup installs **no synchronizer, watcher, import hook or scheduled publication**, and does not
create `sync_projects.json`. Routing only selects a database; it never scans a project. Manual MCP
saves are independent of project synchronization. Changes to source documents are not automatically
imported. The curated importer rejects broad legacy synchronization into these restored corpora.

Keep exports, private source documents, routing, container lists, downloaded models and database
snapshots out of Git. Only generic installation code and documentation should be committed.

## Inspect, restart and stop

```sh
brew services list
launchctl print gui/$(id -u)/local.project-memory.mcp
curl --fail http://127.0.0.1:8765/health
docker --context colima ps
claude mcp list
codex mcp list
```

Logs are in `~/Library/Logs/project-memory/mcp-server.stdout.log` and `mcp-server.stderr.log`.
The health endpoint confirms the shared model is loaded; a successful MCP search also checks Redis.
Colima's service lifecycle is documented in its [official FAQ](https://github.com/abiosoft/colima/blob/main/docs/FAQ.md).

Restart the shared process after code or route changes:

```sh
launchctl kickstart -k gui/$(id -u)/local.project-memory.mcp
```

To disable startup without deleting data:

```sh
launchctl disable gui/$(id -u)/local.project-memory.mcp
launchctl bootout gui/$(id -u)/local.project-memory.mcp
brew services stop colima
colima stop
```

Reinstall the MCP agent and run `brew services start colima` to enable startup again. Back up both
Redis databases using the repository's snapshot instructions before moving or removing Colima.

## Validation

On Apple Silicon with macOS 26.6 and Python 3.14.8, the pinned dependencies installed unchanged.
The full suite with `MEMORY_RUN_INTEGRATION=1`, `MEMORY_TEST_REAL_MODEL=1` and the cached model ran
102 tests: 98 passed, with four Windows-only tests skipped. This includes disposable Redis tests,
real HTTP clients, document-resource discovery, context selection without checkouts, model reuse and project isolation. Launcher
tests cover unavailable Docker, container startup failure, unavailable Redis and paths containing spaces.
