"""The lean gate backend — the rate-limit / quota seam (NOT a metering/billing engine).

Two gate nodes share this DB-backed backend, injected into the walker (``WalkContext.gates``) and
the durable resources so it owns the DB and the walker never opens a session:

* ``ratelimit`` → a trivial fixed-window counter (``gate_counter``): ``hit`` atomically increments
  the current window's count for a scope+key and returns it; the node denies past ``limit``.
* ``quota`` → READS accumulated token usage off existing ``span`` rows (usage read, not re-metered,
  builds no new metering pipeline): ``usage_tokens`` sums ``gen_ai.usage.total_tokens`` across the
  agent's spans in the window; the node denies past ``budget_tokens``.

Granularity is per-KEY now; per-USER is deferred until identity/auth work lands — so quota usage is
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
    """Postgres-backed gate state. Stateless ops over the app's session factory; owns the
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
        """Sum ``gen_ai.usage.total_tokens`` across the agent's spans in the window (read, not
        re-meter). Joins span→run on ``run_id``, filters ``run.graph_id``. The llm capture writes
        the attribute on each ``model.generate`` span from the usage the server reports; when none
        is recorded (an upstream that reports no usage), returns 0 — an honest 'no usage measured →
        allow'. Per-USER attribution is deferred until identity/auth work lands."""
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
