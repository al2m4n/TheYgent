# AGENTS.md — control plane · `observability/`

The span pipeline behind the run waterfall: one capture wrapper, shared by both runtimes, records
a span per run and per node (timing, GenAI semconv scalars, which worker ran it) plus each node's
captured input and output. It fans out to the local Postgres tables and a live bus, and
optionally to an OTLP collector. Rules in [apps/control-plane/AGENTS.md](../../../AGENTS.md)
apply first; the design is in the Observability section of
[control-plane.md](../../../../../docs/dev-docs/control-plane.md).

## Where it sits

- **Called by:** the walker (`WalkContext` carries a `RunTrace`) and the durable compiler
  (`_durable_node_span`, `_begin_run_trace`). Both wrap every node in the same `SpanScope`.
- **Read by:** `GET /runs/{id}/trace`, `GET /runs/{id}/trace/stream` (SSE from `SpanBus`) and
  `GET /runs/{id}/nodes/{node_id}/io` in [app.py](../app.py), all through
  `governance.authorize()`; [gates.py](../gates.py) sums `gen_ai.usage.total_tokens` off `span`
  rows for the `quota` node; [retention.py](../retention.py) deletes old `node_io` rows.
- **Consumed in the UI by:** `apps/interface/src/components/waterfall/` (one waterfall for the
  bench, run detail, chat and the editor's Test panel).
- **Tables:** `span`, `node_io`, `agent_io_policy` (migration `0008_observability`).

## Map

<!-- agents-map: covers=. -->

| File | What it does |
|------|--------------|
| [`telemetry.py`](telemetry.py) | `Telemetry` (one per process: sinks, worker identity `host:pid`, capture bounds) hands out a `RunTrace` per run, which opens the root span and a `SpanScope` per node. The scope times the node, sets attributes, writes `span` + `node_io` per the effective capture policy, and publishes open/close events. Imports no `dbos`. |
| [`spans.py`](spans.py) | Pure values and math: `Span`, `SpanView` / `NodeIoView` / `AgentIoPolicyView`, deterministic span ids (`span_pk`, `derive_trace_id`), payload capping, and `resolve_effective_capture` (deployment ceiling ∧ topology default ∧ the agent's policy). |
| [`store.py`](store.py) | `TraceStore`, `NodeIoStore`, `AgentIoPolicyStore`. Writes are idempotent on replay: span rows insert `ON CONFLICT DO NOTHING` on the deterministic id (the first writer wins, so a resumed run keeps the worker that completed the node), and `node_io` is unique on `(run_id, node_id)`. |
| [`bus.py`](bus.py) | `SpanBus`: the process-local live channel the trace stream subscribes to. No persistence, no replay. |
| [`otlp.py`](otlp.py) | `OtlpSpanSink`: the opt-in export, built only when an endpoint is configured (env `OTEL_EXPORTER_OTLP_ENDPOINT` or the `telemetry.otlp_*` settings). The OpenTelemetry SDK is imported lazily. Exports span scalars only, strips attributes named in `THEYGENT_OTEL_REDACT_ATTRS`, never touches `node_io`. `probe_otlp_endpoint` backs `POST /settings/otlp:test`. |

## Invariants specific to this package

- **`span.node_id` equals the IR node id equals the canvas node id.** The UI's canvas overlay is a
  plain join on it; never mint a different id.
- **The waterfall reads `span`, never the DBOS journal.** Swapping the durable runtime must not
  move it.
- **Per-node I/O stays local.** It is capture-gated (`THEYGENT_IO_CAPTURE`, the
  `telemetry.io_capture*` settings, the agent's IO policy), capped per payload, never exported.
  Check the policy before persisting a new kind of payload.
- **A gated payload is a reason, not an error.** The API reports why it is absent.

## Tests

`tests/test_observability_core.py` (pure algebra and the wrapper), `test_observability_api.py`
(the routes and authorization), `test_observability_durable.py` (worker attribution and replay).
