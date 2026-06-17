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
        await conn.execute("TRUNCATE message, run, thread RESTART IDENTITY CASCADE")
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
