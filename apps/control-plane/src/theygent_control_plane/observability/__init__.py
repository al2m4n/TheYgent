"""Observability (M17) — the in-UI run waterfall: timing, gaps, per-node I/O, worker attribution.

**The one rule (§0):** one instrumentation seam, two sinks; the UI reads theygent's OWN ``span``
store, never an external trace backend. Spans are emitted once (the capture wrapper) and fan out to:
(1) the always-on local Postgres ``span``/``node_io`` writers + the live :class:`SpanBus` — this
feeds the in-UI waterfall with **zero external infrastructure** (air-gapped/localhost get the full
picture); (2) the **opt-in, redacted** OTLP sink — only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.

The waterfall reads ``span`` (NOT the DBOS journal — §1.2), so swapping the durable runtime later
does not move it. Per-node I/O lives in its own ``node_io`` table (NOT span attributes — §1.3),
lazy-loaded on click, capture-gated (§1.8) and never exported. ``span.node_id == IR node id ==
React Flow node id`` is the frozen join key (§1.6). Worker attribution (``executor_id``/
``worker_host``) records which durable worker handled each span — so a crash-resumed run visibly
hops workers (§1). Reads pass through the ``governance.authorize`` chokepoint (§1.9).
"""

from __future__ import annotations

from theygent_control_plane.observability.bus import SpanBus, SpanEvent
from theygent_control_plane.observability.otlp import OtlpSpanSink, build_otlp_sink
from theygent_control_plane.observability.spans import (
    AgentIoPolicyView,
    CaptureLevel,
    NodeIoView,
    Span,
    SpanView,
    capture_max_bytes,
    deployment_ceiling,
    now_ns,
    resolve_effective_capture,
    topology_default,
)
from theygent_control_plane.observability.store import (
    AgentIoPolicyStore,
    NodeIoStore,
    TraceStore,
)
from theygent_control_plane.observability.telemetry import (
    INPROC_EXECUTOR,
    RunTrace,
    SpanScope,
    Telemetry,
)

__all__ = [
    "INPROC_EXECUTOR",
    "AgentIoPolicyStore",
    "AgentIoPolicyView",
    "CaptureLevel",
    "NodeIoStore",
    "NodeIoView",
    "OtlpSpanSink",
    "RunTrace",
    "Span",
    "SpanBus",
    "SpanEvent",
    "SpanScope",
    "SpanView",
    "Telemetry",
    "TraceStore",
    "build_otlp_sink",
    "capture_max_bytes",
    "deployment_ceiling",
    "now_ns",
    "resolve_effective_capture",
    "topology_default",
]
