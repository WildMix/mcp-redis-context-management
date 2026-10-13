# Project memory

Use the shared `project-memory` MCP server:

- From any directory, call `list_memory_contexts` and select `context` plus `project` where required.
  Explicit selection needs no checkout; `workdir` remains available for automatic directory routing.
- Call `search_project_memory` before answering, planning or editing in an unfamiliar project area.
  Rephrase missed searches, read cited docs, and check historical notes against current code.
- Record lasting project decisions and their reasons in project documentation. When enabled, the
  background synchronizer imports documentation and saved agent memories automatically. The macOS
  setup leaves synchronization disabled; see [docs/MACOS_SETUP.md](docs/MACOS_SETUP.md).
- `save_to_project_memory` remains optional for lasting knowledge missing from those sources.
  Search for duplicates before saving; no end-of-turn memory review or response is required.

See [README.md](README.md#using-it) for details.
