"""Span value types, id minting, and the capture-policy algebra.

Plain data + pure functions — no DB, no OTel, no DBOS. The wrapper (``telemetry.py``) builds a
:class:`Span` per node/phase and hands it to the sinks; the API maps :class:`SpanView` /
:class:`NodeIoView` / :class:`AgentIoPolicyView` out (domain shapes, like ``run.Run`` — never ORM
rows). ``resolve_effective_capture`` is the effective-capture precedence (deployment ceiling ∧
topology default ∧ agent policy), kept here as one tested function so the wrapper and the API agree.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

# ── capture-policy algebra ───────────────────────────────────────────────────
# Whether a node's raw I/O is persisted is a per-agent decision, capped by the deployment and the
# topology. The three levels are totally ordered by restrictiveness; ``min_capture`` is the meet.
CaptureLevel = Literal["off", "metadata", "full"]
_CAPTURE_RANK: dict[str, int] = {"off": 0, "metadata": 1, "full": 2}
_RANK_CAPTURE: dict[int, CaptureLevel] = {0: "off", 1: "metadata", 2: "full"}


def min_capture(*levels: CaptureLevel) -> CaptureLevel:
    """The most restrictive of ``levels`` (the meet on off < metadata < full)."""
    return _RANK_CAPTURE[min(_CAPTURE_RANK[lvl] for lvl in levels)]


def resolve_effective_capture(
    *, ceiling: CaptureLevel, topology_default: CaptureLevel, agent_policy: CaptureLevel | None
) -> CaptureLevel:
    """The effective capture level for a run. ``ceiling`` is the deployment hard cap (the
    ``THEYGENT_IO_CAPTURE`` env var —
    nothing exceeds it). The agent's explicit policy, if set, applies up to the ceiling — so on a
    hosted topology an agent may *opt into* ``full`` above the ``metadata`` topology default.
    Absent agent policy falls back to the ``topology_default`` (local → ``full``; hosted →
    ``metadata``,
    the sovereignty default — raw payloads never land in theygent's cloud Postgres by default).

    So: ``effective = min(ceiling, agent_policy if set else topology_default)``. An agent may only
    be
    *more* restrictive than the ceiling, never less (``test_policy_precedence``)."""
    chosen: CaptureLevel = agent_policy if agent_policy is not None else topology_default
    return min_capture(ceiling, chosen)


def deployment_ceiling() -> CaptureLevel:
    """The deployment hard cap: ``THEYGENT_IO_CAPTURE`` (off|metadata|full), default ``full``
    for the localhost daily-driver. ``off`` disables payload capture entirely (spans still flow)."""
    raw = (os.environ.get("THEYGENT_IO_CAPTURE") or "full").strip().lower()
    return raw if raw in _CAPTURE_RANK else "full"  # type: ignore[return-value]


def topology_default() -> CaptureLevel:
    """The topology default: ``THEYGENT_TOPOLOGY`` (local|hosted), default ``local``. Local /
    self-hosted → ``full`` allowed (the user's own machine); hosted (Pro/Team) → ``metadata`` unless
    an agent explicitly opts into ``full`` (sovereignty: raw payloads never default into the
    cloud)."""
    return (
        "metadata"
        if (os.environ.get("THEYGENT_TOPOLOGY") or "local").strip().lower() == "hosted"
        else "full"
    )


def capture_max_bytes() -> int:
    """The per-payload cap: ``THEYGENT_IO_CAPTURE_MAX_BYTES``, default 256 KiB. Over-cap
    payloads are truncated with ``truncated=true`` and the true byte count — never silently
    dropped."""
    raw = os.environ.get("THEYGENT_IO_CAPTURE_MAX_BYTES")
    try:
        return int(raw) if raw else 256 * 1024
    except ValueError:
        return 256 * 1024


# ── id minting + clock (OTel-shaped hex ids; ns clock) ───────────────────────


def now_ns() -> int:
    """Epoch nanoseconds — the waterfall's clock. ``end-start`` = duration, ``next.start -
    prev.end`` = gap; clean integer arithmetic, sub-ms precision."""
    return time.time_ns()


def span_pk(
    run_id: str, *, node_id: str | None, phase: str | None, branch_index: int | None
) -> str:
    """The deterministic span PRIMARY KEY (idempotency). A resumed durable run re-executes the
    workflow body and re-opens every span; a deterministic id + ``ON CONFLICT DO NOTHING`` makes the
    redundant re-write a no-op AND preserves the row written by the worker that actually completed
    the step (first-writer-wins) — so a crash-resumed run visibly hops workers instead of
    overwriting history. ``{run_id}:{node_id or '_root'}[:{phase}][:#{branch}]``."""
    parts = [run_id, node_id or "_root"]
    if phase:
        parts.append(phase)
    if branch_index is not None:
        parts.append(f"#{branch_index}")
    return ":".join(parts)


def derive_trace_id(run_id: str) -> str:
    """A 32-hex-char OTel-shaped trace id, **deterministic per run** so every span of a run — even
    ones written by different workers across a crash/resume — shares one trace_id and a stable
    parent
    chain (a random id would re-roll on the resuming worker's ``begin_run`` and split the trace)."""
    return hashlib.sha256(f"trace:{run_id}".encode()).hexdigest()[:32]


def derive_span_id(pk: str) -> str:
    """A 16-hex-char OTel-shaped span id, **deterministic from the span PK** — so a span's id (and
    thus its children's ``parent_span_id``) is identical on every replay/worker."""
    return hashlib.sha256(f"span:{pk}".encode()).hexdigest()[:16]


# ── the in-flight span the wrapper builds, then persists/streams ─────────────


@dataclass
class Span:
    """One span the wrapper opens, fills, and closes (a run-root, a node, or a phase). Carries
    GenAI-semconv SCALARS in ``attributes`` (model, token counts, finish_reason, ttft_ms, …) — NEVER
    payloads (those go to ``node_io``). ``executor_id``/``worker_host`` are the worker
    attribution."""

    id: str
    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    node_id: str | None = None
    node_type: str | None = None
    kind: str | None = None
    phase: str | None = None
    branch_index: int | None = None
    status: str = "running"  # ok | err | skipped | running
    start_ns: int = field(default_factory=now_ns)
    end_ns: int | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    executor_id: str | None = None
    worker_host: str | None = None
    # Set when the run buffers spans in memory and flushes at the end (the interactive path) — then
    # the batch write uses this pre-assigned order instead of a per-row MAX+1 query. ``None`` →
    # write-on-close (the durable path) allocates seq at insert time.
    seq: int | None = None


# ── payload sizing + capping ─────────────────────────────────────────────────


def _json_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str).encode("utf-8"))
    except (TypeError, ValueError, RecursionError):
        try:
            return len(str(value).encode("utf-8"))
        except Exception:  # even repr can blow up (recursive __str__) — size unknown, not fatal
            return 0


def cap_payload(value: Any, max_bytes: int) -> tuple[Any, int, bool]:
    """Return ``(stored_value, true_byte_count, truncated)`` for one payload. Under the cap, the
    value passes through unchanged. Over the cap, it is replaced by a truncated preview + the true
    byte count (never silently dropped), so the drawer can show "first N bytes of M".

    Every serialization here is guarded: a pathological value (circular reference, non-string
    dict keys, absurd nesting) must degrade the CAPTURE, never kill the run that produced it —
    the sizing above already fell back on exactly the error the preview dump would re-raise."""
    raw = _json_bytes(value)
    if raw <= max_bytes:
        return value, raw, False
    try:
        preview = json.dumps(value, default=str)[: max(0, max_bytes)]
    except (TypeError, ValueError, RecursionError):
        try:
            preview = str(value)[: max(0, max_bytes)]
        except Exception:
            preview = "<unserializable>"
    return {"_truncated": True, "_bytes": raw, "_preview": preview}, raw, True


# ── domain read models (the API maps these out; never ORM rows) ──────────────


class SpanView(BaseModel):
    """A waterfall row (GET /runs/{id}/trace). Lightweight: timing + status + scalar attrs +
    worker attribution; NO payloads. The frontend positions a bar from ``start_ns``/``end_ns`` and
    indents by the ``parent_span_id`` chain."""

    id: str
    run_id: str
    trace_id: str
    span_id: str
    parent_span_id: str | None = None
    node_id: str | None = None
    node_type: str | None = None
    kind: str | None = None
    name: str
    phase: str | None = None
    branch_index: int | None = None
    status: str
    start_ns: int
    end_ns: int | None = None
    attributes: dict[str, Any] | None = None
    error: str | None = None
    executor_id: str | None = None
    worker_host: str | None = None
    seq: int
    # Convenience for the edge-size annotations + drawer header (joined from node_io).
    bytes_in: int | None = None
    bytes_out: int | None = None


class NodeIoView(BaseModel):
    """The click-through payload (GET /runs/{id}/nodes/{nodeId}/io). ``capture_level`` and
    ``reason`` drive the drawer's gated states (full / sizes-only / off / not-permitted), so the
    timeline always renders and only the payloads are gated — never a 500."""

    run_id: str
    node_id: str
    capture_level: CaptureLevel
    inputs: dict[str, Any] | None = None
    outputs: dict[str, Any] | None = None
    bytes_in: int = 0
    bytes_out: int = 0
    truncated: bool = False
    reason: str | None = None  # why payloads are absent (capture off/metadata, or not permitted)


class AgentIoPolicyView(BaseModel):
    """The effective + stored capture policy for an agent (GET/PUT /agents/{id}/io-policy).
    ``effective`` is what actually happens (ceiling ∧ topology ∧ stored) so the UI shows the real
    behavior, not a lie; ``capped`` flags when the deployment/topology pins it below the request."""

    agent_id: str
    io_capture: CaptureLevel  # the stored agent policy (or the topology default if no row)
    effective: CaptureLevel  # what actually happens after the ceiling/topology meet
    capped: bool  # effective < io_capture because the deployment/topology limits it
    ceiling: CaptureLevel
    topology_default: CaptureLevel
    io_retention_seconds: int | None = None
    redact_rules: dict[str, Any] | None = None
    updated_at: datetime | None = None
    has_explicit_policy: bool = False  # False → using the topology default (no stored row)
