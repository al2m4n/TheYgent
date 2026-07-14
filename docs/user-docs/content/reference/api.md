# API reference

A quick reference to the HTTP endpoints you are most likely to call directly — for scripting, automation, or debugging. TheYgent has **two services**, each with its own base URL:

- **Control plane** — `http://localhost:8080` — agents, runs, sessions, triggers, connections, artifacts, observability.
- **Inference plane** — `http://localhost:8081` — the OpenAI-compatible data plane (`/v1/*`) and model management (`/admin/*`).

The interface uses these same endpoints, so anything you can do in the UI you can do here. This page lists the user-facing surface; it is not the full internal API.

!!! note "Wire conventions"
    Control-plane **request bodies and stored run/session records use `snake_case`** (`session_id`, `content_hash`). The immediate run **result** envelope and streaming (SSE) frames use `camelCase` (`runId`). The **inference plane** (`/v1/*` and `/admin/*`) is `camelCase` throughout. The graph document (`ir`) is always `camelCase`. Keep them straight when you assemble a body.

!!! warning "Authentication"
    On a local install the control plane's interactive endpoints are **open** — there is no per-request auth in front of runs, agents, or sessions. Two surfaces are the exception and carry real auth: `POST /agents/{id}/invoke` (a bearer token, [`THEYGENT_INVOKE_TOKEN`](environment.md)) and webhook delivery `POST /hooks/{id}` (an HMAC signature). Do not expose the control plane to an untrusted network.

---

## Control plane

### Health

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness — is the process up? Returns `{"status":"ok"}`. |
| GET | `/readyz` | Readiness — 200 when ready, 503 `not-ready` naming the down dependency (database or inference plane). |

### Agents

Saved agents are immutable, content-addressed versions. See [Saving agents](../building/saving-agents.md) and [Agent versioning](../concepts/versioning.md).

| Method | Path | Purpose |
|---|---|---|
| POST | `/agents` | Publish a new agent from an IR document. Body `{ir, name?}`. `201`; reusing an existing id → `409 agent_exists`. |
| GET | `/agents` | List agents, newest first (`limit` 1–200 default 50, `before` cursor). |
| GET | `/agents/{id}` | One agent with its versions, newest first. |
| POST | `/agents/{id}/versions` | Publish a new version. Identical content under an existing version is an idempotent `200`; different content under the same version → `409 version_conflict`. |
| GET | `/agents/{id}/versions/{version}` | The full stored IR + canvas layout for one version. |
| GET / PUT | `/agents/{id}/io-policy` | Read/set the per-agent I/O capture policy. PUT body `{io_capture, io_retention_seconds?, redact_rules?}`. Setting it never changes the content hash. |

There is no endpoint to delete an agent or a version — published versions are permanent.

### Runs

Statuses: `created → streaming → waiting → completed | failed`. See [Runs & sessions](../concepts/runs-and-sessions.md).

| Method | Path | Purpose |
|---|---|---|
| POST | `/runs` | Plain prompt run. Body `{input, model, params?, stream?, session_id?}`. `model` is a logical id. Streams SSE by default. |
| POST | `/graphs/runs` | Run an inline IR graph. Body `{ir, input, stream?, session_id?}`. Invalid IR → `400 invalid_ir` (no run row created). |
| POST | `/agents/{id}/runs` | Run a saved agent by reference. Body `{input, version?, content_hash?, session_id?, stream?}`. |
| POST | `/agents/{id}/invoke` | Token-authed, non-interactive sibling of the above. Same body but `stream` defaults **false**. Requires a bearer token. |
| POST | `/agents/{id}/durable-runs` | Run a saved agent on the durable runtime (the only way to run `human`/`subgraph`/`loop`/`map` agents). Returns `202 {run_id}`; poll `GET /runs/{id}`. Needs [durable mode](../running/durable.md). |
| GET | `/runs` | List runs, newest first (`limit`, `before`). |
| GET | `/runs/{id}` | Run detail, including `output`, `error`, `content_hash`, `trigger_id`, `awaiting_node`, `completed_at`. |
| POST | `/runs/{id}/resume` | Deliver input to a run paused (`waiting`) at a `human` node. Body `{input}`. Returns `202`. |

**Start a streaming prompt run:**

```bash
curl -N -X POST http://localhost:8080/runs \
  -H 'Content-Type: application/json' \
  -d '{"input": "Summarize the theory of relativity.", "model": "triage-fast", "stream": true}'
```

The stream is Server-Sent Events: `run` (status frames), `delta` (answer tokens), `reasoning` (model thinking, kept out of the output), terminated by `data: [DONE]`. Set `"stream": false` for a single JSON response `{runId, status, output}`.

**Run a saved agent (non-streaming) with a session:**

```bash
curl -X POST http://localhost:8080/agents/agent.support/runs \
  -H 'Content-Type: application/json' \
  -d '{"input": {"question": "Where is my order?"}, "session_id": "ses_abc", "stream": false}'
```

**Invoke an agent with the invoke token:**

```bash
curl -X POST http://localhost:8080/agents/agent.support/invoke \
  -H 'Authorization: Bearer YOUR_INVOKE_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"input": {"question": "Where is my order?"}}'
```

**Resume a waiting run:**

```bash
curl -X POST http://localhost:8080/runs/RUN_ID/resume \
  -H 'Content-Type: application/json' \
  -d '{"input": {"approved": true}}'
```

### Observability

Per-run trace data. See [Observability](../running/observability.md).

| Method | Path | Purpose |
|---|---|---|
| GET | `/runs/{id}/trace` | The persisted span tree (timing, status, attribution) — no payloads. |
| GET | `/runs/{id}/trace/stream` | Live SSE overlay: events `span.open`, `span.close`, `done`; `: keepalive` comment every 15s. |
| GET | `/runs/{id}/nodes/{node_id}/io` | Lazy per-node input/output payloads, gated by the capture policy (a gated state is a `reason`, never an error). |

### Triggers & webhooks

A trigger deploys a saved, pinned agent behind an unattended entry point. See [Triggers & webhooks](../running/triggers.md).

| Method | Path | Purpose |
|---|---|---|
| POST | `/triggers` | Create a trigger. Body `{agent_id, kind, version?|content_hash?, config, enabled?}`. Pin exactly one of `version`/`content_hash`. |
| GET | `/triggers` | List triggers (webhook secrets redacted to `"***"`). |
| GET / PATCH / DELETE | `/triggers/{id}` | Read / update (`enabled`, `config` only — kind and pin are immutable) / delete a trigger. |
| POST | `/hooks/{id}` | Fire a `webhook` trigger. The raw JSON body is the run input; requires the `X-Theygent-Signature` header. Returns `202`. |

Trigger `config` keys: `schedule` needs `config.cron` (a valid cron expression) and takes optional `config.input`; `webhook` needs `config.secret` (the inbound signing secret).

**Fire a webhook** — the signature is the HMAC-SHA256 of the *raw* request body keyed by the trigger's secret:

```bash
BODY='{"question":"Where is my order?"}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "YOUR_WEBHOOK_SECRET" | awk '{print $2}')
curl -X POST http://localhost:8080/hooks/TRIGGER_ID \
  -H "X-Theygent-Signature: sha256=$SIG" \
  -H 'Content-Type: application/json' \
  -d "$BODY"
```

A bad or missing signature → `401 invalid_signature`; a disabled trigger → `409 trigger_disabled`.

### Sessions

Opt-in conversational memory. See [Runs & sessions](../concepts/runs-and-sessions.md).

| Method | Path | Purpose |
|---|---|---|
| GET | `/sessions` | List sessions, newest activity first. |
| GET | `/sessions/{id}` | Session detail with messages in order. |
| POST | `/sessions` | Create/ensure a session. Body `{id?, metadata?}`; a missing id is minted server-side. |
| POST | `/sessions/{id}/turns` | Append a `{user_content, assistant_content}` pair (both non-blank). |
| DELETE | `/sessions/{id}` | Delete the session and its messages; its runs are detached, not deleted. |

### Connections

Named tool/MCP credentials. The `secret` is write-only and never returned. See [Tools](../building/nodes/tools.md).

| Method | Path | Purpose |
|---|---|---|
| POST | `/connections` | Create. Body `{name, kind, config, secret?, enabled?}`. `kind` is `http_auth` or `mcp_server`. |
| GET | `/connections` | List (responses expose `hasSecret` only; secret-ish config keys redacted). |
| GET / PATCH / DELETE | `/connections/{id}` | Read / update (a non-empty `secret` rotates in place without changing any content hash) / delete. |

### RAG sources

Retrieval collections agents search through the rag node. See [RAG sources](../rag/index.md).

| Method | Path | Purpose |
|---|---|---|
| POST | `/rag/sources` | Create. Body `{name, kind, embedding_model, config}`. `kind` is `upload` or `crawl`; for `crawl`, `config` carries `root_url` (required), `max_pages?`, `render_js?`. `embedding_model` is a logical id, never an engine name. |
| GET | `/rag/sources` | List (keyset pagination via `limit` + `before`). |
| GET / PATCH / DELETE | `/rag/sources/{id}` | Read (status + live ingest `progress`) / rename or edit crawl config / delete with all documents and vectors. |
| GET | `/rag/sources/{id}/documents` | The source's documents with per-document status, chunk counts, and errors. |
| POST | `/rag/sources/{id}/documents?filename=…` | Upload one document as a raw body (the `Content-Type` header is the file's mime; 50 MiB cap). Returns `202`; poll the source for progress. |
| POST | `/rag/sources/{id}:ingest` | Start (or re-run) the crawl. `202`; unchanged pages are hash-skipped. `409` while an ingest is already running. |
| POST | `/rag/sources/{id}:cancel` | Cancel the in-flight ingest (a no-op on a settled source). |
| POST | `/rag/sources/{id}/query` | Run the same hybrid retrieval a rag node runs. Body `{query, top_k?, min_similarity?}`; returns the match list. |

```bash
curl -X POST "http://localhost:8080/rag/sources/rag_01ABC/documents?filename=handbook.pdf" \
  -H 'Content-Type: application/pdf' \
  --data-binary @handbook.pdf
```

### Artifacts

Audio/image blobs by reference. See [Voice](../chat/voice.md).

| Method | Path | Purpose |
|---|---|---|
| POST | `/artifacts` | Upload raw bytes (the `Content-Type` header is the stored mime). Returns `201 {ref, contentType, bytes}`. |
| GET | `/artifacts/{ref}` | Fetch a stored `art_…` blob (only `art_*` ids are served). |

```bash
curl -X POST http://localhost:8080/artifacts \
  -H 'Content-Type: audio/webm' \
  --data-binary @recording.webm
```

### MCP servers

Register external tool servers. See [MCP tools](../building/nodes/mcp.md).

| Method | Path | Purpose |
|---|---|---|
| PUT / GET / DELETE | `/admin/mcp/servers/{name}` | Register / read / remove a server config. |
| GET | `/admin/mcp/servers` | List registered servers (env **values** are never echoed). |
| GET | `/admin/mcp/servers/{name}/tools` | List the server's tools (lazy-connects; unreachable → `503 mcp_unavailable`). |
| POST | `/admin/mcp/servers/{name}:warm` / `:close` | Start / stop the server process. |

### Bench store

The Bench records results and presets here; testing itself runs against the inference plane. See [The Bench](../running/bench.md).

| Method | Path | Purpose |
|---|---|---|
| POST / GET | `/bench/runs` | Record / list bench results (filter by `logical_id`, `agent_id`, `suite_id`, `case_id`). |
| GET | `/bench/compare?a=&b=` | Per-metric deltas between two saved results. |
| POST / GET / DELETE | `/bench/presets` | Save / list / delete named param presets. |
| POST / GET | `/bench/suites` | Store / list golden-case suites (API-only; no UI yet). |

---

## Inference plane

The data plane is OpenAI-compatible. In every `/v1/*` call, the `model` field is a **logical id** you registered — never an engine name. See [Models & engines](../concepts/models-and-engines.md).

### Data plane (`/v1/*`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/v1/models` | List registered logical ids as OpenAI model objects. |
| POST | `/v1/chat/completions` | Chat (and vision — `messages` content may be text + `image_url` parts). Streaming and non-streaming. |
| POST | `/v1/embeddings` | Embeddings. `input` is a string or list of strings. |
| POST | `/v1/audio/transcriptions` | Speech-to-text. Multipart form with `model` + `file`. |
| POST | `/v1/audio/speech` | Text-to-speech. `{model, input, voice?}`; returns raw audio bytes. |
| POST | `/v1/images/generations` | Text-to-image. `{model, prompt, size?, n?, steps?}`; returns `{data:[{b64_json}]}`. |

```bash
curl http://localhost:8081/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model": "triage-fast", "messages": [{"role":"user","content":"Hello"}]}'
```

### Model management (`/admin/*`)

| Method | Path | Purpose |
|---|---|---|
| PUT | `/admin/models/{id}` | Register/replace a model under a logical id. |
| GET | `/admin/models` | List registered models with live state. |
| GET / DELETE | `/admin/models/{id}` | Read one / unregister (evicts then deletes). |
| GET | `/admin/models/{id}/capabilities` | Probe tool-calling, vision, reasoning, max context, modalities. |
| POST | `/admin/models/{id}:warm` / `:evict` | Pre-load / free the engine. |
| GET | `/admin/engines` | Currently resident engines and `maxResident`. |
| GET | `/admin/credentials` | List local credential names with `hasValue` (values are never read back). |
| PUT / DELETE | `/admin/credentials/{name}` | Set / remove a local credential (values are write-only). |
| GET | `/admin/catalog/models` | Browse installable models (`search`, `sort`, `limit`, `engines`, `size`, `modality`). |
| GET | `/admin/catalog/models/{repo}` | Model detail with per-quant variants and RAM fit badges. |
| POST | `/admin/catalog/install` | Start a download+install job. Body `{repo, engine, variantId, logicalId}`. |
| GET | `/admin/catalog/downloads` / `/{job_id}` | Track download progress. |
| POST | `/admin/catalog/downloads/{job_id}:cancel` | Cancel an in-flight install. |

**Register a hosted, OpenAI-compatible model:**

```bash
curl -X PUT http://localhost:8081/admin/models/claude-fast \
  -H 'Content-Type: application/json' \
  -d '{"binding":"openai-compatible","baseUrl":"https://api.example.com/v1","model":"provider-model-name","credentialRef":"secret://MY_API_KEY"}'
```

See [Remote models](../models/remote.md) for the reachable-binding fields.

### Health

| Method | Path | Purpose |
|---|---|---|
| GET | `/healthz` | Liveness. |
| GET | `/readyz` | Per-`(engine, modality)` readiness — each slot with a `reason` and install hint when not ready. Stays 200 while any engine is available. |

```bash
curl http://localhost:8081/readyz
```

## Related pages

- [Environment variables](environment.md) — the `THEYGENT_*` knobs these services read.
- [Troubleshooting](troubleshooting.md) — what the error codes mean and how to fix them.
- [Glossary](glossary.md) — terms used above (logical id, binding, run, trigger, artifact).
