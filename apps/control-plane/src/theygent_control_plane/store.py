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

from sqlalchemy import func, insert, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from theygent_control_plane.models import MessageRow, RunRow, ThreadRow
from theygent_control_plane.run import (
    Run,
    RunStatus,
    ThreadDetail,
    ThreadMessage,
    ThreadSummary,
    new_ulid,
    now,
)


def _to_run(row: RunRow) -> Run:
    """Map a persistence row to the detached domain entity (§1.3)."""
    return Run(
        id=row.id,
        thread_id=row.thread_id,
        # DB columns are untyped str; the lifecycle is constrained at write time.
        status=cast(RunStatus, row.status),
        model=row.model,
        graph_id=row.graph_id,
        graph_version=row.graph_version,
        content_hash=row.content_hash,
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
        graph_id: str | None = None,
        graph_version: str | None = None,
        content_hash: str | None = None,
    ) -> Run:
        # Graph fields default to None so the /runs path is unchanged; /graphs/runs passes them
        # (the IR's id/version/contentHash — M5 §4).
        run = Run(
            model=model,
            thread_id=thread_id,
            graph_id=graph_id,
            graph_version=graph_version,
            content_hash=content_hash,
        )
        session.add(
            RunRow(
                id=run.id,
                thread_id=thread_id,
                status=run.status,
                model=model,
                params=params or None,
                graph_id=graph_id,
                graph_version=graph_version,
                content_hash=content_hash,
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

    async def list_runs(
        self, session: AsyncSession, *, limit: int, before: str | None = None
    ) -> list[Run]:
        """Recent runs, newest first (M8 §1.1) — the cockpit home page.

        Keyset pagination on ``(created_at, id)`` DESC: ``before`` is a run id cursor; rows
        strictly older than that run are returned. A read-only list over already-persisted
        rows — it adds no contract, it surfaces one (M8 §2). An unknown ``before`` id is
        ignored (treated as no cursor), never an error.
        """
        stmt = select(RunRow).order_by(RunRow.created_at.desc(), RunRow.id.desc()).limit(limit)
        if before is not None:
            anchor = await session.get(RunRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(RunRow.created_at, RunRow.id) < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_run(row) for row in rows]

    async def list_threads(
        self, session: AsyncSession, *, limit: int, before: str | None = None
    ) -> list[ThreadSummary]:
        """Recent threads, newest-activity first (M8 §1.3).

        Each summary carries the message count, last-activity instant, and the first user
        message preview (always ``position == 0`` — turns are appended as user/assistant
        pairs, so the very first user turn is position 0). ``before`` is a thread id cursor
        on ``(created_at, id)`` DESC, mirroring ``list_runs``.
        """
        counts = (
            select(
                MessageRow.thread_id.label("thread_id"),
                func.count().label("message_count"),
                func.max(MessageRow.created_at).label("last_message_at"),
            )
            .group_by(MessageRow.thread_id)
            .subquery()
        )
        first_user = (
            select(MessageRow.thread_id.label("thread_id"), MessageRow.content.label("preview"))
            .where(MessageRow.position == 0)
            .subquery()
        )
        stmt = (
            select(
                ThreadRow.id,
                ThreadRow.created_at,
                ThreadRow.updated_at,
                func.coalesce(counts.c.message_count, 0).label("message_count"),
                func.coalesce(counts.c.last_message_at, ThreadRow.updated_at).label(
                    "last_activity"
                ),
                first_user.c.preview,
            )
            .outerjoin(counts, counts.c.thread_id == ThreadRow.id)
            .outerjoin(first_user, first_user.c.thread_id == ThreadRow.id)
            .order_by(ThreadRow.created_at.desc(), ThreadRow.id.desc())
            .limit(limit)
        )
        if before is not None:
            anchor = await session.get(ThreadRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(ThreadRow.created_at, ThreadRow.id) < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).all()
        return [
            ThreadSummary(
                id=row.id,
                created_at=row.created_at,
                last_activity=row.last_activity,
                message_count=int(row.message_count),
                preview=row.preview,
            )
            for row in rows
        ]

    async def get_thread(self, session: AsyncSession, thread_id: str) -> ThreadDetail | None:
        """A thread and its messages in ``position`` order (M8 §1.3 thread detail)."""
        thread = await session.get(ThreadRow, thread_id)
        if thread is None:
            return None
        rows = (
            await session.execute(
                select(
                    MessageRow.id,
                    MessageRow.run_id,
                    MessageRow.role,
                    MessageRow.content,
                    MessageRow.position,
                    MessageRow.created_at,
                )
                .where(MessageRow.thread_id == thread_id)
                .order_by(MessageRow.position)
            )
        ).all()
        return ThreadDetail(
            id=thread.id,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            messages=[
                ThreadMessage(
                    id=row.id,
                    run_id=row.run_id,
                    role=row.role,
                    content=row.content,
                    position=row.position,
                    created_at=row.created_at,
                )
                for row in rows
            ],
        )

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
