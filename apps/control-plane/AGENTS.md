# AGENTS.md — control plane

`apps/control-plane` (`theygent_control_plane`) is the orchestration service: one FastAPI app on
:8080 that runs agent graphs, keeps the agent registry and its versions, chat sessions, triggers,
connections and secrets, MCP hosting, retrieval, observability, platform settings, identity, and
the AI gateway's records. It reaches models only over the inference plane's HTTP data plane.
The repo-wide rules in the root [AGENTS.md](../../AGENTS.md) apply first; the design is in
[control-plane.md](../../docs/dev-docs/control-plane.md),
[graph-execution.md](../../docs/dev-docs/graph-execution.md) and
[durable-execution.md](../../docs/dev-docs/durable-execution.md).

## Where it sits

- **Called by:** the [interface](../interface/AGENTS.md) (every operator surface except raw model
  traffic), the [MCP server](../mcp-server/AGENTS.md) (with a personal API key), the
  [AI gateway](../ai-gateway/AGENTS.md) (`/gateway/*` with a service key), and external callers
  (`/agents/{id}/invoke` with an API key or `THEYGENT_INVOKE_TOKEN`, `/hooks/{trigger_id}` signed
  with `X-TheYgent-Signature`).
- **Calls:** the [inference plane](../inference-plane/AGENTS.md)'s `/v1/*` through
  [packages/gateway-client](../../packages/gateway-client/AGENTS.md), by logical model id only; MCP
  servers; HTTP tools; the Apify API; OIDC providers; an OTLP collector when export is on.
- **Shares:** Postgres with the [worker](../worker/AGENTS.md) (app tables in `public`, DBOS
  checkpoints in `dbos`); the IR models with everyone through
  [packages/ir](../../packages/ir/AGENTS.md).
- **Owns on disk:** the artifact store (`THEYGENT_ARTIFACT_DIR`) for audio and image bytes.
- **Never:** imports an engine, reads the inference plane's state files, or receives the HF token or
  the organization's Anthropic key.

## Map

<!-- agents-map: covers=src/theygent_control_plane -->

All paths under [`src/theygent_control_plane/`](src/theygent_control_plane):

| Module | What it does | Context |
|--------|--------------|---------|
| [`app.py`](src/theygent_control_plane/app.py) | `create_app`: request models, auth dependencies, every route as a closure over injected stores, the lifespan (DB, gateway client, MCP rehydrate, dispatcher or durable runtime, ingest, retention), the shared IR-run path `_execute_ir_run`, the trigger `fire()` seam, SSE helpers | Route groups below |
| [`asgi.py`](src/theygent_control_plane/asgi.py), [`__main__.py`](src/theygent_control_plane/__main__.py) | ASGI entrypoint and CLI; env parsing (`DATABASE_URL`, `THEYGENT_INFERENCE_PLANE_URL`, `THEYGENT_DURABLE`, …) | |
| [`db.py`](src/theygent_control_plane/db.py) | One pooled async engine, `async_sessionmaker`, readiness ping | |
| [`run.py`](src/theygent_control_plane/run.py) | Pydantic domain entities: `Run`, sessions, agents and versions, drafts, `Trigger`, `Connection`, bench types | |
| [`models.py`](src/theygent_control_plane/models.py) | SQLAlchemy rows (storage shape only) | [alembic/](alembic/AGENTS.md) |
| [`store.py`](src/theygent_control_plane/store.py) | `RunStore` (runs + session memory), `AgentStore`, `DraftStore`, `TriggerStore`, `ConnectionStore`, `McpStore`, `BenchStore`; domain ↔ row mapping | |
| [`walker.py`](src/theygent_control_plane/walker.py) | The interactive runtime: `walk()` over an `IRDocument`, `$in` resolution, edge liveness, and the runtime-agnostic executors (`execute_llm`, `execute_tool`, `execute_http_tool`, `execute_mcp_tool`, `execute_rag`, `execute_crawler`, `execute_transcribe`, `execute_speak`, `execute_imagine`, guardrail and gate helpers, the LLM tool-call loop) | [graph-execution.md](../../docs/dev-docs/graph-execution.md) |
| [`durable/`](src/theygent_control_plane/durable) | The DBOS runtime that reuses those executors as steps | [durable/AGENTS.md](src/theygent_control_plane/durable/AGENTS.md) |
| [`reasoning.py`](src/theygent_control_plane/reasoning.py) | `ThinkSplitter`: separates inline `<think>` from the answer so reasoning never lands in `run.output` or session history | |
| [`tools/registry.py`](src/theygent_control_plane/tools/registry.py) | `ToolRegistry` with the built-ins `echo` and `http_fetch` | |
| [`tool_resolve.py`](src/theygent_control_plane/tool_resolve.py) | The server-side connection resolver: reads a `connection` row and decrypts its secret at step time | |
| [`secrets.py`](src/theygent_control_plane/secrets.py) | `SecretStore`: Fernet-encrypted secrets behind `sec_` refs (`THEYGENT_SECRET_KEY`, comma-separated for rotation) | |
| [`gates.py`](src/theygent_control_plane/gates.py) | Backends for `ratelimit` (fixed-window `gate_counter`) and `quota` (token usage summed from `span` rows) | |
| [`mcp/`](src/theygent_control_plane/mcp) | MCP transports, connection manager, generated servers, OAuth, the hub | [mcp/AGENTS.md](src/theygent_control_plane/mcp/AGENTS.md) |
| [`rag/`](src/theygent_control_plane/rag) | Retrieval sources, ingest, hybrid search on pgvector | [rag/AGENTS.md](src/theygent_control_plane/rag/AGENTS.md) |
| [`crawlers/`](src/theygent_control_plane/crawlers) | The `crawler` node's provider seam (Apify) and store browser | [crawlers/AGENTS.md](src/theygent_control_plane/crawlers/AGENTS.md) |
| [`observability/`](src/theygent_control_plane/observability) | Span capture, trace and node-I/O stores, live bus, OTLP export | [observability/AGENTS.md](src/theygent_control_plane/observability/AGENTS.md) |
| [`ai_gateway/`](src/theygent_control_plane/ai_gateway) | The AI gateway's records, configs, keys, reports, catalog | [ai_gateway/AGENTS.md](src/theygent_control_plane/ai_gateway/AGENTS.md) |
| [`samples/`](src/theygent_control_plane/samples) | The shipped example agents and their installer helpers | [samples/AGENTS.md](src/theygent_control_plane/samples/AGENTS.md) |
| [`auth.py`](src/theygent_control_plane/auth.py) | Identity: accounts, scrypt passwords, hashed opaque bearers (sessions `tys_`, API keys `tyk_`, gateway service keys `tygs_`), `AuthStore`, OIDC/OAuth2 code-flow helpers | |
| [`governance.py`](src/theygent_control_plane/governance.py) | `authorize(principal, permission, resource)`: the role floor per permission (`viewer < editor < admin`) | |
| [`settings.py`](src/theygent_control_plane/settings.py) | The typed `platform_setting` catalog: env pin vs env cap, stored values, live-apply hooks, `SettingsService` | |
| [`artifacts.py`](src/theygent_control_plane/artifacts.py) | Local blob store for audio and image bytes; nodes pass `{ref, contentType}` | |
| [`dispatcher.py`](src/theygent_control_plane/dispatcher.py) | The in-process cron dispatcher (non-durable mode only; single instance by design) | |
| [`transfer.py`](src/theygent_control_plane/transfer.py) | `POST /export` / `POST /import` bundle: DB-level, id-preserving, secrets stripped | |
| [`retention.py`](src/theygent_control_plane/retention.py) | Timed sweeps: archive old AI gateway request rows, delete expired `node_io` | |
| [`alembic/`](alembic) | The migration chain | [alembic/AGENTS.md](alembic/AGENTS.md) |
| [`tests/`](tests) | Fast and integration suites, fakes | [tests/AGENTS.md](tests/AGENTS.md) |
| [`spikes/prompt_cache/`](spikes/prompt_cache) | A measurement spike (results only; not product code) | |

## Route groups in `app.py`

Sections are marked with `# ── … ──` banners in `create_app`, in this order:

| Section | Routes | Backed by |
|---------|--------|-----------|
| Runs | `POST /runs` (single model call, optional `session_id`), `GET /runs`, `GET /runs/{id}`, `POST /runs/{id}/resume` | `RunStore`, gateway client |
| Run waterfall | `GET /runs/{id}/trace`, `/trace/stream`, `/nodes/{node_id}/io` | `observability/` |
| Sessions | `/sessions`, `/sessions/{id}`, `/sessions/{id}/turns` | `RunStore` |
| Artifacts, transfer | `/artifacts`, `/artifacts/{ref}`; `POST /export`, `POST /import` | `artifacts.py`, `transfer.py` |
| Graph runs | `POST /graphs/runs` (inline IR, used by the editor's Test panel) | `walker.py` via `_execute_ir_run` |
| Agent registry | `/agents`, `/agents/{id}`, `/agents/{id}/versions[/{v}]`, `/agents/{id}/io-policy`, `/stats` | `AgentStore`, `theygent_ir.content_hash` |
| Samples, settings | `/samples`, `/samples/install`; `/settings`, `/settings/otlp:test` | `samples/`, `settings.py` |
| Agent runs | `POST /agents/{id}/runs`, `POST /agents/{id}/durable-runs` | walker, `durable/` |
| Drafts | `/drafts`, `/drafts/{id}` | `DraftStore` |
| Triggers | `/triggers`, `/triggers/{id}`, `POST /agents/{id}/invoke`, `POST /hooks/{trigger_id}` | `TriggerStore`, `fire()` |
| Connections | `/connections`, `/connections/{id}` | `ConnectionStore`, `SecretStore` |
| RAG | `/rag/sources…` | `rag/` |
| MCP | `/admin/mcp/servers…`, `/connections/{id}/mcp…`, `/connections/{id}/mcp-oauth…`, `/mcp/oauth/callback`, `/admin/mcp/registries`, `/admin/mcp/catalog…`, `/admin/mcp/generated:preview` | `mcp/` |
| Bench | `/bench/suites`, `/bench/runs`, `/bench/compare`, `/bench/presets` | `BenchStore` |
| Crawlers | `/crawlers/catalog…` | `crawlers/` |
| Identity | `/auth/*` (status, setup, login, logout, me, password, API keys, providers, OIDC start/callback/exchange), `/users` | `auth.py` |
| AI gateway | `/gateway/*` | `ai_gateway/` |
| Health | `GET /healthz`, `GET /readyz` (inference plane and Postgres reachable) | |

Wire casing here is snake_case. A few surfaces whose payloads mirror node config (crawler catalog,
MCP hub install) accept camelCase aliases on purpose.

## Key flows

1. **Interactive graph run** (`POST /graphs/runs`, `POST /agents/{id}/runs`): parse and
   `validate_graph` (400 `invalid_ir`, nothing persisted) → reject engine names and unexecutable
   types → create the `Run` row → `walker.walk()` inside a `RunTrace` → stream SSE deltas → persist
   the output, spans and node I/O. A client disconnect cancels the run.
2. **Durable run** (triggers when `THEYGENT_DURABLE=1`, `POST /agents/{id}/durable-runs`):
   `DurableRuntime.fire()` enqueues `theygent_run` with the pinned `agent_ref`; the
   [durable package](src/theygent_control_plane/durable/AGENTS.md) walks it step by step.
3. **Publish**: `POST /agents` hashes the view-stripped IR (`theygent_ir.content_hash`) → 409
   `agent_exists` → the interface adds a version through `POST /agents/{id}/versions`; the same
   hash function runs on every run, so registry and runtime never disagree.
4. **Chat turn**: `POST /runs` (a model) or `POST /agents/{id}/runs` (an agent) with a
   `session_id` replays the stored turns verbatim and appends the new pair after the stream ends.

## Making changes

- **New endpoint:** a request model near the top of `app.py`, a closure in the matching section,
  `require_auth` / `require_editor` / `require_admin`, `governance.authorize()` for sensitive
  reads, the house error envelope, a test in `tests/`, and the user docs API reference
  (`docs/user-docs/content/reference/api.md`).
- **New node type:** see the execution rules below and
  [durable/AGENTS.md](src/theygent_control_plane/durable/AGENTS.md#adding-a-node-type).
- **New table or column:** a migration, `models.py`, the store, and the chain table in
  [alembic/AGENTS.md](alembic/AGENTS.md).
- **New platform setting:** the catalog in `settings.py`, the interface's settings tab, and
  `docs/user-docs/content/reference/settings.md`.
- **New env var:** read it empty-safe, add it to `.env.example`, the deploy manifests that need it,
  and `docs/user-docs/content/reference/environment.md`.

## Structure rules

- **One FastAPI app, built by the `create_app` factory** in `app.py`; every stateful
  backend (stores, gateway client, MCP manager, tool resolver, gates, telemetry, settings)
  is injected so tests can substitute fakes. Importing the module must have no side
  effects — `asgi.py` is the entrypoint.
- **Domain vs persistence split:** Pydantic entities in `run.py` are the wire/logic shape,
  SQLAlchemy rows in `models.py` are the storage shape, `store.py` maps between them. ORM
  rows never leak out of a store; the wire contract is never welded to the table schema.
- **Alembic owns the schema.** Migrations are hand-written, linear, and reviewed; never
  `create_all`, including in tests. The DBOS `dbos` schema is managed by DBOS itself and
  stays outside the Alembic chain.
- **Stores never commit — the caller owns the transaction.** Read handlers use the
  request-scoped session; writes use one transaction per logical operation. Ordering keys
  are explicit columns (`message.position`, `agent_version.seq`, `span.seq`), never
  timestamps.
- **Every sensitive read goes through `governance.authorize()`** (trace, I/O, settings,
  transfer, gateway usage permissions). It ranks the caller's role against a per-permission
  floor; ownership checks live at the endpoints that carry an owner column. Keep new sensitive
  surfaces routed through it rather than adding a second authorization path.
- Errors are the house envelope `{"error": {"message", "code"}}` with stable snake_case
  codes; add new codes, don't repurpose existing ones.

## Execution rules

- **The walker (`walker.py`) does no DB I/O** — it is a pure async interpreter over an
  injected context. Keep new node executors runtime-agnostic so both the in-process walker
  and the durable runtime reuse them.
- **A new node type is additive in three places:** a handler in `walker.py`, a lowering in
  `durable/compiler.py`, and a `NODE_TYPE_KIND` entry in `packages/ir` — plus tests in
  both runtimes. Dispatch is by `kind` first, then `type`.
- **Durable discipline:** the `dbos` import stays under `durable/`. The registered workflows
  are generic, never per agent: `theygent_run`, plus `theygent_scheduled_fire`, which runs it as a
  child. DBOS recovers by registered name, so per-agent workflows would be unrecoverable.
  Orchestration nodes (router/transform/loop control) do no I/O — they must re-derive
  identically from journaled step results on replay. All I/O lives in step bodies. Only
  serializable data crosses the workflow→step boundary; bytes travel as artifact references,
  never journaled.
- **Model ids:** engine names (`mlx`/`vllm`/`llamacpp`) are rejected up front with
  `400 engine_name_not_allowed` on every model-carrying surface — before a Run row exists.
- **The tool ok/err contract:** a failing tool/mcp/http/rag/audio/image step binds a
  structured error to the node's `error` handle and the run continues; only transport-level
  failures fail the run. An edge is live only if its source executed and activated that
  handle (router selects one, tool ok XOR err, guardrail pass XOR block).
- **`$in` resolution is loud:** unknown roots, undeclared ports, and missing fields raise
  errors naming the node and token — silent literal pass-through is forbidden.
- **Secrets resolve at step time only** (inside `SecretStore.resolve()`); plaintext never
  appears in the IR, logs, or journal. Connection auth is built server-side per call.
- **MCP connections follow the actor pattern:** each transport's async contexts live on
  one dedicated task; callers enqueue calls. Lazy connect, reuse for process lifetime, one
  reconnect retry — no eviction/arbitration (MCP processes are cheap; engines are heavy).
- **RAG:** a graph references a source by stable id, so re-ingesting never changes an
  agent's `contentHash`. The embedding model is pinned per source by logical id;
  ingest replaces documents atomically (chunk + embed before any row is touched).
- **Observability:** both runtimes run inside the same span-capture wrapper; span/node-IO
  rows are the source for the UI waterfall, and OTLP export is an opt-in second sink.
  Payload capture obeys the per-agent IO policy — check it before persisting new payload
  kinds.

## Tests

```bash
uv run --package theygent-control-plane pytest          # fast suite (ephemeral Postgres, fake gateway/MCP)
uv run --package theygent-control-plane pytest -m integration   # opt-in; needs real engines
```

Fixtures live in `tests/_*.py` (ephemeral DB, fake inference gateway, fake MCP servers,
IR builders); [tests/AGENTS.md](tests/AGENTS.md) maps which file covers what. Durable tests run
the real DBOS runtime against the ephemeral DB. Every new endpoint, node type, or migration
lands with coverage here — migrations are exercised by `tests/test_migrations.py`.
