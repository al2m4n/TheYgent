# AGENTS.md — worker

Rules for `apps/worker`. The repo-wide rules in the root [AGENTS.md](../../AGENTS.md) and
the control-plane rules in [apps/control-plane/AGENTS.md](../control-plane/AGENTS.md)
apply first; the design is documented in
[docs/dev-docs/durable-execution.md](../../docs/dev-docs/durable-execution.md).

The worker is a **thin bootstrap** that hosts the durable runtime as a separate deployable
for server topologies. On desktop, the control plane runs the identical runtime in-process
and this binary is unused — one codebase, two deployables, never two architectures.

## Where it sits

- **Runs:** the control plane's [durable package](../control-plane/src/theygent_control_plane/durable/AGENTS.md)
  (`DurableRuntime`, the `theygent_run` and `theygent_scheduled_fire` workflows, every step).
  It imports them from `theygent_control_plane`.
- **Consumes:** the durable queue the control plane enqueues onto (`THEYGENT_DURABLE=1` on the
  control plane), and recovers workflows a crash left pending.
- **Shares:** the control plane's Postgres (`DATABASE_URL`; app tables in `public`, checkpoints in
  `dbos`) and, in Kubernetes, the `theygent-data` volume for artifacts.
- **Calls:** the inference plane's `/v1/*` (`THEYGENT_INFERENCE_PLANE_URL`, or the older
  `THEYGENT_INFERENCE_PLANE_BASE_URL`) through `packages/gateway-client`; MCP servers, spawned in
  the worker's own trust domain.
- **Deployed by:** the `worker` profile in `docker-compose.yml`, `deploy/k8s/worker.yaml`, and
  [Dockerfile](Dockerfile) (see [deploy/AGENTS.md](../../deploy/AGENTS.md)).

## Map

<!-- agents-map: covers=src/theygent_worker -->

| File | What it does |
|------|--------------|
| [`src/theygent_worker/app.py`](src/theygent_worker/app.py) | `build_runtime`: the sessionmaker, the MCP manager with its registry rehydrated, a `GatewayClient` with `max_retries=0`, the worker's own `Telemetry` and settings, and a `DurableRuntime` with `executor_id="worker"`. `run_worker`: launch, reconcile DBOS schedules to the enabled schedule triggers, then block while DBOS consumes the queue. |
| [`src/theygent_worker/__main__.py`](src/theygent_worker/__main__.py) | `theygent-worker` / `python -m theygent_worker` |
| [`Dockerfile`](Dockerfile) | The worker image; it mirrors the control-plane image because it runs the same package and spawns MCP servers itself |

## Rules

- **No business logic lives here.** Workflows, steps, and node executors all live in the
  control-plane package (`durable/` + the shared executors); the worker only wires seams
  and launches the runtime. If a change adds logic to the worker, it belongs elsewhere.
- **Executor identity is per role:** the worker registers its own executor id, distinct
  from the control plane's in-process runtime, so crash recovery claims only workflows
  this role's process left pending — never another role's.
- **The worker's gateway client is constructed with zero SDK retries** — the durable
  runtime owns retry on this path, and provider-level retry on top of a step retry is a
  double-retry hazard. Keep it that way.
- The worker reaches inference only through `packages/gateway-client` with logical model
  ids, and shares the control plane's Postgres (app tables in `public`, durable state in
  the `dbos` schema).
- Unattended contexts fail fast on anything interactive (e.g. an MCP connection that
  needs a user-driven OAuth flow binds an error rather than hanging).

There is no test directory here on purpose: the durable runtime and its recovery are
tested in `apps/control-plane/tests/` against the same code the worker hosts.
