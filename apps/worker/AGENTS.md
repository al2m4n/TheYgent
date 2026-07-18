# AGENTS.md — worker

Rules for `apps/worker`. The repo-wide rules in the root [AGENTS.md](../../AGENTS.md) and
the control-plane rules in [apps/control-plane/AGENTS.md](../control-plane/AGENTS.md)
apply first; the design is documented in
[docs/dev-docs/durable-execution.md](../../docs/dev-docs/durable-execution.md).

The worker is a **thin bootstrap** that hosts the durable runtime as a separate deployable
for server topologies. On desktop, the control plane runs the identical runtime in-process
and this binary is unused — one codebase, two deployables, never two architectures.

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
