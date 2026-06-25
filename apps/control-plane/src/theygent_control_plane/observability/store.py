"""Postgres persistence for the observability tables (M17 §3) — the same M4 discipline as
``RunStore``: stateless ops over a caller-provided ``AsyncSession``, domain shapes out
(:class:`SpanView` / :class:`NodeIoView` / :class:`AgentIoPolicyView`), ORM rows never leak.

Two write paths, both **idempotent on a durable replay** (§4): the span row is keyed by the
deterministic ``span.id`` with **ON CONFLICT DO NOTHING** (first-writer-wins, so a resumed run's
re-opened span does not overwrite the worker that actually completed it — the worker-attribution
demo), and ``node_io`` by ``UNIQUE(run_id, node_id)`` likewise. ``seq`` is allocated per-run
``MAX+1`` at insert (the M4 §3 ordering key) — a run's spans are written sequentially by its single
walk, so no lock is needed (unlike ``append_turn``, which races across instances).
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from theygent_control_plane.models import AgentIoPolicyRow, NodeIoRow, SpanRow
from theygent_control_plane.observability.spans import (
    AgentIoPolicyView,
    CaptureLevel,
    NodeIoView,
    Span,
    SpanView,
    now_ns,
    resolve_effective_capture,
)
from theygent_control_plane.run import new_ulid, now


class TraceStore:
    """Span timeline persistence (the durable record the waterfall reads — §1.2)."""

    async def write_span(self, session: AsyncSession, span: Span) -> None:
        """Persist one finished (or in-flight) span, idempotently (§4). Allocates ``seq`` =
        per-run ``MAX+1`` and inserts ON CONFLICT (id) DO NOTHING — a durable replay re-opens the
        span with the SAME deterministic id, so the re-write is a no-op and the original
        (completing-worker) row stands. Never inside ``@DBOS.transaction`` (m13-dbos §3) — a plain
        data-layer write.

        ``span.seq`` is pre-assigned when the run buffered its spans in memory (the interactive
        flush-at-end path) — use it directly; otherwise (write-on-close, the durable path) allocate
        ``MAX+1`` per run at insert time."""
        if span.seq is not None:
            next_seq: int = span.seq
        else:
            next_seq = (
                await session.execute(
                    select(func.coalesce(func.max(SpanRow.seq), -1) + 1).where(
                        SpanRow.run_id == span.run_id
                    )
                )
            ).scalar_one()
        await session.execute(
            pg_insert(SpanRow)
            .values(
                id=span.id,
                run_id=span.run_id,
                trace_id=span.trace_id,
                otel_span_id=span.span_id,
                parent_span_id=span.parent_span_id,
                node_id=span.node_id,
                node_type=span.node_type,
                kind=span.kind,
                name=span.name,
                phase=span.phase,
                branch_index=span.branch_index,
                status=span.status,
                start_ns=span.start_ns,
                end_ns=span.end_ns,
                attributes=span.attributes or None,
                error=span.error,
                executor_id=span.executor_id,
                worker_host=span.worker_host,
                seq=next_seq,
                created_at=now(),
            )
            .on_conflict_do_nothing(index_elements=[SpanRow.id])
        )

    async def list_spans(self, session: AsyncSession, run_id: str) -> list[SpanView]:
        """Every span for a run, ordered by ``seq`` (M4 §3 — the §5 contract; the waterfall itself
        positions bars by ``start_ns`` and nests by ``parent_span_id``, so list order is just a
        stable hint, not load-bearing for rendering). Joins per-node byte sizes from ``node_io`` so
        the timeline can annotate each transition ("→ 4.2 KB") WITHOUT loading the blob (§3 notes).
        No payloads here — they are lazy (§1.3)."""
        rows = (
            (
                await session.execute(
                    select(SpanRow).where(SpanRow.run_id == run_id).order_by(SpanRow.seq)
                )
            )
            .scalars()
            .all()
        )
        io_sizes = await self._io_sizes(session, run_id)
        out: list[SpanView] = []
        for row in rows:
            sizes = io_sizes.get(row.node_id) if row.node_id else None
            out.append(
                SpanView(
                    id=row.id,
                    run_id=row.run_id,
                    trace_id=row.trace_id,
                    span_id=row.otel_span_id,
                    parent_span_id=row.parent_span_id,
                    node_id=row.node_id,
                    node_type=row.node_type,
                    kind=row.kind,
                    name=row.name,
                    phase=row.phase,
                    branch_index=row.branch_index,
                    status=row.status,
                    start_ns=row.start_ns,
                    end_ns=row.end_ns,
                    attributes=dict(row.attributes) if row.attributes else None,
                    error=row.error,
                    executor_id=row.executor_id,
                    worker_host=row.worker_host,
                    seq=row.seq,
                    bytes_in=sizes[0] if sizes else None,
                    bytes_out=sizes[1] if sizes else None,
                )
            )
        return out

    async def _io_sizes(self, session: AsyncSession, run_id: str) -> dict[str, tuple[int, int]]:
        rows = (
            await session.execute(
                select(NodeIoRow.node_id, NodeIoRow.bytes_in, NodeIoRow.bytes_out).where(
                    NodeIoRow.run_id == run_id
                )
            )
        ).all()
        return {r.node_id: (r.bytes_in, r.bytes_out) for r in rows}

    async def has_spans(self, session: AsyncSession, run_id: str) -> bool:
        return (
            await session.execute(
                select(func.count()).select_from(SpanRow).where(SpanRow.run_id == run_id)
            )
        ).scalar_one() > 0


class NodeIoStore:
    """Per-node I/O persistence (lazy-loaded, never exported — §1.3)."""

    async def write_io(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        node_id: str,
        span_id: str | None,
        capture_level: CaptureLevel,
        inputs: dict[str, Any] | None,
        outputs: dict[str, Any] | None,
        bytes_in: int,
        bytes_out: int,
        truncated: bool,
    ) -> None:
        """Write one ``node_io`` row, idempotently (§4 — ON CONFLICT (run_id, node_id) DO NOTHING,
        so
        a durable replay's re-capture is a no-op). The caller has already resolved + capped payloads
        per the effective capture level (``full`` → payloads + sizes; ``metadata`` → sizes only,
        payloads None; ``off`` → not called at all)."""
        await session.execute(
            pg_insert(NodeIoRow)
            .values(
                id=new_ulid(),
                run_id=run_id,
                node_id=node_id,
                span_id=span_id,
                inputs=inputs,
                outputs=outputs,
                bytes_in=bytes_in,
                bytes_out=bytes_out,
                truncated=truncated,
                capture_level=capture_level,
                created_at=now(),
            )
            .on_conflict_do_nothing(index_elements=[NodeIoRow.run_id, NodeIoRow.node_id])
        )

    async def get_io(self, session: AsyncSession, run_id: str, node_id: str) -> NodeIoView | None:
        row = (
            await session.execute(
                select(NodeIoRow).where(NodeIoRow.run_id == run_id, NodeIoRow.node_id == node_id)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return NodeIoView(
            run_id=row.run_id,
            node_id=row.node_id,
            capture_level=cast(CaptureLevel, row.capture_level),
            inputs=dict(row.inputs) if row.inputs else None,
            outputs=dict(row.outputs) if row.outputs else None,
            bytes_in=row.bytes_in,
            bytes_out=row.bytes_out,
            truncated=row.truncated,
        )


class AgentIoPolicyStore:
    """Per-agent capture policy persistence (§1.8). Keyed to the stable ``agent_id`` — editing it
    never changes ``contentHash`` (M11 immutability). Absent row → the topology default."""

    async def get_policy(self, session: AsyncSession, agent_id: str) -> AgentIoPolicyRow | None:
        return await session.get(AgentIoPolicyRow, agent_id)

    async def get_capture_level(
        self, session: AsyncSession, agent_id: str | None
    ) -> CaptureLevel | None:
        """The stored agent policy level, or ``None`` (→ caller uses the topology default). ``None``
        agent_id (an inline /graphs/runs run with no saved agent) also → ``None``."""
        if not agent_id:
            return None
        row = await session.get(AgentIoPolicyRow, agent_id)
        return cast(CaptureLevel, row.io_capture) if row is not None else None

    async def upsert_policy(
        self,
        session: AsyncSession,
        *,
        agent_id: str,
        io_capture: CaptureLevel,
        io_retention_seconds: int | None,
        redact_rules: dict[str, Any] | None,
        updated_by: str | None,
    ) -> AgentIoPolicyRow:
        ts = now()
        await session.execute(
            pg_insert(AgentIoPolicyRow)
            .values(
                agent_id=agent_id,
                io_capture=io_capture,
                io_retention_seconds=io_retention_seconds,
                redact_rules=redact_rules,
                updated_at=ts,
                updated_by=updated_by,
            )
            .on_conflict_do_update(
                index_elements=[AgentIoPolicyRow.agent_id],
                set_={
                    "io_capture": io_capture,
                    "io_retention_seconds": io_retention_seconds,
                    "redact_rules": redact_rules,
                    "updated_at": ts,
                    "updated_by": updated_by,
                },
            )
        )
        row = await session.get(AgentIoPolicyRow, agent_id)
        assert row is not None
        return row

    def view(
        self,
        *,
        agent_id: str,
        row: AgentIoPolicyRow | None,
        ceiling: CaptureLevel,
        topo_default: CaptureLevel,
    ) -> AgentIoPolicyView:
        """Build the §5/§6 effective+stored policy view. ``effective`` is what actually happens, so
        the UI shows the real behavior; ``capped`` flags when the deployment/topology pins it below
        the stored request (so the user sees "Full requested; capped to Sizes only", §6)."""
        stored: CaptureLevel = (
            cast(CaptureLevel, row.io_capture) if row is not None else topo_default
        )
        effective = resolve_effective_capture(
            ceiling=ceiling,
            topology_default=topo_default,
            agent_policy=cast(CaptureLevel, row.io_capture) if row is not None else None,
        )
        return AgentIoPolicyView(
            agent_id=agent_id,
            io_capture=stored,
            effective=effective,
            capped=effective != stored,
            ceiling=ceiling,
            topology_default=topo_default,
            io_retention_seconds=row.io_retention_seconds if row else None,
            redact_rules=dict(row.redact_rules) if row and row.redact_rules else None,
            updated_at=row.updated_at if row else None,
            has_explicit_policy=row is not None,
        )


def epoch_ns() -> int:
    """Re-export of the ns clock so callers outside ``spans`` can stamp gap/queue timings."""
    return now_ns()
