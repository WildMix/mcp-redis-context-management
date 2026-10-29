# External MCP context sources

`project-memory` can query other MCP servers while searching Redis. Configure
sources per context, optionally narrowed to project IDs. Agents keep using
`search_project_memory`; the service handles the upstream connections, arguments
and response normalization. `get_context_document(source, id, ...)` optionally
retrieves a longer page using the same context/project selectors.

Sources are live reference evidence, not automatically verified facts. Their URLs,
versions and timestamps help an agent assess discrepancies with saved memories.
An external read does not save a note, import a document, create a Redis revision,
or enable background synchronization. Explicit memory saves retain their existing
behavior. External page versions and Redis memory revisions are independent.

## Source contract, version 1

Every adapter implements these async operations:

```python
search(query: str, limit: int) -> list[SearchResult]
fetch(id: str) -> SourceDocument
```

The built-in `contract` adapter calls two configured MCP tools. By default:

| Tool | Arguments | Response JSON |
| --- | --- | --- |
| `context_search` | `{"query": "natural-language question", "limit": 3}` | `{"results": [SearchResult, ...]}` |
| `context_fetch` | `{"id": "stable-source-id"}` | `SourceDocument` |

Responses may use MCP `structuredContent` or JSON in text content. Errors use MCP
`isError`. No free-form prose, embedded resources, images or tool-selection
instructions are accepted as the response contract. Search results must have
unique IDs and respect `limit`. Fetch must return the requested ID and the latest
accessible document. Both operations must be free of external mutations.

```typescript
type SearchResult = {
  id: string;                 // Stable within this source, 1–512 characters
  title: string;              // 1–1,000 characters
  url: string | null;         // Absolute HTTP(S) citation URL
  excerpt: string;            // Plain text, at most 4,000 characters
};

type SourceDocument = {
  id: string;
  title: string;
  url: string | null;
  content: string;            // Plain text, at most 12,000 characters
  version: string | null;     // Opaque source revision, at most 128 characters
  updatedAt: string | null;   // ISO 8601 timestamp with timezone
  truncated?: boolean;       // Default false; true if content is only a prefix
};
```

All fields except `truncated` are required; unavailable metadata uses `null`.
Extra fields and type coercions are rejected. These are executable Pydantic
contracts in `memory_sources.py`; `SearchResponse.model_json_schema()` and
`SourceDocument.model_json_schema()` expose their JSON schemas. Upstream JSON
payloads over 256,000 characters are rejected after protocol decoding. This is an
accepted-response bound, not a transport-level streaming limit.

The service adds its own source ID and retrieval time. A document is identified by
the pair `(source, id)`, not its title or URL. External results retain source
ranking; their scores are not compared to Redis cosine similarity.

## Configuration and scope

Copy `memory_sources.example.json` to a private local configuration and set
`MEMORY_SOURCES` to its absolute path in the server environment. Restart the
service after changes. With the variable absent, searches use Redis alone.

Each entry specifies `id`, `context`, optional `projects`, an `adapter`, connection
settings, two tool names and their explicit `allowed_tools` list. Empty `projects`
means every project in the selected context. Fixed-project contexts accept only
their own project. Unknown contexts, duplicate source IDs, extra configuration
fields and invalid allowlists fail configuration loading. Without `MEMORY_ROUTES`,
use `context: ""` and a nonempty list of explicit project IDs.

Connections support:

- `streamable-http`: `url`, optional `headers_file` (a private JSON string map),
  and `headers_from_environment` (header name to environment-variable name).
  For example, `WIKI_AUTHORIZATION` contains the complete `Bearer ...` value.
- `stdio`: `command`, optional `args`, `cwd`, `env`, `env_file` (a private JSON
  string map), and `env_from_environment` (child variable to server variable).
  Commands execute directly without a shell. Only the SDK's minimal environment
  plus explicitly supplied variables reaches the child process.

Relative file paths and `cwd` resolve against the source configuration directory.
Keep credentials in owner-readable local files or environment variables. The
repository ignores `memory_sources.local.json` and
`memory_sources.secrets.local.json`. Exported corpus bundles include the runtime
module but never these connection settings or credentials. The context catalog
lists source IDs and project scopes without connection details.

Sessions are opened on demand and closed after each operation. There is no daemon
pool, crawler, persistent cache, new model or synchronization job. Each source has
a total request budget (`timeout_seconds`, default 15, range 1–30), including
connect/search/fetch; transport teardown may add a short cleanup interval.

## Confluence adapter

The `confluence` adapter supports the `@atlassian-dc-mcp/confluence` tool contract
(verified with 0.38.0). It converts the question into a bounded keyword CQL query,
then retrieves matching page bodies as text. If the initial search is empty, it
tries one relaxed query anchored to the first topic, preferring acronyms. This is
a lexical retrieval heuristic; phrasing can affect recall. It does not claim an
exhaustive semantic search of Confluence.

```json
{
  "schema": 1,
  "sources": [{
    "id": "company-wiki",
    "context": "reference",
    "projects": ["platform"],
    "adapter": "confluence",
    "connection": {
      "transport": "stdio",
      "command": "/absolute/path/to/node",
      "args": ["/absolute/path/to/confluence/build/index.js"],
      "env_file": "memory_sources.secrets.local.json"
    },
    "search_tool": "confluence_searchContent",
    "fetch_tool": "confluence_getContent",
    "allowed_tools": ["confluence_searchContent", "confluence_getContent"],
    "timeout_seconds": 20,
    "limit": 3
  }]
}
```

The private environment file supplies the installed Confluence server's
`CONFLUENCE_HOST` and `CONFLUENCE_API_TOKEN`. Use a separately installed/pinned
upstream executable; it need not be the process used by an agent's direct
connection. Optional `spaces` restricts CQL to the named space keys and checks the
space again when fetching a page. Fetched pages must have current status; no
attachments, archived pages or page history are imported by this adapter.

## Enforced read boundary

Every upstream `tools/call` passes through `ReadClient.call`, which rejects tools
outside the exact configured search/fetch pair before contacting the upstream.
The Confluence adapter additionally fixes those names to
`confluence_searchContent` and `confluence_getContent`. Adding a Confluence write
tool to its allowlist is a configuration error. No agent or retrieved document
can supply a tool name, connection URL or credential override.

`readOnlyHint` does not grant access. The integration does not expose upstream
tools to agents, forward upstream server instructions, or implement sampling,
elicitation or filesystem-root callbacks. Retrieved text is labeled as source
material, not instructions. Upstream errors and stderr are not relayed verbatim
because they can contain credentials or document bodies.

Configuration, adapters and upstream implementations are trusted operator code:
a generic tool must actually honor the read contract. An allowlist cannot make
a misimplemented or malicious allowed tool read-only. The restriction applies
to context gathering through this service. An agent's independent Confluence
connection retains its existing write tools and permissions.

## Results and failures

Redis's three logical memory results are preserved. External sources run alongside
that search, with up to eight configured sources and 1–5 results per source
(default three). Search fetches those pages and includes a 4,000-character preview
per page; truncation is explicit. `get_context_document` returns up to 12,000
characters and reports whether the upstream page was itself truncated. For larger
pages, use the citation URL or the agent's direct source tools.

Empty results differ from failures. Errors such as `timeout`, `upstream_error`,
`invalid_response` and `credentials_unavailable` appear with the source ID. A page
fetch failure preserves its search excerpt. Source outages preserve local results;
a Redis outage is also explicit and does not suppress available external evidence.
Invalid context/project selection fails before any outbound source call.

Validate the generic contract, tool restrictions and real stdio transport with:

```sh
.venv/bin/python -m unittest tests.test_sources -v
MEMORY_RUN_INTEGRATION=1 .venv/bin/python -m unittest tests.test_sources -v
```

The second command also exercises a disposable local HTTP MCP peer. Tests use
synthetic documents and never contact Confluence or modify the live Redis corpus.
