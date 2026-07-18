# AGENTS.md — control plane

Rules specific to `apps/control-plane`. The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first; the component's design is documented in
[docs/dev-docs/control-plane.md](../../docs/dev-docs/control-plane.md),
[graph-execution.md](../../docs/dev-docs/graph-execution.md), and
[durable-execution.md](../../docs/dev-docs/durable-execution.md).

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
- **Every sensitive read goes through `governance.authorize()`** (trace/io/settings
  permissions). It allows everything today; keep routing through it so real RBAC needs no
  endpoint retrofit.
- Errors are the house envelope `{"error": {"message", "code"}}` with stable snake_case
  codes; add new codes, don't repurpose existing ones.

## Execution rules

- **The walker (`walker.py`) does no DB I/O** — it is a pure async interpreter over an
  injected context. Keep new node executors runtime-agnostic so both the in-process walker
  and the durable runtime reuse them.
- **A new node type is additive in three places:** a handler in `walker.py`, a lowering in
  `durable/compiler.py`, and a `NODE_TYPE_KIND` entry in `packages/ir` — plus tests in
  both runtimes. Dispatch is by `kind` first, then `type`.
- **Durable discipline:** the `dbos` import stays under `durable/`; there is exactly one
  registered workflow (`theygent_run`) — DBOS recovers by registered name, so per-agent
  workflows would be unrecoverable. Orchestration nodes (router/transform/loop control)
  do no I/O — they must re-derive identically from journaled step results on replay. All
  I/O lives in step bodies. Only serializable data crosses the workflow→step boundary;
  bytes travel as artifact references, never journaled.
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
IR builders). Durable tests run the real DBOS runtime against the ephemeral DB. Every new
endpoint, node type, or migration lands with coverage here — migrations are exercised by
`tests/test_migrations.py`.
