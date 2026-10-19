# AGENTS.md — control plane · `alembic/`

The control plane's Postgres schema, as a linear chain of hand-written Alembic migrations. Alembic
owns every table in the `public` schema; nothing calls `create_all`, tests included. The DBOS
`dbos` schema is DBOS's own and stays out of this chain. Rules in
[apps/control-plane/AGENTS.md](../AGENTS.md) apply first; the schema overview is in
[control-plane.md](../../../docs/dev-docs/control-plane.md) ("Postgres: schema, domain, stores").

## Where it sits

- **ORM mirror:** [models.py](../src/theygent_control_plane/models.py) holds the SQLAlchemy rows,
  kept in lock-step with these migrations by hand. `Base.metadata` is `target_metadata` here only
  so tests can diff the two.
- **Run by:** `make migrate` (`alembic upgrade head` with `DATABASE_URL`), the compose `migrate`
  service, the control-plane pod's `migrate` init container in Kubernetes, and the test suite's
  session fixture.
- **[env.py](env.py)** runs async over the same `postgresql+asyncpg://` DSN as the app and fails
  loudly when `DATABASE_URL` is unset; [script.py.mako](script.py.mako) is the template
  `alembic revision` fills in.

## The chain

<!-- agents-map: covers=. -->
<!-- agents-map: covers=versions -->

| Revision | Adds |
|----------|------|
| `0001_initial` | `thread`, `run`, `message` |
| `0002_graph_run` | Graph-run fields on `run` |
| `0003_run_output` | `run.output` |
| `0004_mcp_server` | `mcp_server` (name-keyed MCP registrations) |
| `0005_agent` | `agent`, `agent_version` (the content-addressed registry) |
| `0006_trigger` | `trigger`, `run.trigger_id`, `run.completed_at` |
| `0007_human_waiting` | `run.awaiting_node` (durable human wait) |
| `0008_observability` | `span`, `node_io`, `agent_io_policy` |
| `0009_bench` | Bench suites, cases, runs, param presets |
| `0010_connection` | `connection`, `secret` (the encrypted auth seam) |
| `0011_gate_counter` | `gate_counter` (ratelimit windows) |
| `0012_run_runtime` | `run.runtime` (interpreter vs durable owner) |
| `0013_mcp_http` | HTTP transport fields on `mcp_server` |
| `0014_sessions` | Renames `thread` → `chat_session`; `message.run_id` nullable |
| `0015_rag` | `rag_source`, `rag_document`, `rag_chunk` (needs pgvector) |
| `0016_agent_draft` | `agent_draft` (editor autosave) |
| `0017_platform_setting` | `platform_setting` |
| `0018_identity` | Accounts, auth sessions, API keys, sign-in providers, run/session ownership |
| `0019_user_avatar` | Provider avatar URL on accounts |
| `0020_ai_gateway` | AI gateway records, daily usage, instances, configs, fingerprints, developers |
| `0021_ai_gateway_key_set` | Key-set version trigger |
| `0022_ai_gateway_catalog_stats` | Catalog keeps observation stats only |
| `0023_ai_gateway_router` | Cost without routing (`baseline_usd`) |
| `0024_ai_gateway_shadows` | Shadow-run fields |
| `0025_ai_gateway_retention` | Request archive and `archived_before` |
| `0026_ai_gateway_shadow_agreement` | Shadow same-next-action agreement |
| `0027_ai_gateway_shadow_model` | Shadow runs grouped by the model they ran on |

Keep this table current: a new revision adds a row in the same change.

## Writing a migration

- One linear head. Name the file `NNNN_<what>.py` with the next number and set `down_revision` to
  the previous head.
- Write `upgrade()` and a real `downgrade()`; the suite round-trips the whole chain.
- Update [models.py](../src/theygent_control_plane/models.py) in the same change. The suite
  compares the migrated schema to the ORM metadata, so drift fails.
- A data migration that rewrites existing rows says so in its docstring and is tested against
  rows created at the previous revision.

## Tests

`tests/test_migrations.py`: `upgrade head` → `downgrade base` on a scratch database, plus the
ORM-vs-schema comparison.
