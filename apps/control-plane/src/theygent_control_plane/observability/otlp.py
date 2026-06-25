"""The opt-in OTLP export sink — the *second* sink (M17 §0/§1.1/§4).

The one rule (§0): the in-UI waterfall reads theygent's own ``span`` store and needs **zero external
infrastructure** (air-gapped, self-hosted, localhost all get the full picture). The OTLP exporter is
an **export target, never a dependency** — constructed **only when ``OTEL_EXPORTER_OTLP_ENDPOINT``
is
set**, and the OpenTelemetry SDK is **lazily imported** so the control-plane's core has no new heavy
dep (§10). When the endpoint is set but the SDK isn't installed, this logs a clear warning and stays
off — never crashes a run.

Redaction by construction (§1.3/§6): the user's full-fidelity I/O stays local in ``node_io`` (never
touched here — this sink only ever sees span *scalars*), and any scalar attribute the user marks
sensitive (``THEYGENT_OTEL_REDACT_ATTRS``) is stripped before export — so the ``span`` row keeps it
locally while the OTLP path does not. This is the ``RedactingSpanProcessor`` of §4, as a sink.
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
    """Ships finished spans to the user's collector with sensitive scalars stripped (§1.3). The
    ``span`` row keeps every attribute locally; this redacted copy is what leaves the machine."""

    def __init__(self, *, send: SendFn, redact_attrs: frozenset[str]) -> None:
        self._send = send
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


def build_otlp_sink(
    *, endpoint: str | None = None, send: SendFn | None = None
) -> OtlpSpanSink | None:
    """Construct the OTLP sink **iff** an endpoint is configured (§1.1: env set → on, unset → off).
    ``send`` is injectable for tests; otherwise the real OpenTelemetry OTLP/HTTP exporter is built
    **lazily** — if the SDK isn't installed the request degrades to off with a warning (the core has
    no hard OTel dep, §10). Returns ``None`` when export is off, so the caller can assert which
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
    return OtlpSpanSink(send=real, redact_attrs=redact)


def _build_real_send(endpoint: str | None) -> SendFn | None:
    """Lazily build a real OTLP/HTTP exporter-backed ``send``. Returns ``None`` (with a warning) if
    the OpenTelemetry SDK isn't installed — so requesting export without the optional dep is honest,
    not a crash. Kept here so the heavy import never happens on the always-local path."""
    try:  # the heavy, OPTIONAL deps — imported only when export is actually requested
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
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

    # A dedicated, NON-global provider (never trace.set_tracer_provider — process-global would
    # collide with the many-apps-per-process fast suite, the same hazard DBOS has). One batch
    # processor → the OTLP/HTTP exporter. The redacted attrs ride on the emitted OTel span.
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    tracer = provider.get_tracer("theygent.control_plane")

    def send(span: Span, redacted: dict[str, Any]) -> None:
        otel_span = tracer.start_span(span.name, start_time=span.start_ns)
        for key, value in redacted.items():
            otel_span.set_attribute(
                key, value if isinstance(value, (str, int, float, bool)) else str(value)
            )
        otel_span.set_attribute("theygent.run_id", span.run_id)
        if span.node_id:
            otel_span.set_attribute("theygent.node_id", span.node_id)
        otel_span.end(end_time=span.end_ns if span.end_ns is not None else span.start_ns)
        # ReadableSpan import kept for type-completeness of the export contract above.
        _ = ReadableSpan

    return send
