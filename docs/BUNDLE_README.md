# Local curated project memory

This bundle contains a Redis snapshot, the selected Markdown and manifest, and a small MCP runtime.
The domain, shared project, counts, image digest and embedding model are recorded in `bundle.json`.
Each restored copy is independent. No external Redis service or automatic synchronization is needed.

## Prerequisites

- Docker with Linux containers (Docker Desktop or Docker inside WSL).
- Python 3.11 or later with support for the pinned packages in `runtime/requirements.txt`.
- Internet for the first installation of the pinned Docker image, Python packages and embedding model.
  The ZIP contains the database and application source, not those downloaded dependencies.

## Restore on Windows

Extract the ZIP to a directory you want to keep. Open PowerShell there:

```powershell
python restore.py
```

If Docker runs inside the Ubuntu WSL distribution, use `python restore.py --wsl-distro Ubuntu`.
The script verifies bundle checksums, refuses existing containers/volumes, restores to a fresh volume
with AOF disabled, enables AOF, then recreates the container and verifies that the data survived.
Defaults are container `project-memory-copy` and Redis port `6380`. Use `--name another-copy
--port 6382` for a second installation or an occupied port. Redis is bound to localhost.

The printed routing-file path belongs to this installation. The context name defaults to the
bundle's corpus label; override it with `--context <name>`. All restored targets default to
`read_only: true`; use `--writable` to permit explicit MCP saves, or edit `read_only` later and
restart the server. Labels and context names never determine write permissions.

A bundle exported with a fixed project can be selected without a project argument. Otherwise pass
`project` explicitly. For optional automatic directory selection, add routes using your checkout
paths, context name and exact project values from the manifest:

```json
{"root": "C:/projects/my-game", "target": "projects", "project": "game"}
```

Linked worktrees inherit their main checkout's route. Restore additional corpora with distinct
container names and ports. To serve them from one MCP process, combine their targets and routes
in one routing file and remove `default_target`. Keep existing container names and volumes when
upgrading; the generic example names do not rename existing installations.

## Start the MCP server

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r runtime/requirements.txt
$env:MEMORY_ROUTES = (Resolve-Path project-memory-copy.routes.json).Path
.venv\Scripts\python runtime/mcp_server.py --transport streamable-http --port 8765
```

For a different `--name`, use its generated `.routes.json` file. If using WSL, also set
`$env:REDIS_WSL_DISTRO = 'Ubuntu'` before starting the server. First startup downloads the embedding
model. The server prints its listening address after model warmup; `http://127.0.0.1:8765/health`
reports readiness. Keep this process running while using your agent. Use another HTTP port if 8765
is already occupied.

Register an HTTP MCP server named `project-memory` in your agent, with URL
`http://127.0.0.1:8765/mcp`. Use the port you selected. No credentials are required for this local
endpoint. Call `list_memory_contexts` to discover contexts/projects, then `search_project_memory`
with a discovered `context` and `project` when required, from any directory.
`save_to_project_memory` accepts the same selectors and requires a writable target. Without selectors,
pass an existing absolute project directory as `workdir` for automatic routing.
Fixed-project searches cover the entire shared corpus, including relationships between repositories.

To test without an MCP client, in a second PowerShell window with the same environment:

```powershell
.venv\Scripts\python runtime/mcp_server.py --query "How is this platform structured?" --workdir (Get-Location).Path
```

On Linux/macOS, use `.venv/bin/python` and `export MEMORY_ROUTES=/absolute/path/to/the.routes.json`;
the restore script and MCP runtime otherwise work the same way.

## Source documents and maintenance

Search results cite portable paths such as `library.md`, relative to the manifest’s corpus root.
The snapshot already includes their text and embeddings; repositories need not be cloned to search.
Treat the Markdown as a curated architectural reference. Review it against current code when design
changes; a durable reference is not a guarantee that every future implementation remains unchanged.
Automatic updates and raw agent-history import are intentionally absent from this bundle.

To publish reviewed changes, update the selected files and their SHA-256 values in `manifest.json`,
then run `runtime/import_curated.py manifest.json --redis-url redis://127.0.0.1:6380/0` with the bundle's
Python environment. The validator requires one complete H2 topic per embedding chunk. Publishing
replaces the selected corpus, including removed topics; keep the full selection in the manifest.
Keep the configured model/index unchanged unless performing a deliberate revalidated migration.

Keep this ZIP and extracted corpus within the intended audience. The database snapshot contains the
whole isolated instance. Neither the ZIP nor its source documents belong in the generic tool's Git repo.
