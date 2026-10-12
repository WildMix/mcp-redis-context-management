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
Defaults are container `project-memory-work-copy` and Redis port `6380`. Use `--name another-copy
--port 6382` for a second installation or an occupied port. Redis is bound to localhost.

The printed routing-file path belongs to this installation. The default work bundle maps all agent
directories to its one shared project; it contains no personal database. A personal bundle requires
explicit directory/project routes before serving. Restore personal data with a distinct name and
port, for example `python restore.py --name project-memory-personal-copy --port 6381`. Then edit
`project-memory-personal-copy.routes.json` and add one route per project to its `routes` array:

```json
{"root": "C:/projects/my-game", "target": "personal", "project": "<project value from manifest.json>"}
```

Use your new computer's checkout paths and the exact project values in the manifest. Linked worktrees
inherit their main checkout's route. To serve both restored instances from one MCP process, combine
their two targets and all explicit routes into one routing file; remove `default_target` and add
your work checkout root as a route to the work target. Point `MEMORY_ROUTES` to this combined file.

## Start the MCP server

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r runtime/requirements.txt
$env:MEMORY_ROUTES = (Resolve-Path project-memory-work-copy.routes.json).Path
.venv\Scripts\python runtime/mcp_server.py --transport streamable-http --port 8765
```

For a different `--name`, use its generated `.routes.json` file. If using WSL, also set
`$env:REDIS_WSL_DISTRO = 'Ubuntu'` before starting the server. First startup downloads the embedding
model. The server prints its listening address after model warmup; `http://127.0.0.1:8765/health`
reports readiness. Keep this process running while using your agent. Use another HTTP port if 8765
is already occupied.

Register an HTTP MCP server named `project-memory` in your agent, with URL
`http://127.0.0.1:8765/mcp`. Use the port you selected. No credentials are required for this local
endpoint. The tools are `search_project_memory` and `save_to_project_memory`; work saves are rejected.
Tell the agent to pass its existing absolute project directory as `workdir` on every search.
Work searches cover the entire shared corpus, including relationships between repositories.

To test without an MCP client, in a second PowerShell window with the same environment:

```powershell
.venv\Scripts\python runtime/mcp_server.py --query "How is this platform structured?" --workdir (Get-Location).Path
```

On Linux/macOS, use `.venv/bin/python` and `export MEMORY_ROUTES=/absolute/path/to/the.routes.json`;
the restore script and MCP runtime otherwise work the same way.

## Source documents and maintenance

Search results cite portable paths such as `work/library.md`, relative to this extracted directory.
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
