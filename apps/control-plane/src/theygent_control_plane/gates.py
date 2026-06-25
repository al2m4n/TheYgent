"""The lean gate backend — the M19 §2.8/§1.6 seam (NOT a metering/billing engine).

Two gate nodes share this DB-backed backend, injected into the walker (``WalkContext.gates``) and
the durable resources so it owns the DB and the walker never opens a session:

* ``ratelimit`` → a trivial fixed-window counter (``gate_counter``): ``hit`` atomically increments
  the current window's count for a scope+key and returns it; the node denies past ``limit`` (§2.8).
* ``quota`` → READS accumulated token usage off M17 ``span`` rows (§1.6: usage read, not re-metered,
  builds no new metering pipeline): ``usage_tokens`` sums ``gen_ai.usage.total_tokens`` across the
  agent's spans in the window; the node denies past ``budget_tokens``.

Granularity is per-KEY now; per-USER is the deferred identity milestone (§1.6) — so quota usage is
scoped to the AGENT (per-user attribution off spans needs the identity work; recorded, not faked).
"""

from __future__ import annotations

import math

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theygent_control_plane.models import GateCounterRow, RunRow, SpanRow
from theygent_control_plane.run import now


class GateBackend:
    """Postgres-backed gate state (M19 §2.8). Stateless ops over the app's session factory; owns the
    DB so the walker stays session-free (the same injection pattern as the connection resolver +
    telemetry). The counter is the only new table; quota reads existing spans."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    async def hit(self, scope: str, window_seconds: int) -> int:
        """Atomically count one hit for ``scope`` in the current fixed window; return the new count.
        Fixed window: ``window_id = floor(now / window)``; a new window resets the count to 1. The
        upsert is a single statement (ON CONFLICT … CASE), so concurrent hits don't lose a count."""
        ts = now()
        window_seconds = max(1, int(window_seconds))
        window_id = int(ts.timestamp()) // window_seconds
        row_id = f"{scope}:{window_id}"
        stmt = (
            pg_insert(GateCounterRow)
            .values(id=row_id, count=1, window_start=ts, updated_at=ts)
            .on_conflict_do_update(
                index_elements=[GateCounterRow.id],
                set_={"count": GateCounterRow.count + 1, "updated_at": ts},
            )
            .returning(GateCounterRow.count)
        )
        async with self._sm() as session, session.begin():
            count = (await session.execute(stmt)).scalar_one()
        return int(count)

    async def usage_tokens(self, agent_id: str | None, window_seconds: int) -> int:
        """Sum ``gen_ai.usage.total_tokens`` across the agent's spans in the window (M19 §1.6 —
        read, not re-meter). Joins span→run on ``run_id``, filters ``run.graph_id``. When
        no token usage is recorded (the common case until the gateway populates it), returns 0 — an
        honest 'no usage yet → allow'. Per-USER attribution is the deferred identity work (§1.6)."""
        window_seconds = max(1, int(window_seconds))
        since = now().timestamp() - window_seconds
        token_expr = func.coalesce(SpanRow.attributes["gen_ai.usage.total_tokens"].as_float(), 0.0)
        stmt = (
            select(func.coalesce(func.sum(token_expr), 0.0))
            .select_from(SpanRow)
            .join(RunRow, RunRow.id == SpanRow.run_id)
            .where(func.extract("epoch", SpanRow.created_at) >= since)
        )
        if agent_id is not None:
            stmt = stmt.where(RunRow.graph_id == agent_id)
        async with self._sm() as session:
            total = (await session.execute(stmt)).scalar_one()
        return math.floor(float(total or 0.0))
