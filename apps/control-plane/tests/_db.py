"""Direct-DB helpers for tests — assert persisted state the API doesn't expose.

There is no /messages endpoint (M4 builds no standalone memory resource — §7), so the
thread-memory tests inspect the ``message`` / ``run`` rows directly over asyncpg. Reads
only; the production path always goes through the app + Alembic-managed schema.
"""

from __future__ import annotations

import asyncpg


def plain_dsn(url: str) -> str:
    """asyncpg wants a driverless DSN (no SQLAlchemy ``+asyncpg`` suffix)."""
    return url.replace("+asyncpg", "")


async def truncate(url: str) -> None:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        await conn.execute("TRUNCATE message, run, thread, mcp_server RESTART IDENTITY CASCADE")
    finally:
        await conn.close()


async def seed_run(url: str, run_id: str, status: str, *, model: str = "triage-fast") -> None:
    """Insert a run row directly in a given lifecycle state (M9 §2.1 reconciliation tests need a
    run stuck at ``streaming`` as if a crash left it there — the app would never create one)."""
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        await conn.execute(
            "INSERT INTO run (id, status, model, created_at, updated_at) "
            "VALUES ($1, $2, $3, now(), now())",
            run_id,
            status,
            model,
        )
    finally:
        await conn.close()


async def get_run_row(url: str, run_id: str) -> dict[str, object] | None:
    """(status, error, output) for a run, read straight from Postgres."""
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        row = await conn.fetchrow("SELECT status, error, output FROM run WHERE id = $1", run_id)
        return dict(row) if row is not None else None
    finally:
        await conn.close()


async def fetch_messages(url: str, thread_id: str) -> list[tuple[str, str, int]]:
    """(role, content, position) for a thread, ordered by the ordering key (position)."""
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        rows = await conn.fetch(
            "SELECT role, content, position FROM message WHERE thread_id = $1 ORDER BY position",
            thread_id,
        )
        return [(r["role"], r["content"], r["position"]) for r in rows]
    finally:
        await conn.close()


async def count_messages(url: str, thread_id: str) -> int:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        return await conn.fetchval("SELECT count(*) FROM message WHERE thread_id = $1", thread_id)
    finally:
        await conn.close()


async def count_all_messages(url: str) -> int:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        return await conn.fetchval("SELECT count(*) FROM message")
    finally:
        await conn.close()
