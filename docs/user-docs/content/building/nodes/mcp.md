# MCP tools

An **mcp_tool** node calls one tool on an external MCP server. MCP — the Model Context Protocol — is an open standard for exposing tools (a filesystem browser, a database client, a computer-vision model, a company API) to an agent. Where a [built-in or REST tool](tools.md) is defined inside TheYgent, an MCP tool lives in a separate server process that you register once and then reference from any agent. It is an **activity** node.

## Where the node comes from

`mcp_tool` is not a separate item in the node palette. To add one, drop a **Tool** node onto the canvas and choose **MCP** in the inspector's **Kind** picker. (Agents you import that already contain `mcp_tool` nodes still render normally.)

## Ports

Same shape as the other tool nodes.

| Port | Direction | Required | Description |
|---|---|---|---|
| `in` | in | yes* | The node's input, used when the tool runs as a step. *Left unfed when the node is a capability (exempt from the required-port check). |
| `out` | out | — | The tool's return value on success. |
| `err` | out | — | The error message when the call fails. Its port type is `error`. |
| `use` | out | — | The violet capability handle. Wire it into an llm's `tools` port to let the model call the tool. |

## Configuration

| Field | Type | Default | Description |
|---|---|---|---|
| `tool` | string | *(required)* | The tool name the server exposes. |
| `server` | string | `null` | The registered MCP server's name (from the MCP page). Set **this or** `connection`, never both. |
| `connection` | string | `null` | The id of an `mcp_server` connection. Set **this or** `server`, never both. |
| `args` | object | `{}` | Arguments for the tool in step mode. `$in` references are allowed. In capability mode the model supplies them. |
| `description` | string | `null` | An optional "use this when…" hint for the model in capability mode. MCP tools already describe themselves through the server, so this is a nudge, not a requirement. |

You must set **exactly one** of `server` or `connection`. In the inspector, picking one clears the other automatically. Use `server` for a plainly-registered server; use `connection` when the server needs authentication supplied from an encrypted secret (see [Secrets in server config](#secrets-in-server-config)).

## The MCP page

Register and manage servers from the **MCP** page in the sidebar. It has two parts: the server registry at the top and a tool tester below it.

### Define a server

Click **Define server**. The form registers a **stdio** server — one that runs as a local subprocess:

| Field | Placeholder | Notes |
|---|---|---|
| **Name** | `filesystem` | The logical name you reference from `mcp_tool` nodes. |
| **Command** | `npx` | The executable to launch. Required. |
| **Args** | `-y @modelcontextprotocol/server-filesystem /tmp` | Whitespace-separated arguments. |
| **Env** | `API_KEY=…` | One `KEY=value` per line, passed into the spawned process. Values are never logged. |
| **Working dir (optional)** | `/path/to/cwd` | The directory the process starts in. |

**Save server** is enabled once Name and Command are filled in. Registrations persist across restarts (the connection itself stays lazy — the process is not spawned until the server is first used).

### Manage a registered server

Each row shows the server's name, a transport badge (`stdio` or `http`), and a connected/idle badge. The actions are:

- **Tools** — connect if needed and list the tools the server reports, each with its name and description.
- **Warm** — start the server process and connect now, instead of on first use.
- **Close** — stop the server process. The registration is kept, and the server reconnects lazily on the next call.
- **Delete** — remove the registration. This is irreversible: the command, args, and env cannot be recovered, so it goes through a confirmation dialog.

A search box and transport/status filters help when you have many servers.

### The tool tester

Below the registry, the tool tester runs a single tool through a throwaway one-node graph so you can check it works before wiring it into an agent. When a tool returns image detections (bounding boxes with labels and scores), they are drawn over the image — handy for computer-vision servers.

### HTTP (remote) servers

The **Define server** form registers stdio servers only. To reach a remote server over streamable-HTTP/SSE, you have two options:

- Register it through the admin API with `transport: http` and a `url` (see the [API reference](../../reference/api.md)).
- Reference it through an `mcp_server` **connection** whose config sets `transport: http` and `url`. This is the right choice when the server needs an auth header built from a secret.

## How tools are discovered

You do not list a server's tools by hand — the server reports them. When a server first connects (on first use, or when you press **Warm** or **Tools**), TheYgent calls the server's tool-listing method and caches the result for the connection's lifetime.

That cache also drives validation. When you reference a tool in an `mcp_tool` node:

- If the server is currently connected, the tool name is checked against its reported tools up front — an unknown name is rejected before the agent runs (`mcp_tool_not_found`).
- If the server is not connected, the agent is accepted and the check happens lazily at run time — a missing tool binds the node's `err` port instead of failing the run.

An unregistered server name is always rejected up front (`mcp_server_not_found`).

## Secrets in server config

MCP servers run locally, in your trust domain — the same posture as your inference engines. How a credential reaches the server depends on the transport:

- **stdio servers** receive secrets and paths through their **Env** — each `KEY=value` line is set in the subprocess's environment. This is where an API key or a data path goes. Env values are stored so the registration survives a restart, but are never echoed back or logged with their values.
- **HTTP servers** get their authentication header built server-side from an `mcp_server` **connection**'s secret. The secret never appears in the agent document.

To create an `mcp_server` connection, open the [editor](../editor.md), click empty canvas so nothing is selected, and use the **Connections (tool / MCP auth)** panel: choose **New connection**, set **Kind** to `mcp_server`, put `transport`/`url`/`headers` in **Config**, and the credential in the write-only **Secret** field.

## Calling MCP tools from an llm

Like any tool node, an `mcp_tool` can be a **step** (wired with data edges, arguments templated with `$in`) or a **capability** the model calls itself. To make it a capability, wire its violet `use` handle into an [llm node's](llm.md) `tools` port with a **tool** edge.

Two things make MCP capabilities especially convenient:

- **The node's id becomes the function name** the model calls — rename the node to give the model a clear name.
- **MCP tools self-describe.** The server already provides each tool's schema, so — unlike a REST capability — you do not write a `parameterSchema`. An optional `description` on the node nudges the model on when to use it.

## Behavior notes

- **Error contract.** A tool-level error the server reports, an unknown tool name, or a connection failure all bind the node's `err` port; the run continues. A tool error is structured output, not a run failure.
- **Reconnect once, then fail honestly.** On a transport failure the manager retries the connection once; if it still fails, the node binds `err`.
- **Lazy connect and reuse.** A server connects on first use and stays connected for the control plane's lifetime (there is no idle-timeout teardown — server processes are cheap). **Warm** pre-connects; **Close** tears the connection down but keeps the registration.
- **Per-call timeout.** A single tool call is capped at 120 seconds by default, tunable with `THEYGENT_MCP_CALL_TIMEOUT_S` (see the [environment reference](../../reference/environment.md)).
- **Wrong node type.** A plain `tool` node pointing at an MCP binding is rejected up front (`tool_binding_mismatch`) — use an `mcp_tool` node instead.

## Worked example — a filesystem tool as a capability

Give an assistant read access to files through a filesystem MCP server, and let it decide when to read.

```mermaid
graph LR
  in([input]) --> llm["llm · assistant"]
  llm --> out([output])
  fs["mcp_tool · read_file"] -. tool .-> llm
```

Register a `filesystem` server on the MCP page (command `npx`, args `-y @modelcontextprotocol/server-filesystem /tmp`), then add a Tool node, choose **MCP**, pick the `filesystem` server, and set the tool to `read_file`:

```json
{
  "id": "read_file",
  "type": "mcp_tool",
  "kind": "activity",
  "config": { "tool": "read_file", "server": "filesystem" }
}
```

Wiring its `use` handle into the llm's `tools` port exposes it as a `read_file` function the model can call. To use the same node as a **step** instead — reading one fixed file each run — feed its `in` port and template the path, for example `"args": { "path": "$in.in.path" }` when the run input is an object like `{"path": …, "question": …}`. See [Input references](../input-references.md) for the `$in` grammar.

## Works well with

- [The llm node](llm.md) — the `tools` port that turns an MCP tool into a model capability.
- [Tool nodes](tools.md) — built-in and REST tools, wired the same two ways.
- [Input references](../input-references.md) — the `$in` grammar for templating arguments in step mode.
- [Nodes, ports, and edges](../../concepts/nodes-ports-edges.md) — how tool-channel edges differ from data edges.
