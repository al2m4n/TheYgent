# MCP servers

An **MCP server** gives your agents tools that live outside TheYgent — a filesystem browser, a database client, a company API, a hosted search service. MCP (the Model Context Protocol) is the open standard those servers speak. You set a server up once on the **MCP** page in the sidebar, and from then on any agent can call its tools through the [mcp_tool node](../building/nodes/mcp.md), either at a fixed step in the graph or autonomously, whenever the model decides to.

The MCP page covers the whole story: **browse and install** published servers from public hubs, **add your own** four different ways (a local process, a remote endpoint, an OpenAPI spec, a GraphQL endpoint), **sign in** to OAuth-protected servers from the browser, and manage everything in one list.

Servers run in **your trust domain** — the same posture as your inference engines. Local servers are subprocesses on your machine, remote servers are reached directly from your control plane, and credentials are stored encrypted on your side. Nothing transits anyone else's servers on the way.

## One server list

Every server lands in the same list, whether you defined it by hand or installed it from a hub. Each row shows the server's **name**, a **transport badge** — `stdio` (local subprocess), `http` / `sse` (remote), `openapi` / `graphql` (generated in-process) — a **connected/idle** badge, a lock icon when the server holds an encrypted secret, and, for hub installs, an **origin badge** naming the registry and version it came from.

The actions per row:

| Action | What it does |
|---|---|
| **Tools** | Connect if needed and list the tools the server reports, with names and descriptions. |
| **Connect** | Only on OAuth servers: run the browser sign-in (see [below](#authorize-with-oauth-sign-in)). |
| **Warm** | Start and connect the server now, instead of on first use. |
| **Close** | Disconnect. The definition is kept; the server reconnects lazily on the next call. |
| **Rotate secret** | On connections with a pasted credential: write a new secret in place (write-only — the old value is never shown). No agent changes. OAuth rows re-authorize with **Connect** instead. |
| **Delete** | Remove the server entirely (a confirmation dialog stands in the way). |

A search box and transport/status filter chips help when the list grows. Below the list, the **tool tester** runs a single tool through a throwaway one-node graph so you can check a tool works before wiring it into an agent.

Agents reference every kind of server the same way: an `mcp_tool` node picks a server (by name) or a connection, and the node's **tool** field is a searchable picker over the tools that server actually exposes. See the [mcp_tool node reference](../building/nodes/mcp.md).

## Installing from a hub

**Browse hubs** opens a catalog over MCP registries — public directories where server authors publish how their server is launched or reached. Two are built in:

| Hub | What it is |
|---|---|
| **Official MCP Registry** | The community registry at `registry.modelcontextprotocol.io`. |
| **GitHub MCP Registry** | GitHub's registry of MCP servers. |

You can add your own with the `THEYGENT_MCP_REGISTRIES` environment variable — a JSON array of `{"id", "label", "url"}` objects. Any registry that speaks the standard subregistry API works, and a registry you host doubles as an **allowlist**: in an air-gapped or policy-controlled deployment, point the browse surface at your own mirror instead of the public hubs. A malformed value is a loud startup error, never a silently dropped registry.

Pick a hub, search, and read the entries: each shows its description, version, transport and package badges, and a star count where the publisher provides one. A **deprecated** entry carries a visible warning with the publisher's message — prefer whatever replacement it names.

!!! note "Cold searches can take a while"
    The official registry's substring search can take fifteen seconds or more on a cold query. TheYgent waits generously rather than timing out — a slow first search is the hub warming up, not the hub being down. Repeat searches are cached and fast.

### Installing an entry

Expanding an entry shows every launchable form it publishes: a **package** spawned locally over stdio (launched with `npx`, `uvx`, `docker`, or `dnx`, depending on the package type), or a **remote** reached over streamable-HTTP or SSE. Pick one and press **Install**:

- The form is generated from the server's **own declared inputs** — required fields are marked, declared choices become dropdowns, and secret-marked values are entered as passwords.
- The **connection name** is how agents will reference the server.
- **Secrets are stored encrypted server-side and never shown again.** A hub install with secrets is exactly a [connection](../building/nodes/tools.md#authentication-with-connections-and-secrets) with an encrypted secret — rotating the value later is a connection edit, and no agent version changes.
- A remote entry that supports OAuth offers **"Sign in with the provider instead"** — install first, then press **Connect** on the new row to authorize in the browser.

The installed server appears in the list with its origin badge, and the catalog marks the entry as installed so you don't install it twice.

## Adding a server by hand

**Add server** offers four kinds. All four create a connection: the non-secret settings are stored plainly, anything secret is encrypted and write-only.

### Stdio — a local subprocess

A server that runs as a process on your machine, in your trust domain.

| Field | Notes |
|---|---|
| **Name** | What agents reference, e.g. `filesystem`. |
| **Command** | The executable to launch, e.g. `npx`. |
| **Args** | Whitespace-separated arguments. |
| **Env** | Non-secret `NAME=value` lines, passed into the subprocess. |
| **Secret env** | Secret `NAME=value` lines — stored encrypted, injected into the subprocess at launch, never shown again. |
| **Working dir** | Optional; the directory the process starts in. |

### Remote — an HTTP or SSE endpoint

A server someone else hosts, reached over streamable-HTTP or SSE. Give it a name, the URL, any non-secret headers, and an auth type:

| Auth | You supply | What TheYgent sends |
|---|---|---|
| **None** | — | plain requests |
| **Bearer token** | the token | `Authorization: Bearer <token>` |
| **API key header** | a header name + the key | the key in that header |
| **Basic** | username + password | `Authorization: Basic base64(user:password)` |
| **Header map** | whole headers, one `NAME=value` per line | every header verbatim |
| **OAuth2 client credentials** | token URL, client id, optional scope, client secret | fetches a token, sends it as a bearer |
| **OAuth sign-in** | nothing to paste | you authorize in the browser after creating — see [below](#authorize-with-oauth-sign-in) |

Whatever the type, the credential itself is stored encrypted and turned into request headers server-side — it never appears in the stored config or in any agent document.

### OpenAPI — generate tools from a spec

If you already have a REST API, you don't have to write an MCP server for it. Paste an OpenAPI spec (JSON) or give its URL, set the base URL and upstream auth, and **every operation in the spec becomes a callable tool**, named by its `operationId`. The server is hosted in-process — no subprocess, no socket; only the upstream API calls leave, carrying the auth that was resolved server-side.

Press **Preview** first: it shows exactly the tool list an agent would see, and **Create** is enabled only once the spec derives cleanly.

!!! warning "A relative `servers` entry needs an explicit base URL"
    Many specs declare a relative server URL (say `/api/v3`), which only means something relative to wherever the spec lives. When you give the spec by URL, TheYgent resolves the relative path against the spec's own origin. A **pasted** spec has no origin — give an explicit **Base URL** or the derived tools will have nowhere to call.

### GraphQL — generate tools from an endpoint

Give a GraphQL endpoint and its auth, and the server derives exactly **two tools**: `introspect_schema` (returns the schema) and `run_query` (runs a query the model writes). Queries are parsed and validated locally against the introspected schema before anything goes upstream; mutations are rejected unless you turn on **Allow mutations**, and subscriptions always are. Two generic tools rather than one per field is deliberate — the model reads the schema and writes real queries, including nested selections a per-field tool couldn't express. **Preview** confirms the endpoint introspects before you create.

## Authorize with OAuth (sign in)

Many hosted MCP servers use OAuth: there is no token to paste, because the server wants *you* to sign in and consent. TheYgent handles this without any manual configuration:

1. Create the connection with the **OAuth sign-in** auth type (Remote form), or check **"Sign in with the provider instead"** when installing from a hub.
2. Press **Connect** on the server's row. A browser tab opens the provider's consent page.
3. Approve. The tab tells you it's done; back on the MCP page, the row shows connected.

TheYgent stores the tokens **encrypted** in your own database and refreshes them automatically — you should only see the consent page again if the provider revokes access or the grant fully expires. Authorizing (or re-authorizing) never changes any agent's version: the tokens live behind the connection's stable secret reference.

Unattended runs never hang waiting for a login. If a server needs re-authorization during a triggered or scheduled run, the tool call **fails immediately** with a message telling you to reconnect the server on the MCP page — the node binds its `err` port and the run carries on, per the normal [tool error contract](../building/nodes/tools.md#the-success-error-contract). A browser flow only ever starts because you pressed Connect.

## Using a server in an agent

Drop a **Tool** node in the [editor](../building/editor.md), choose **MCP** in the Kind picker, and pick the server — a name-keyed server or a connection, whichever the list shows. The **tool** field is a searchable dropdown over the tools that server actually reports (it degrades to free text if the server can't be reached, so authoring is never blocked). Wire the node as a fixed **step**, or wire its violet `use` handle into an llm's `tools` port to make it a **capability** the model calls on its own — see the [mcp_tool node reference](../building/nodes/mcp.md) for ports, config, and the error contract.

## Troubleshooting

- **A hub entry is marked deprecated.** The publisher has retired it. The warning shows their message, which usually names a successor — install that instead. An already-installed deprecated server keeps working, but don't build new agents on it.
- **An OpenAPI server derives tools but every call fails.** Almost always a relative `servers` entry with no base URL — edit the connection (or recreate it) with an explicit base URL. Preview shows the upstream URL it will use; check it's absolute.
- **The hub search feels stuck.** Cold queries against the official registry can take ~15 seconds. Wait it out once; the page is cached after that. A genuinely unreachable registry comes back as a clear error, not a spinner.
- **A run reports "authorize this connection".** The server's OAuth grant needs renewing — open the MCP page and press **Connect** on its row. Runs never open a browser themselves.
- **You need to rotate a secret from a hub install.** It's an ordinary connection: press **Rotate secret** on its row and write the new value. The old one is never displayed (write-only), and rotating changes no agent version.

## Related pages

- [Example agents](examples.md) — one runnable agent per server kind, plus capability mode
- [The mcp_tool node](../building/nodes/mcp.md) — ports, config, step vs capability
- [Tool nodes](../building/nodes/tools.md) — connections and the tool error contract
- [API reference](../reference/api.md) — the `/admin/mcp/*` and `/connections/{id}/mcp*` endpoints
- [Environment variables](../reference/environment.md) — `THEYGENT_MCP_REGISTRIES`, `THEYGENT_MCP_CALL_TIMEOUT_S`
