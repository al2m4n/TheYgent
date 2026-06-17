"""``RunStore`` — Postgres-backed run persistence + thread memory (M4 §4/§5).

Replaces M3's in-memory ``RunRegistry``. Every method takes an ``AsyncSession`` handed in
by the caller, who owns the transaction boundary (§1.2): the read path uses a request
session; the run-execution path opens a transaction per logical operation (so the
post-stream pair-write is atomic on its own). Nothing here commits — the caller does.

Domain/persistence split (§1.3): callers see the Pydantic ``Run`` and plain message
tuples; ``RunRow``/``MessageRow``/``ThreadRow`` never leak out.

Thread memory is **mechanical, not smart** (§4): store the turns, replay them verbatim.
No summarization, no token-budget truncation, no vector retrieval — full replay only.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from theygent_control_plane.models import MessageRow, RunRow, ThreadRow
from theygent_control_plane.run import Run, RunStatus, new_ulid, now


def _to_run(row: RunRow) -> Run:
    """Map a persistence row to the detached domain entity (§1.3)."""
    return Run(
        id=row.id,
        thread_id=row.thread_id,
        # DB columns are untyped str; the lifecycle is constrained at write time.
        status=cast(RunStatus, row.status),
        model=row.model,
        created_at=row.created_at,
        updated_at=row.updated_at,
        error=row.error,
    )


class RunStore:
    """Stateless persistence operations against a caller-provided session."""

    async def create_run(
        self,
        session: AsyncSession,
        *,
        model: str,
        thread_id: str | None,
        params: dict | None,
    ) -> Run:
        run = Run(model=model, thread_id=thread_id)
        session.add(
            RunRow(
                id=run.id,
                thread_id=thread_id,
                status=run.status,
                model=model,
                params=params or None,
                error=None,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
        )
        await session.flush()
        return run

    async def get_run(self, session: AsyncSession, run_id: str) -> Run | None:
        row = await session.get(RunRow, run_id)
        return _to_run(row) if row is not None else None

    async def set_status(
        self,
        session: AsyncSession,
        run_id: str,
        status: RunStatus,
        *,
        error: str | None = None,
    ) -> Run:
        row = await session.get(RunRow, run_id)
        if row is None:  # pragma: no cover - the run is always created first
            raise KeyError(run_id)
        row.status = status
        row.error = error
        row.updated_at = now()
        await session.flush()
        return _to_run(row)

    async def ensure_thread(self, session: AsyncSession, thread_id: str) -> None:
        """Idempotently create the thread row (existing or new — §4). ON CONFLICT DO
        NOTHING so a follow-up run in an existing thread is a no-op, not a PK violation."""
        ts = now()
        await session.execute(
            pg_insert(ThreadRow)
            .values(id=thread_id, created_at=ts, updated_at=ts, meta=None)
            .on_conflict_do_nothing(index_elements=[ThreadRow.id])
        )

    async def load_thread_messages(
        self, session: AsyncSession, thread_id: str
    ) -> list[dict[str, str]]:
        """Prior turns ordered by ``position`` (the ordering key, never a timestamp), as
        OpenAI-shaped ``{role, content}`` dicts ready to prepend to the new input (§4)."""
        rows = (
            await session.execute(
                select(MessageRow.role, MessageRow.content)
                .where(MessageRow.thread_id == thread_id)
                .order_by(MessageRow.position)
            )
        ).all()
        return [{"role": role, "content": content} for role, content in rows]

    async def append_turn(
        self,
        session: AsyncSession,
        *,
        thread_id: str,
        run_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """Append the user+assistant pair at the next two positions.

        Called inside the caller's single success transaction (§4), alongside the run's
        ``completed`` update — so the pair and the run state land together or not at all.
        The thread row is locked first (FOR UPDATE) so concurrent runs in the same thread
        can't pick the same ``position`` (the control-plane scales horizontally — §8)."""
        await session.execute(
            select(ThreadRow.id).where(ThreadRow.id == thread_id).with_for_update()
        )
        next_pos = (
            await session.execute(
                select(func.coalesce(func.max(MessageRow.position), -1) + 1).where(
                    MessageRow.thread_id == thread_id
                )
            )
        ).scalar_one()
        ts = now()
        await session.execute(
            insert(MessageRow),
            [
                {
                    "id": new_ulid(),
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "role": "user",
                    "content": user_content,
                    "position": next_pos,
                    "created_at": ts,
                },
                {
                    "id": new_ulid(),
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "role": "assistant",
                    "content": assistant_content,
                    "position": next_pos + 1,
                    "created_at": ts,
                },
            ],
        )
