# AGENTS.md — control plane · `tests/`

The control plane's pytest suites. The posture: everything real except the model. A real
ephemeral Postgres migrated by real Alembic (never SQLite, never `create_all`), a real HTTP
server standing in for the inference plane, real subprocesses for MCP. Rules in
[apps/control-plane/AGENTS.md](../AGENTS.md) apply first; the repo-wide test map is in
[testing.md](../../../docs/dev-docs/testing.md).

```bash
uv run --package theygent-control-plane pytest                    # fast suite (default: -m 'not integration')
uv run --package theygent-control-plane pytest -m integration     # env-gated: real engines, serial
uv run --package theygent-control-plane pytest tests/test_rag.py  # one file
```

The fast suite needs Docker (testcontainers Postgres with pgvector). Without Docker the DB-backed
tests skip; they do not fail.

## Fixtures and fakes

<!-- agents-map: covers=. -->

| File | What it provides |
|------|------------------|
| [`conftest.py`](conftest.py) | One testcontainers Postgres per session, migrated with `alembic upgrade head`; every fast test starts from a truncated schema (autouse). Integration tests use `DATABASE_URL` instead, so they run on hosts without Docker. |
| [`_auth.py`](_auth.py) | Signs clients in through the real `/auth/setup` and `/auth/login` (the API is closed until an admin exists). |
| [`_db.py`](_db.py) | Read-only asyncpg helpers for asserting rows the API does not expose. |
| [`_ir.py`](_ir.py) | The trivial `input → llm → output` IR and builders that return fresh copies. |
| [`_fake_inference.py`](_fake_inference.py) | A real uvicorn OpenAI-compatible server on an ephemeral port: chat (streaming, tool calls, reasoning), embeddings, audio, images. |
| [`_durable.py`](_durable.py) | A blockable streaming fake inference (freeze a run mid-activity to simulate a crash), agent builders, DBOS schema reset between tests. |
| [`_fake_mcp_server.py`](_fake_mcp_server.py) | A real stdio MCP server subprocess. |
| [`_fake_http.py`](_fake_http.py), [`_http.py`](_http.py) | Real local HTTP servers for the http tool (recording requests) and `http_fetch`. |
| [`_fake_idp.py`](_fake_idp.py) | A threaded OIDC provider (discovery, token, userinfo) for sign-in tests. |
| [`_fake_apify.py`](_fake_apify.py) | A local stand-in for the Apify API. |
| [`fixtures/mcp_registry/`](fixtures/mcp_registry) | Captured MCP registry payloads. |

## Where a test goes

| Area | Files |
|------|-------|
| Runs, sessions, memory, output | `test_fast_suite.py`, `test_persistence.py`, `test_run_output.py`, `test_memory.py`, `test_sessions.py`, `test_lists.py`, `test_reconcile.py`, `test_reasoning_and_interrupt.py`, `test_inline_think.py` |
| Graph walker and node types | `test_graph_runs.py`, `test_substitution.py`, `test_multi_input.py`, `test_boundary_nodes.py`, `test_tool_router.py`, `test_tool_binding.py`, `test_http_tool.py`, `test_transform.py`, `test_guardrail.py`, `test_gates.py`, `test_audio.py`, `test_vision_content.py`, `test_llm_parity.py` |
| LLM tool calling | `test_tool_calling.py`, `test_tool_wiring.py`, `test_tool_result_cap.py` |
| Durable runtime | `test_durable.py`, `test_durable_run_endpoint.py`, `test_lowering_nodes.py`, `test_observability_durable.py` |
| Registry, drafts, triggers, samples | `test_agents.py`, `test_drafts.py`, `test_triggers.py`, `test_samples.py` |
| MCP | `test_mcp.py`, `test_mcp_http.py`, `test_mcp_generated.py`, `test_mcp_hub.py`, `test_mcp_registry.py` |
| RAG, crawler | `test_rag.py`, `test_rag_parse.py`, `test_crawler.py` |
| Connections, secrets, transfer, artifacts | `test_connections.py`, `test_transfer.py`, `test_artifacts_http.py` |
| Observability | `test_observability_core.py`, `test_observability_api.py` |
| Identity, settings, bench | `test_auth.py`, `test_settings.py`, `test_bench.py` |
| AI gateway side | `test_ai_gateway_events.py`, `test_ai_gateway_config.py`, `test_ai_gateway_keys.py`, `test_ai_gateway_reports.py`, `test_ai_gateway_catalog.py`, `test_retention.py` |
| Schema | `test_migrations.py` |
| Real engines (opt-in) | `test_integration_mlx.py`, `test_integration_tool_calling.py`, `test_integration_durable.py`, `test_integration_lowering_nodes.py` |

## Rules for new tests

- A new endpoint, node type, setting or migration lands with a test here. A node type is tested
  in both runtimes.
- Prefer a real local server over a mock; shape fakes after what real servers return.
- Integration tests carry `@pytest.mark.integration`, gate on env vars, and skip cleanly when
  their prerequisites are absent.
