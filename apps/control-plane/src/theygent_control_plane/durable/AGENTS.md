# AGENTS.md — control plane · `durable/`

The durable runtime: it runs an agent's IR as a crash-safe DBOS workflow, so a run survives a
process crash and resumes from its last completed step. It is the second of the two runtimes
(the in-process [walker](../walker.py) is the first). Both execute the same IR with the same
activity executors. Rules in [apps/control-plane/AGENTS.md](../../../AGENTS.md) apply first;
the design is in [durable-execution.md](../../../../../docs/dev-docs/durable-execution.md).

## Where it sits

- **Started by:** the control plane's lifespan in [app.py](../app.py) when `THEYGENT_DURABLE=1`
  (in-process, the desktop topology), and by [apps/worker](../../../../worker/AGENTS.md) (the
  separate deployable, server topology). Same code in both. Never a second implementation.
- **Entered through:** `DurableRuntime.fire()` (triggers: invoke, webhook, schedule), the
  `POST /agents/{id}/durable-runs` endpoint, and `POST /runs/{id}/resume` (a human node's
  input). The non-durable `/runs` and `/graphs/runs` paths never come here.
- **Calls:** the walker's runtime-agnostic executors (`execute_llm`, `execute_tool`, …) as step
  bodies; the stores in [store.py](../store.py) for run rows; [observability/](../observability/AGENTS.md)
  for spans; the inference plane only through `packages/gateway-client`.
- **State:** DBOS checkpoints live in the `dbos` schema of the app's own Postgres (not Alembic's;
  DBOS migrates it on launch). App tables stay in `public`.

## Map

<!-- agents-map: covers=. -->

| File | What it does |
|------|--------------|
| [`__init__.py`](__init__.py) | The only place `dbos` is imported from. Importing the package registers the workflows and steps. |
| [`compiler.py`](compiler.py) | The lowering. `theygent_run` (the run workflow) resolves the pinned IR, then walks it deterministically. Activities become `@DBOS.step` (`_llm_step`, `_tool_step`, `_mcp_step`, `_rag_step`, `_http_step`, `_crawler_*_step`, `_transcribe_step`, `_speak_step`, `_imagine_step`, gate steps). Orchestration (router, transform, rule guardrail, loop control) runs inline with no I/O. Durable-only nodes: `human` → `DBOS.recv` on topic `human:<node_id>`; `subgraph` → child `theygent_run` (`maxDepth` rides in `agent_ref`); `loop` → bounded inline repetition; `map` → fan-out on `MAP_QUEUE`. `theygent_scheduled_fire` is the one generic schedule workflow and runs `theygent_run` as its child. `DurableResources` / `set_resources()` hand the steps their process-local collaborators. |
| [`runtime.py`](runtime.py) | `DurableRuntime`: the DBOS lifecycle (one per process), `RUN_QUEUE`, `fire(trigger, input)`, schedule CRUD over DBOS dynamic schedules named `trigger-<id>`, and `reconcile_schedules` on boot. The `trigger` table stays the source of truth. |
| [`config.py`](config.py) | DBOS configuration: the system DB defaults to `DATABASE_URL` (override `DBOS_SYSTEM_DATABASE_URL`); `to_dbos_url` strips `+asyncpg` so one DSN serves both drivers. |
| [`bus.py`](bus.py) | `DeltaBus`: the in-process, non-durable token side channel. Steps publish deltas while they run; only the step's final return value is journaled, so a replayed step re-streams nothing. |

## Invariants specific to this package

- **Workflows are generic, never per agent.** DBOS recovers by registered name, so only
  `theygent_run` and `theygent_scheduled_fire` exist. Changing either signature strands every
  pending workflow. Extra run options ride inside `agent_ref` instead.
- **Replay determinism.** Code between steps re-runs on recovery and must derive the same result
  from journaled step outputs: no clock reads, no randomness, no I/O outside a step.
- **Only serializable data crosses into a step.** Bytes travel as artifact refs
  ([artifacts.py](../artifacts.py)); secrets resolve inside the step body and are never arguments.
- **Steps retry** with `_RETRY` (3 attempts, backoff). A step that must not repeat (the crawler
  start) sets `retries_allowed=False`. The worker's gateway client has zero SDK retries, so the
  two retry layers never stack.
- **Executor identity is per role.** Recovery claims only workflows this process role left
  pending (see [apps/worker](../../../../worker/AGENTS.md)).

## Adding a node type

A new node type lands in [walker.py](../walker.py) (an executor plus a `_walk_*` handler) and
here (a step wrapping the same executor, plus its branch in `_durable_walk`), with
`NODE_TYPE_KIND` in [packages/ir](../../../../../packages/ir/AGENTS.md). A durable-only type also
goes into the interface's `lib/durable.ts` mirror.

## Tests

`tests/test_durable.py`, `test_durable_run_endpoint.py`, `test_lowering_nodes.py`,
`test_observability_durable.py` (fast, real DBOS against the ephemeral
Postgres; helpers in `tests/_durable.py`), plus the env-gated `test_integration_durable.py` and
`test_integration_lowering_nodes.py`. `test_reconcile.py` covers the startup sweep that must
leave durable-owned runs (`run.runtime`) alone.
