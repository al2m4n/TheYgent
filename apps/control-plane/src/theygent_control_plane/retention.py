"""Retention: what the control plane stops keeping in its hot tables, on a timer.

Two sweeps, each governed by a setting an admin chooses:

* **AI gateway request rows** (``ai_gateway.request_retention_days``, default 90; 0 keeps every
  row). Rows that started before the cutoff (that many whole UTC days ago) move to
  ``ai_gateway_request_archive``: the records exactly as gateways sent them, gzip-compressed JSON
  lines, downloadable per day. Nothing a report shows is lost. The per-day aggregates hold every
  grouping a report offers, and ``archived_before`` advances to the cutoff *before* any row
  moves, so reports read those days from the aggregates throughout. A record arriving late for an
  archived day is counted there at once, and archived on the next sweep.
* **Agent run I/O** (``node_io``). Captured inputs and outputs older than the agent's
  ``io_retention_seconds`` are deleted, or older than ``telemetry.io_retention_days`` where the
  agent sets none (default 0: keep). These are payloads an admin chose to expire; the run, its
  spans and its timing stay.

Each sweep works in bounded batches, one transaction each. Only one instance sweeps at a time
(``pg_try_advisory_xact_lock``): a replica that finds the lock taken skips that batch.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theygent_control_plane.ai_gateway.store import record_from_row
from theygent_control_plane.models import (
    AiGatewayRequestArchiveRow,
    AiGatewayRequestRow,
    AiGatewayStateRow,
)
from theygent_control_plane.run import new_ulid

logger = logging.getLogger("theygent.control_plane.retention")

REQUEST_RETENTION_KEY = "ai_gateway.request_retention_days"
IO_RETENTION_KEY = "telemetry.io_retention_days"
#: Rows moved or deleted per transaction.
BATCH = 5_000
#: Batches per sweep, so one tick never holds the database for long; the next tick continues.
MAX_BATCHES = 20
DEFAULT_INTERVAL_S = 3_600.0
_LOCK = text("SELECT pg_try_advisory_xact_lock(hashtext('theygent_retention'))")

#: The I/O rows past their retention: the agent's own period, else the platform's (in seconds).
_EXPIRED_IO = text(
    """
    DELETE FROM node_io WHERE id IN (
        SELECT n.id FROM node_io n
        JOIN run r ON r.id = n.run_id
        LEFT JOIN agent_io_policy p ON p.agent_id = r.graph_id
        WHERE COALESCE(NULLIF(GREATEST(p.io_retention_seconds, 0), 0), :default_seconds) > 0
          AND n.created_at < CAST(:now AS timestamptz) - make_interval(
              secs => COALESCE(NULLIF(GREATEST(p.io_retention_seconds, 0), 0), :default_seconds)
          )
        LIMIT :batch
    )
    """
)


@dataclass(frozen=True)
class SweepResult:
    archived_requests: int = 0
    archive_chunks: int = 0
    expired_io: int = 0


def cutoff_day(now: datetime, retention_days: int) -> date | None:
    """The first UTC day kept in detail, ``retention_days`` whole days back; ``None``: keep all."""
    if retention_days <= 0:
        return None
    return now.astimezone(UTC).date() - timedelta(days=retention_days)


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), UTC)


def encode_records(lines: list[str]) -> bytes:
    """A chunk of the archive: JSON lines, gzip-compressed (concatenated chunks stay valid gzip)."""
    return gzip.compress(("\n".join(lines) + "\n").encode(), mtime=0)


async def _locked(session: AsyncSession) -> bool:
    return bool((await session.execute(_LOCK)).scalar_one())


async def advance_archived_before(session: AsyncSession, cutoff: date) -> None:
    """Reports read days before ``cutoff`` from the aggregates from now on. Never moves back."""
    statement = insert(AiGatewayStateRow).values(id=1, key_set_version=1, archived_before=cutoff)
    table = AiGatewayStateRow.__table__
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=["id"],
            set_={
                "archived_before": func.greatest(
                    func.coalesce(table.c.archived_before, cutoff), cutoff
                )
            },
        )
    )


async def archive_batch(
    session: AsyncSession, cutoff: date, *, batch: int | None = None
) -> tuple[int, int]:
    """Move up to ``batch`` request rows that started before ``cutoff`` into the archive, a chunk
    per UTC day. Returns (rows moved, chunks written)."""
    rows = list(
        (
            await session.execute(
                select(AiGatewayRequestRow)
                .where(AiGatewayRequestRow.started_at < _midnight(cutoff))
                .order_by(AiGatewayRequestRow.started_at, AiGatewayRequestRow.id)
                .limit(batch if batch is not None else BATCH)
            )
        ).scalars()
    )
    if not rows:
        return 0, 0
    by_day: dict[date, list[str]] = {}
    for row in rows:
        day = row.started_at.astimezone(UTC).date()
        by_day.setdefault(day, []).append(record_from_row(row).model_dump_json())
    now = datetime.now(UTC)
    await session.execute(
        insert(AiGatewayRequestArchiveRow),
        [
            {
                "id": new_ulid(),
                "day": day,
                "record_count": len(lines),
                "records": encode_records(lines),
                "created_at": now,
            }
            for day, lines in sorted(by_day.items())
        ],
    )
    await session.execute(
        delete(AiGatewayRequestRow).where(AiGatewayRequestRow.id.in_([r.id for r in rows]))
    )
    return len(rows), len(by_day)


class RetentionSweeper:
    """Runs both sweeps every ``interval_s``. ``setting`` reads a setting's current value."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        setting: Callable[[str], Awaitable[Any]],
        *,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessionmaker = sessionmaker
        self._setting = setting
        self._interval_s = interval_s
        self._clock = clock
        self._task: asyncio.Task[None] | None = None

    async def sweep(self) -> SweepResult:
        now = self._clock()
        archived = chunks = expired = 0
        cutoff = cutoff_day(now, int(await self._setting(REQUEST_RETENTION_KEY) or 0))
        if cutoff is not None:
            async with self._sessionmaker() as session, session.begin():
                if not await _locked(session):
                    return SweepResult()
                await advance_archived_before(session, cutoff)
            for _ in range(MAX_BATCHES):
                async with self._sessionmaker() as session, session.begin():
                    if not await _locked(session):
                        break
                    moved, written = await archive_batch(session, cutoff)
                archived, chunks = archived + moved, chunks + written
                if moved < BATCH:
                    break
        default_days = int(await self._setting(IO_RETENTION_KEY) or 0)
        for _ in range(MAX_BATCHES):
            async with self._sessionmaker() as session, session.begin():
                if not await _locked(session):
                    break
                result = await session.execute(
                    _EXPIRED_IO,
                    {"default_seconds": max(default_days, 0) * 86_400, "now": now, "batch": BATCH},
                )
            count = getattr(result, "rowcount", 0) or 0
            expired += count
            if count < BATCH:
                break
        if archived or expired:
            logger.info(
                "retention.swept",
                extra={
                    "archived_requests": archived,
                    "archive_chunks": chunks,
                    "expired_io": expired,
                },
            )
        return SweepResult(archived, chunks, expired)

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return

        async def _loop() -> None:
            while True:
                await asyncio.sleep(self._interval_s)
                try:
                    await self.sweep()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a failed sweep must not end the loop
                    logger.warning("retention.sweep_failed", extra={"error": type(exc).__name__})

        self._task = asyncio.get_running_loop().create_task(_loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
