# AGENTS.md — control plane · `mcp/`

MCP hosting: how an agent's `mcp_tool` node (or an MCP capability wired into an `llm` node's
`tools` port) reaches an external MCP server. This package owns the transports, the persistent
connections, servers generated from an OpenAPI spec or GraphQL endpoint, interactive OAuth, and
browsing public MCP registries ("the hub"). Rules in
[apps/control-plane/AGENTS.md](../../../AGENTS.md) apply first; the design is in the MCP section
of [control-plane.md](../../../../../docs/dev-docs/control-plane.md).

## Where it sits

- **Called by:** [walker.py](../walker.py) (`execute_mcp_tool`, `execute_mcp_connection_tool`,
  `build_capability_schemas`) and its durable twins in [durable/compiler.py](../durable/compiler.py);
  the `/admin/mcp/*`, `/connections/{id}/mcp*`, `/mcp/oauth/callback` and
  `/admin/mcp/catalog*` routes in [app.py](../app.py).
- **Two ways a server is named:** a name-keyed registration (the `mcp_server` table, managed at
  `/admin/mcp/servers/{name}`, rehydrated into `McpManager` on startup), or a `connection` row
  of kind `mcp_server` (encrypted auth, hub installs, generated servers) resolved per call through
  [tool_resolve.py](../tool_resolve.py). Both end in the same `McpClient` protocol.
- **Calls out to:** the MCP server itself (a stdio subprocess on this host, or a remote over
  streamable HTTP or SSE); for generated servers, the upstream API; for the hub, registry REST
  endpoints. The official `mcp` SDK is imported here and nowhere else.
- **Also used by:** [apps/worker](../../../../worker/AGENTS.md), which rehydrates the same
  registry so durable `mcp_tool` steps can spawn servers in the worker's trust domain.

## Map

<!-- agents-map: covers=. -->

| File | What it does |
|------|--------------|
| [`client.py`](client.py) | The `McpClient` protocol and its transports (`StdioMcpClient`, `HttpMcpClient`, `SseMcpClient`) over a shared `_ActorMcpClient`: one dedicated task owns the SDK's context managers, and callers enqueue calls to it. Also `McpServerConfig`, `McpToolDescriptor`, `McpResult`, and the call-timeout resolution. |
| [`manager.py`](manager.py) | `McpManager`: the named-server registry, lazy connect on first use, reuse for the process lifetime, a tool-list cache, one reconnect retry on a transport failure. No eviction or idle teardown. |
| [`factory.py`](factory.py) | `build_client_factory`: the app-assembled factory that adds generated servers and OAuth-authorized connections to what the bare default factory can build. |
| [`generated.py`](generated.py) | OpenAPI and GraphQL turned into an in-process MCP server (fastmcp), reached over an in-memory transport. OpenAPI gives one tool per operation, narrowed by include/exclude globs; GraphQL gives two generic tools. `preview_tools` lists tools without creating anything. |
| [`oauth.py`](oauth.py) | Interactive OAuth 2.1 for remote servers: `DbTokenStorage` (tokens plus the dynamically registered client as one encrypted secret behind the connection's `secret_ref`), `OAuthBroker` (the browser round trip through `/mcp/oauth/callback`), and `worker_oauth_builder` / `non_interactive_handlers` so unattended runs fail fast instead of waiting for a browser. |
| [`registry.py`](registry.py) | The hub: `McpRegistryClient` (paged `/v0.1/servers` listing, TTL cache), pure mappers to `CatalogEntry` and `InstallCandidate`, and `build_install`, which produces the connection config plus a separate secret map. No side effects; installing is the route's job. |

## Invariants specific to this package

- **SDK isolation.** The rest of the control plane imports this package's protocol and
  dataclasses only, never `mcp` or `fastmcp` directly.
- **One task per connection.** The SDK's anyio cancel scopes must enter and exit on the same task;
  never call a session from the request task.
- **No secret in config or IR.** Headers, env values and tokens resolve server-side at call time.
  Hub installs return secrets in a separate map that goes straight into the secret store.
- **Unattended contexts never wait for a human.** An OAuth connection without valid tokens binds
  an error in a trigger or worker run.

## Tests

`tests/test_mcp.py` (stdio against the real fake server in `tests/_fake_mcp_server.py`),
`test_mcp_http.py`, `test_mcp_generated.py`, `test_mcp_hub.py`, `test_mcp_registry.py`,
`test_tool_wiring.py` (MCP as an llm capability).
