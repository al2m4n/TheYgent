# Platform settings

Platform settings configure how this installation behaves: what the run waterfall captures, how long tool calls may take, how much of a tool's result a model is shown, how documents are chunked. An admin edits them on the **Settings** page, one tab per group, or over the API. Each tab saves only its own group.

## How a setting gets its value

Each setting resolves in this order:

1. **An environment variable**, if the setting has one and it is set to a non-empty value. Some variables **pin** the setting: the environment wins and the field is locked in the UI. Others **cap** it: you can still edit the setting, but only up to the environment's value.
2. **The stored value**, if an admin saved one.
3. **The default** listed below.

Changes apply without a restart, in every control-plane process and the worker, within about 15 seconds. Some apply at a later point, which the UI names next to the field: at the next run, connection, ingest, crawl or sign-in. A stored value that no longer passes validation (for example, after an upgrade narrowed its range) is ignored in favor of the default, and the field says so.

## Tools

Applies to every tool an `llm` node calls: builtin, REST, MCP and retrieval, in both interactive and durable runs.

| Setting | Default | Range | What it does |
|---|---|---|---|
| `tools.result_max_chars` | `16000` | 1,000–1,000,000 | The most characters of one tool result the model is shown. A longer result is cut, and a note at the end tells the model how many characters were left out. Applies from the next run. An `llm` node's own `maxToolResultChars` overrides it; see [Tools the model can call](../building/nodes/llm.md#tools-the-model-can-call). |

The default is about 4,000 tokens: a default retrieval (five 450-token chunks) reaches the model whole, while a set of crawled pages is cut. The run inspector still shows every tool's whole result, and the tool's span in the waterfall records the result's full length and whether it was cut.

## Telemetry

| Setting | Default | Environment | What it does |
|---|---|---|---|
| `telemetry.io_capture` | `full` | `THEYGENT_IO_CAPTURE` (cap) | The deployment-wide ceiling on node input/output capture: `off`, `metadata` or `full`. Each agent's own policy applies under it. |
| `telemetry.io_capture_max_bytes` | `262144` | `THEYGENT_IO_CAPTURE_MAX_BYTES` (cap) | The size cap for one captured payload. A larger payload is stored truncated, with its true size. |
| `telemetry.io_retention_days` | `0` | — | Delete captured run inputs and outputs after this many days, for agents that set no retention of their own. `0` keeps them. |
| `telemetry.otlp_enabled` | on only when an endpoint is set in the environment | — | Export spans to an OTLP collector. Storing `false` stops export even when the environment sets an endpoint. |
| `telemetry.otlp_endpoint` | unset | `OTEL_EXPORTER_OTLP_ENDPOINT` (pin) | The OTLP/HTTP collector endpoint. |
| `telemetry.otlp_headers` | unset | — | Headers sent with every export, such as collector auth. Write-only: reads return the header names, never the values. |
| `telemetry.otlp_redact_attrs` | none | `THEYGENT_OTEL_REDACT_ATTRS` (pin) | Span attribute names replaced with `[redacted]` before export. The local span store keeps them. |

See [Observability](../running/observability.md).

## RAG

| Setting | Default | Range | What it does |
|---|---|---|---|
| `rag.chunk_max_tokens` | `450` | 50–4,000 | Token budget per chunk, at ingest. |
| `rag.chunk_overlap_tokens` | `60` | 0–1,000 | Overlap between chunks, at ingest. |
| `rag.crawl_desired_concurrency` | `2` | 1–16 | Concurrent page fetches a crawl aims for. |
| `rag.crawl_max_concurrency` | `4` | 1–32 | The most concurrent page fetches during a crawl. |
| `rag.max_upload_bytes` | 50 MiB | 1 MiB–1 GiB | The largest document you can upload. |
| `rag.default_top_k` | `5` | 1–50 | How many passages a retrieval returns when the query or node sets no `topK`. |
| `rag.default_embedding_model` | unset | — | The embedding model the new-source form suggests. Each source keeps the model it was created with. |

See [RAG sources](../rag/index.md).

## MCP

| Setting | Default | Environment | What it does |
|---|---|---|---|
| `mcp.call_timeout_s` | `120` | `THEYGENT_MCP_CALL_TIMEOUT_S` (pin) | Seconds one MCP tool call may take. Applies to new and reconnected server connections. |
| `mcp.connect_timeout_s` | `30` | — | Seconds starting and connecting to one MCP server may take. |
| `mcp.extra_registries` | none | `THEYGENT_MCP_REGISTRIES` (pin) | Self-hosted MCP registries to browse alongside the public ones. |
| `mcp.oauth_redirect_url` | `http://localhost:8080/mcp/oauth/callback` | `THEYGENT_OAUTH_REDIRECT_URL` (pin) | Where an authorization server sends the browser back after consent. Change it behind a reverse proxy. |

See [MCP servers](../mcp/index.md).

## Sign-in

| Setting | Default | Environment | What it does |
|---|---|---|---|
| `auth.session_ttl_hours` | `168` | — | How long an interactive sign-in stays valid. Applies to sign-ins after the change. |
| `auth.oidc_redirect_url` | `http://localhost:8080/auth/oidc/callback` | `THEYGENT_OIDC_REDIRECT_URL` (pin) | The redirect URI to register with each sign-in provider. |

See [Users, roles & sign-in](../running/users.md).

## AI gateway

| Setting | Default | What it does |
|---|---|---|
| `ai_gateway.request_retention_days` | `90` | Days each AI gateway request is kept in detail. Older days move to a compressed archive, and reports read them from daily totals. `0` keeps every request in detail. It is edited under **AI Gateway → Settings → Request records**. |

## Over the API

Reading settings needs an admin, as does changing them.

```bash
curl -s http://localhost:8080/settings -H "Authorization: Bearer $TOKEN"
```

```bash
curl -s -X PATCH http://localhost:8080/settings -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"tools.result_max_chars": 8000}'
```

A `PATCH` body maps keys to values; `null` resets a key to its default. The patch is all or nothing: if any key is unknown or out of range, the response is `422 invalid_setting` and nothing is stored.
