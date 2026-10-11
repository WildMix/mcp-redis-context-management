# Instructions for coding agents

## Project memory: search before you answer

This project has a shared long-term memory, the `project-memory` MCP server. Every agent (Codex,
Claude Code) in every worktree reads and writes the same memory

**Zero-guessing policy.** You do not hold all of this project's context. When you are asked about a
design pattern, feature, past implementation, earlier session or decision that you don't
immediately know, or can't see in the files you have open, you must not guess, and you must not
answer that you lack context. First call the `search_project_memory` tool of the `project-memory`
MCP server, then answer from what it returns.

- Search before planning or editing code in an area you haven't worked on in this session.
- If a search misses, rephrase it (other words, the feature's or file's name) and search again
  before concluding that nothing is recorded.
- Each result says where it comes from. For a doc, open the file at the given lines. A note is
  history: check it against the current code, and when they disagree, the code wins.
- If the tool is unavailable (the server isn't connected), say so plainly and fall back to reading
  the repository. Never present a guess as project knowledge.

**Saving.** Save with `save_to_project_memory` only knowledge about this project that is:
- **new**: not already in the memory, and not plain from the code, commits or docs;
- **lasting**: a decision and its reasons, a rejected alternative, a constraint, a gotcha, a
  non-obvious cause, a known limitation or unfinished piece.

Logs of edits, work on other repositories and work on your tools don't belong there; when in doubt,
don't save. When a turn changes the project's files, a Stop hook asks you to make this check. If
nothing qualifies, which is common, reply "Nothing to save to project memory."