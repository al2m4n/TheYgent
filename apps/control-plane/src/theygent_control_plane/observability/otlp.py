"""The opt-in OTLP export sink — the *second* sink alongside the in-UI Postgres sink.

The one rule: the in-UI waterfall reads theygent's own ``span`` store and needs **zero external
infrastructure** (air-gapped, self-hosted, localhost all get the full picture). The OTLP exporter is
an **export target, never a dependency** — constructed **only when ``OTEL_EXPORTER_OTLP_ENDPOINT``
is
set**, and the OpenTelemetry SDK is **lazily imported** so the control-plane's core has no new heavy
dep. When the endpoint is set but the SDK isn't installed, this logs a clear warning and stays
off — never crashes a run.

Redaction by construction: the user's full-fidelity I/O stays local in ``node_io`` (never
touched here — this sink only ever sees span *scalars*), and any scalar attribute the user marks
sensitive (``THEYGENT_OTEL_REDACT_ATTRS``) is stripped before export — so the ``span`` row keeps it
locally while the OTLP path does not. This sink applies redaction before forwarding to the
collector.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from theygent_control_plane.observability.spans import Span

logger = logging.getLogger("theygent.control_plane.observability")

#: A sink ``send`` takes (span, redacted_attributes) and ships it to the collector. Injectable so
#: the two-sink/redaction test can assert what crosses the OTLP boundary without the real SDK.
SendFn = Callable[[Span, dict[str, Any]], None]


def _redact_attrs_from_env() -> frozenset[str]:
    raw = os.environ.get("THEYGENT_OTEL_REDACT_ATTRS", "")
    return frozenset(a.strip() for a in raw.split(",") if a.strip())


class OtlpSpanSink:
    """Ships finished spans to the user's collector with sensitive scalars stripped. The
    ``span`` row keeps every attribute locally; this redacted copy is what leaves the machine."""

    def __init__(
        self,
        *,
        send: SendFn,
        redact_attrs: frozenset[str],
        shutdown: Callable[[], None] | None = None,
    ) -> None:
        self._send = send
        self._shutdown = shutdown
        self.redact_attrs = redact_attrs

    def export(self, span: Span) -> None:
        """Redact then ship. Best-effort: a transport failure must never fail the run (the export is
        a side-channel, not the durable record), so it is swallowed with a warning."""
        redacted = {
            k: ("[redacted]" if k in self.redact_attrs else v) for k, v in span.attributes.items()
        }
        try:
            self._send(span, redacted)
        except Exception as exc:  # pragma: no cover - transport is environment-specific
            logger.warning("otlp.export_failed", extra={"span": span.name, "error": str(exc)})

    def shutdown(self) -> None:
        """Flush + stop the underlying exporter. The real exporter batches on a background thread,
        so without this the tail of a run's spans is silently lost at process exit. Best-effort;
        a no-op for an injected test ``send``."""
        if self._shutdown is None:
            return
        try:
            self._shutdown()
        except Exception as exc:  # pragma: no cover - transport is environment-specific
            logger.warning("otlp.shutdown_failed", extra={"error": str(exc)})


def build_otlp_sink(
    *, endpoint: str | None = None, send: SendFn | None = None
) -> OtlpSpanSink | None:
    """Construct the OTLP sink **iff** an endpoint is configured (env set → on, unset → off).
    ``send`` is injectable for tests; otherwise the real OpenTelemetry OTLP/HTTP exporter is built
    **lazily** — if the SDK isn't installed the request degrades to off with a warning (the core has
    no hard OTel dep). Returns ``None`` when export is off, so the caller can assert which
    sinks
    exist (the ``test_two_sink_wiring`` claim)."""
    resolved = endpoint if endpoint is not None else os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not resolved and send is None:
        return None  # the always-local default: only the Postgres sink exists
    redact = _redact_attrs_from_env()
    if send is not None:
        return OtlpSpanSink(send=send, redact_attrs=redact)
    real = _build_real_send(resolved)
    if real is None:
        return None
    real_send, real_shutdown = real
    return OtlpSpanSink(send=real_send, redact_attrs=redact, shutdown=real_shutdown)


def _build_real_send(
    endpoint: str | None,
) -> tuple[SendFn, Callable[[], None]] | None:
    """Lazily build a real OTLP/HTTP exporter-backed ``(send, shutdown)`` pair. Returns ``None``
    (with a warning) if the OpenTelemetry SDK isn't installed — so requesting export without the
    optional dep is honest, not a crash. Kept here so the heavy import never happens on the
    always-local path."""
    try:  # the heavy, OPTIONAL deps — imported only when export is actually requested
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.trace import SpanContext, SpanKind, TraceFlags
        from opentelemetry.trace.status import Status, StatusCode
    except ImportError:
        logger.warning(
            "otlp.sdk_missing",
            extra={
                "endpoint": endpoint,
                "hint": "OTEL_EXPORTER_OTLP_ENDPOINT is set but the OpenTelemetry SDK is not "
                "installed; install the 'otlp' optional dependency to export. The in-UI "
                "waterfall is unaffected (it reads theygent's own span store).",
            },
        )
        return None

    # One batch processor → the OTLP/HTTP exporter. NO tracer/provider: a tracer would mint fresh
    # random trace/span ids, so every span of a run would export as its own unlinked single-span
    # trace. Spans are instead reconstructed as ReadableSpans carrying the SAME deterministic
    # trace/span/parent ids the local store keeps — the exported trace nests exactly like the
    # in-UI waterfall, stable across a crash-resumed run's worker hops.
    processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint))
    resource = Resource.create({"service.name": "theygent-control-plane"})
    sampled = TraceFlags(TraceFlags.SAMPLED)

    def send(span: Span, redacted: dict[str, Any]) -> None:
        trace_id = int(span.trace_id, 16)
        context = SpanContext(
            trace_id=trace_id, span_id=int(span.span_id, 16), is_remote=False, trace_flags=sampled
        )
        parent = (
            SpanContext(
                trace_id=trace_id,
                span_id=int(span.parent_span_id, 16),
                is_remote=False,
                trace_flags=sampled,
            )
            if span.parent_span_id
            else None
        )
        attributes: dict[str, Any] = {
            key: value if isinstance(value, (str, int, float, bool)) else str(value)
            for key, value in redacted.items()
        }
        attributes["theygent.run_id"] = span.run_id
        if span.node_id:
            attributes["theygent.node_id"] = span.node_id
        if span.status:  # the local status vocabulary (ok/err/skipped/running), verbatim
            attributes["theygent.status"] = span.status
        # Worker attribution rides to the collector too — a crash-resumed run's worker hop is
        # exactly the thing a fleet operator's tracing backend should show.
        if span.executor_id:
            attributes["theygent.executor_id"] = span.executor_id
        if span.worker_host:
            attributes["theygent.worker_host"] = span.worker_host
        if span.status == "err":  # a failed node must not look green in the collector
            status = Status(StatusCode.ERROR, span.error or None)
        elif span.status == "ok":
            status = Status(StatusCode.OK)
        else:
            status = Status(StatusCode.UNSET)
        processor.on_end(
            ReadableSpan(
                name=span.name,
                context=context,
                parent=parent,
                resource=resource,
                attributes=attributes,
                events=[],
                links=[],
                kind=SpanKind.INTERNAL,
                status=status,
                start_time=span.start_ns,
                end_time=span.end_ns if span.end_ns is not None else span.start_ns,
            )
        )

    return send, processor.shutdown
