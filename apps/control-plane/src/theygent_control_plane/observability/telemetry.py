"""The capture wrapper — the one new piece of execution code (M17 §4), shared by both runtimes.

``Telemetry`` is the per-process observability resource (one per app / durable runtime). It owns the
two always-local sinks (the Postgres ``span``/``node_io`` writers + the live :class:`SpanBus`), the
optional OTLP sink (§1.1, opt-in), the worker identity (process ``host:pid``), and the capture
policy bounds (deployment ceiling ∧ topology default — §1.8). It hands out a :class:`RunTrace` per
run, which opens a run-root span and a :class:`SpanScope` per node; the scope is the wrapper that
**opens an OTel-shaped span, records timing, sets GenAI-semconv scalars + the worker that handled
it,
writes the ``span`` row + ``node_io`` per the effective capture policy, and fans the close event
onto
the live bus + the OTLP sink** (§4). It is **runtime-agnostic** (no ``dbos`` import) so it runs
identically under the interactive M5 walker and the durable DBOS step body — the §1.5 "one wrapper,
both runtimes" rule. It writes through the **normal async data layer**, never inside
``@DBOS.transaction`` (m13-dbos §3).

Idempotency on resume (§4): spans/io are written ONCE, on close, keyed by deterministic ids with
ON CONFLICT DO NOTHING — first-writer-wins. A resumed durable run re-opens every span on the new
worker, but the completed ones' rows already stand (with the worker that finished them pre-crash),
so the waterfall **visibly hops workers** at the resume point instead of overwriting history. The
only spans written post-crash are the ones that actually (re-)completed there.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
from collections.abc import AsyncIterator, Mapping
from typing import Any

from theygent_control_plane.observability.bus import SpanBus
from theygent_control_plane.observability.otlp import OtlpSpanSink
from theygent_control_plane.observability.spans import (
    CaptureLevel,
    Span,
    cap_payload,
    capture_max_bytes,
    deployment_ceiling,
    derive_span_id,
    derive_trace_id,
    now_ns,
    resolve_effective_capture,
    span_pk,
    topology_default,
)
from theygent_control_plane.observability.store import (
    AgentIoPolicyStore,
    NodeIoStore,
    TraceStore,
)

logger = logging.getLogger("theygent.control_plane.observability")

# The interactive M5 walker has no DBOS executor — its spans are stamped with this sentinel so the
# waterfall can still say "handled by: in-process" (vs a named durable worker — the §1 attribution).
INPROC_EXECUTOR = "inproc"


def _stringify_attr(value: Any) -> Any:
    """OTLP scalar coercion — keep numbers/bools/strings, JSON-ify the rest (defensive; we only ever
    set scalars on spans, never payloads — §1.3)."""
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


class _RunBuffer:
    """In-memory accumulator for a run that flushes its spans + ``node_io`` to Postgres ONCE at the
    end, instead of a transaction between every node. This is what keeps the walk from blocking on a
    DB round-trip per step — the inter-step gaps on a fast graph were largely those writes. Keyed by
    ``run_id`` on the :class:`Telemetry`.

    **Interactive path only.** The durable worker deliberately does NOT buffer: write-on-close is
    what lets a crash-resumed run keep the row written by the worker that first completed each step
    (first-writer-wins → the visible worker hop), and survive a crash that happens before any flush.
    Buffering would re-stamp every span with the resuming worker and lose a crashed run's trace."""

    def __init__(self) -> None:
        self.spans: list[Span] = []
        self.ios: list[dict[str, Any]] = []
        self._seq = 0

    def next_seq(self) -> int:
        s = self._seq
        self._seq += 1
        return s


class SpanScope:
    """One open span the wrapper hands the walk to fill (a node, a phase, or the run root). The walk
    sets scalar attributes, the resolved I/O (node spans only), and the ok/err status; on close the
    scope writes the ``span`` row + ``node_io`` (per the effective capture level), publishes the
    live
    close event, and ships the redacted copy to the OTLP sink. NEVER stores payloads in span
    attributes (§1.3) — payloads go to ``node_io`` only, capped + truncated (§1.7)."""

    def __init__(
        self,
        telemetry: Telemetry,
        *,
        run_id: str,
        name: str,
        node_id: str | None,
        node_type: str | None,
        kind: str | None,
        phase: str | None,
        branch_index: int | None,
        parent_span_id: str | None,
        trace_id: str,
        executor_id: str,
        capture_level: CaptureLevel,
    ) -> None:
        self._t = telemetry
        pk = span_pk(run_id, node_id=node_id, phase=phase, branch_index=branch_index)
        self.span = Span(
            id=pk,
            run_id=run_id,
            trace_id=trace_id,
            span_id=derive_span_id(pk),
            parent_span_id=parent_span_id,
            name=name,
            node_id=node_id,
            node_type=node_type,
            kind=kind,
            phase=phase,
            branch_index=branch_index,
            start_ns=now_ns(),
            executor_id=executor_id,
            worker_host=telemetry.worker_host,
        )
        self._capture_level = capture_level
        self._inputs: dict[str, Any] | None = None
        self._outputs: dict[str, Any] | None = None
        self._explicit_status: str | None = None

    @property
    def span_id(self) -> str:
        return self.span.span_id

    @property
    def trace_id(self) -> str:
        return self.span.trace_id

    def set_attributes(self, attrs: Mapping[str, Any]) -> None:
        """Set GenAI-semconv SCALARS (model, token counts, finish_reason, ttft_ms, …). NEVER
        payloads — those go through :meth:`set_io`."""
        for k, v in attrs.items():
            self.span.attributes[k] = _stringify_attr(v)

    def set_status(self, status: str) -> None:
        self._explicit_status = status

    def set_error(self, message: str) -> None:
        self._explicit_status = "err"
        self.span.error = message

    def set_io(
        self, *, inputs: dict[str, Any] | None = None, outputs: dict[str, Any] | None = None
    ) -> None:
        """Record the node's RESOLVED (post-``$in``) per-port I/O (§3). Always recorded in memory;
        what is *persisted* on close is governed by the effective capture level (full → payloads +
        sizes; metadata → sizes only; off → no row)."""
        if inputs is not None:
            self._inputs = inputs
        if outputs is not None:
            self._outputs = outputs

    def child_phase(self, phase: str, name: str | None = None) -> _SpanCM:
        """Open a child PHASE span (queue.wait / model.generate / model.load / mcp.connect — §2),
        parented to this span. Phase spans carry timing only — no ``node_io``."""
        return self._t._span_cm(
            run_id=self.span.run_id,
            name=name or phase,
            node_id=self.span.node_id,
            node_type=None,
            kind=None,
            phase=phase,
            branch_index=self.span.branch_index,
            parent_span_id=self.span.span_id,
            trace_id=self.span.trace_id,
            executor_id=self.span.executor_id or INPROC_EXECUTOR,
            capture_level="off",  # phase spans never capture I/O
        )

    async def _close(self, *, error_status: bool) -> None:
        self.span.end_ns = now_ns()
        if self._explicit_status is not None:
            self.span.status = self._explicit_status
        elif error_status:
            self.span.status = "err"
        else:
            self.span.status = "ok"
        bytes_in, bytes_out, truncated, inputs_store, outputs_store = self._materialize_io()
        if bytes_in is not None:
            self.span.attributes.setdefault("theygent.bytes_in", bytes_in)
        if bytes_out is not None:
            self.span.attributes.setdefault("theygent.bytes_out", bytes_out)
        # The node_io row to persist (node spans only, per capture level; phase/root never).
        io_kwargs: dict[str, Any] | None = None
        if (
            self.span.node_id is not None
            and self.span.phase is None
            and self._capture_level != "off"
        ):
            io_kwargs = {
                "run_id": self.span.run_id,
                "node_id": self.span.node_id,
                "span_id": self.span.id,
                "capture_level": self._capture_level,
                "inputs": inputs_store,
                "outputs": outputs_store,
                "bytes_in": bytes_in or 0,
                "bytes_out": bytes_out or 0,
                "truncated": truncated,
            }
        # 1) persist the span + node_io. BUFFERED (interactive): accumulate in memory and flush at
        # run end, so the walk never blocks on a DB write between nodes (the gaps). WRITE-ON-CLOSE
        # (durable): write now — first-writer-wins keeps the per-worker, crash-resilient rows.
        buf = self._t._buffers.get(self.span.run_id)
        if buf is not None:
            self.span.seq = buf.next_seq()
            buf.spans.append(self.span)
            if io_kwargs is not None:
                buf.ios.append(io_kwargs)
        else:
            await self._t._write_span(self.span)
            if io_kwargs is not None:
                await self._t._write_io(**io_kwargs)
        # 2) live close + OTLP export (always — both best-effort side-channels, never buffered, so
        # the live /trace/stream waterfall still grows in real time regardless of the flush policy).
        self._t.bus.publish(self.span.run_id, "close", _span_payload(self.span))
        if self._t.otlp is not None:
            self._t.otlp.export(self.span)

    def _materialize_io(
        self,
    ) -> tuple[int | None, int | None, bool, dict[str, Any] | None, dict[str, Any] | None]:
        """Compute byte sizes + (per capture level) the stored payloads. ``full`` stores capped
        payloads + sizes; ``metadata`` stores sizes only (payloads ``None``); ``off`` stores
        nothing (no row written). A node with no recorded I/O yields all-None."""
        if self.span.node_id is None or self.span.phase is not None:
            return None, None, False, None, None
        if self._inputs is None and self._outputs is None:
            return None, None, False, None, None
        max_bytes = self._t.max_bytes
        in_store, bytes_in, trunc_in = _cap_port_map(self._inputs, max_bytes)
        out_store, bytes_out, trunc_out = _cap_port_map(self._outputs, max_bytes)
        truncated = trunc_in or trunc_out
        if self._capture_level == "metadata":  # sizes only, no payloads (§4)
            return bytes_in, bytes_out, truncated, None, None
        return bytes_in, bytes_out, truncated, in_store, out_store


def _cap_port_map(
    port_map: dict[str, Any] | None, max_bytes: int
) -> tuple[dict[str, Any] | None, int, bool]:
    """Cap each port's value independently (§1.7), returning (stored_map, total_bytes,
    truncated)."""
    if not port_map:
        return None, 0, False
    stored: dict[str, Any] = {}
    total = 0
    truncated = False
    for port, value in port_map.items():
        capped, raw, was_trunc = cap_payload(value, max_bytes)
        stored[port] = capped
        total += raw
        truncated = truncated or was_trunc
    return stored, total, truncated


def _span_payload(span: Span) -> dict[str, Any]:
    """The lightweight live/SSE shape (no payloads — §1.3). **snake_case**, identical in field names
    to :class:`SpanView` so the frontend merges /trace (persisted) and /trace/stream (live) into one
    shape — matching the existing theygent API convention (``/runs`` is snake_case too)."""
    return {
        "id": span.id,
        "span_id": span.span_id,
        "parent_span_id": span.parent_span_id,
        "node_id": span.node_id,
        "node_type": span.node_type,
        "kind": span.kind,
        "name": span.name,
        "phase": span.phase,
        "branch_index": span.branch_index,
        "status": span.status,
        "start_ns": span.start_ns,
        "end_ns": span.end_ns,
        "executor_id": span.executor_id,
        "worker_host": span.worker_host,
        "attributes": span.attributes or None,
        "error": span.error,
    }


class _SpanCM:
    """Async context manager around a :class:`SpanScope`: publishes the live ``open`` on enter,
    closes (writing rows + the live ``close``) on exit, marking ``err`` if the body raised."""

    def __init__(self, telemetry: Telemetry, scope: SpanScope) -> None:
        self._t = telemetry
        self.scope = scope

    async def __aenter__(self) -> SpanScope:
        self._t.bus.publish(self.scope.span.run_id, "open", _span_payload(self.scope.span))
        return self.scope

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        await self.scope._close(error_status=exc_type is not None)
        return False  # never suppress — telemetry observes, it does not swallow the run's errors


class RunTrace:
    """The per-run handle a walk drives (M17 §4). Opens the run-root span (the waterfall's t0
    anchor)
    and hands out a node span per node. ``executor_id`` is the worker that ran this walk —
    ``inproc``
    for the interactive walker, the DBOS executor id for the durable worker (worker attribution)."""

    def __init__(
        self,
        telemetry: Telemetry,
        *,
        run_id: str,
        executor_id: str,
        capture_level: CaptureLevel,
    ) -> None:
        self._t = telemetry
        self.run_id = run_id
        self.executor_id = executor_id
        self.capture_level = capture_level
        self.trace_id = derive_trace_id(run_id)
        self._root_pk = span_pk(run_id, node_id=None, phase=None, branch_index=None)
        self._root_span_id = derive_span_id(self._root_pk)
        self._root_start_ns = now_ns()
        # Announce the root on the live bus so the waterfall has its t0 immediately (the row itself
        # is written once, on finish — write-on-close keeps resume idempotency, §4).
        self._t.bus.publish(
            run_id,
            "open",
            {
                "id": self._root_pk,
                "span_id": self._root_span_id,
                "parent_span_id": None,
                "name": run_id,
                "node_id": None,
                "phase": None,
                "status": "running",
                "start_ns": self._root_start_ns,
                "end_ns": None,
                "executor_id": executor_id,
                "worker_host": self._t.worker_host,
            },
        )

    def node_span(self, node: Any) -> _SpanCM:
        """Open a node span (parented to the run root). ``node`` is an IR ``Node`` (has
        id/type/kind);
        the wrapper stamps ``span.node_id == node.id == React Flow node id`` (the §1.6 frozen join
        key) so the waterfall later overlays straight onto the M15 canvas."""
        return self._t._span_cm(
            run_id=self.run_id,
            name=node.id,
            node_id=node.id,
            node_type=node.type,
            kind=node.kind,
            phase=None,
            branch_index=None,
            parent_span_id=self._root_span_id,
            trace_id=self.trace_id,
            executor_id=self.executor_id,
            capture_level=self.capture_level,
        )

    def branch_span(self, node: Any, branch_index: int) -> _SpanCM:
        """Open a per-iteration span for a ``loop``/``map`` node (M14 §2) so the trace reads against
        the drawn graph one bar per branch. Named ``<node_id>#<i>``; parented to the run root."""
        return self._t._span_cm(
            run_id=self.run_id,
            name=f"{node.id}#{branch_index}",
            node_id=node.id,
            node_type=node.type,
            kind=node.kind,
            phase=None,
            branch_index=branch_index,
            parent_span_id=self._root_span_id,
            trace_id=self.trace_id,
            executor_id=self.executor_id,
            capture_level="off",  # the branch is a child workflow with its own captured run
        )

    async def skipped(self, node: Any) -> None:
        """Record a SKIPPED node (a dead branch this run — m6.md §4) as a zero-width span so the
        waterfall greys it in place (status=skipped). No I/O, no body."""
        scope = self._make_scope(
            name=node.id,
            node_id=node.id,
            node_type=node.type,
            kind=node.kind,
            phase=None,
            branch_index=None,
            parent_span_id=self._root_span_id,
            capture_level="off",
        )
        scope.set_status("skipped")
        await scope._close(error_status=False)

    async def emit_queue_wait(self, enqueued_ns: int) -> None:
        """Emit the ``queue.wait`` phase span (§2): enqueue → workflow pickup, the often-biggest gap
        on the durable path. A run-level phase (it precedes every node), parented to the run root,
        spanning ``[enqueued_ns, now]``."""
        scope = self._make_scope(
            name="queue.wait",
            node_id=None,
            node_type=None,
            kind=None,
            phase="queue_wait",
            branch_index=None,
            parent_span_id=self._root_span_id,
            capture_level="off",
        )
        scope.span.start_ns = enqueued_ns
        scope.set_status("ok")
        await scope._close(error_status=False)

    async def finish(self, *, status: str = "ok", error: str | None = None) -> None:
        """Close the run-root span (status mirrors the run outcome), persist it, and signal
        end-of-stream to live subscribers."""
        scope = self._make_scope(
            name=self.run_id,
            node_id=None,
            node_type=None,
            kind=None,
            phase=None,
            branch_index=None,
            parent_span_id=None,
            capture_level="off",
        )
        scope.span.start_ns = self._root_start_ns
        scope.set_status(status)
        if error is not None:
            scope.span.error = error
        await scope._close(error_status=status == "err")
        # Flush the buffered run in ONE transaction (no-op for the write-on-close durable path). The
        # root span was just appended by its _close, so it rides in the same batch.
        await self._t._flush_run(self.run_id)
        self._t.bus.close(self.run_id)

    def _make_scope(
        self,
        *,
        name: str,
        node_id: str | None,
        node_type: str | None,
        kind: str | None,
        phase: str | None,
        branch_index: int | None,
        parent_span_id: str | None,
        capture_level: CaptureLevel,
    ) -> SpanScope:
        return SpanScope(
            self._t,
            run_id=self.run_id,
            name=name,
            node_id=node_id,
            node_type=node_type,
            kind=kind,
            phase=phase,
            branch_index=branch_index,
            parent_span_id=parent_span_id,
            trace_id=self.trace_id,
            executor_id=self.executor_id,
            capture_level=capture_level,
        )


class Telemetry:
    """The per-process observability resource (M17 §4). Owns the sinks, the worker identity, and the
    capture-policy bounds; hands out a :class:`RunTrace` per run. Threaded into the interactive
    ``WalkContext`` and the durable ``DurableResources`` so the SAME wrapper runs in both."""

    def __init__(
        self,
        *,
        sessionmaker: Any,
        span_bus: SpanBus | None = None,
        otlp_sink: OtlpSpanSink | None = None,
        ceiling: CaptureLevel | None = None,
        topology: CaptureLevel | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.bus = span_bus or SpanBus()
        self.otlp = otlp_sink
        self.ceiling: CaptureLevel = ceiling or deployment_ceiling()
        self.topology_default: CaptureLevel = topology or topology_default()
        self.max_bytes = max_bytes or capture_max_bytes()
        self.worker_host = f"{socket.gethostname()}:{os.getpid()}"
        self.trace_store = TraceStore()
        self.io_store = NodeIoStore()
        self.policy_store = AgentIoPolicyStore()
        # Per-run in-memory span/node_io buffers (interactive flush-at-end). Keyed by run_id; a run
        # is present here only between ``begin_run(buffered=True)`` and ``finish``.
        self._buffers: dict[str, _RunBuffer] = {}

    @property
    def otlp_enabled(self) -> bool:
        """Whether the opt-in OTLP sink is wired (the ``test_two_sink_wiring`` claim — §7)."""
        return self.otlp is not None

    def resolve_capture(self, agent_policy: CaptureLevel | None) -> CaptureLevel:
        """The §1.8 effective capture level (ceiling ∧ topology default ∧ agent policy)."""
        return resolve_effective_capture(
            ceiling=self.ceiling,
            topology_default=self.topology_default,
            agent_policy=agent_policy,
        )

    async def effective_capture_for(self, agent_id: str | None) -> CaptureLevel:
        """Resolve the effective capture level for a run, loading the agent's policy once (§4 — the
        wrapper resolves the level once per run, not per node). ``None`` agent_id (inline graph run,
        no saved agent) → the topology default under the ceiling."""
        async with self.sessionmaker() as session:
            agent_policy = await self.policy_store.get_capture_level(session, agent_id)
        return self.resolve_capture(agent_policy)

    def begin_run(
        self,
        run_id: str,
        *,
        executor_id: str = INPROC_EXECUTOR,
        capture_level: CaptureLevel = "full",
        buffered: bool = False,
    ) -> RunTrace:
        """Open a run trace. ``buffered=True`` (the interactive walker) accumulates spans/node_io in
        memory and flushes once at ``finish`` — so the walk never blocks on a per-node DB write.
        ``buffered=False`` (the durable worker, the default) writes each span on close, preserving
        the per-worker, crash-resilient rows the resume waterfall needs."""
        if buffered:
            self._buffers[run_id] = _RunBuffer()
        return RunTrace(self, run_id=run_id, executor_id=executor_id, capture_level=capture_level)

    def _span_cm(self, **kwargs: Any) -> _SpanCM:
        return _SpanCM(self, SpanScope(self, **kwargs))

    async def _flush_run(self, run_id: str) -> None:
        """Flush a buffered run's spans + node_io in ONE transaction (no-op for a write-on-close
        run, which has no buffer). Best-effort — telemetry never fails the run it observes."""
        buf = self._buffers.pop(run_id, None)
        if buf is None or (not buf.spans and not buf.ios):
            return
        try:
            async with self.sessionmaker() as session, session.begin():
                for span in buf.spans:
                    await self.trace_store.write_span(session, span)
                for io in buf.ios:
                    await self.io_store.write_io(session, **io)
        except Exception as exc:  # pragma: no cover - defensive; observability never fails the run
            logger.warning("trace.flush_failed", extra={"run_id": run_id, "error": str(exc)})

    async def _write_span(self, span: Span) -> None:
        try:
            async with self.sessionmaker() as session, session.begin():
                await self.trace_store.write_span(session, span)
        except Exception as exc:  # telemetry must never fail the run it observes
            logger.warning("span.write_failed", extra={"span": span.id, "error": str(exc)})

    async def _write_io(self, **kwargs: Any) -> None:
        try:
            async with self.sessionmaker() as session, session.begin():
                await self.io_store.write_io(session, **kwargs)
        except Exception as exc:  # pragma: no cover - defensive; observability never fails the run
            logger.warning(
                "node_io.write_failed",
                extra={
                    "run_id": kwargs.get("run_id"),
                    "node_id": kwargs.get("node_id"),
                    "error": str(exc),
                },
            )


@contextlib.asynccontextmanager
async def nullcontext_scope() -> AsyncIterator[None]:
    """A no-op async CM for code paths that may run without telemetry wired (defensive)."""
    yield None
