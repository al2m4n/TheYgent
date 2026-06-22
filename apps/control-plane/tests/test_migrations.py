"""Migration chain round-trip (M4 §6) — catch broken/un-reversible migrations early.

``alembic upgrade head`` then ``downgrade base`` must round-trip cleanly. Run against a
dedicated scratch database on the same Postgres server so the shared session schema is
untouched. Sync test on purpose: alembic's async ``env.py`` calls ``asyncio.run`` itself,
so the test must not already be inside an event loop.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import asyncpg
from _db import plain_dsn
from alembic import command
from alembic.config import Config

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
_SCRATCH_DB = "theygent_migration_roundtrip"


def _admin_dsn(url: str) -> str:
    # Connect to the always-present maintenance DB to CREATE/DROP the scratch DB.
    base, _, _ = plain_dsn(url).rpartition("/")
    return f"{base}/postgres"


def _scratch_url(url: str) -> str:
    base, _, _ = url.rpartition("/")
    return f"{base}/{_SCRATCH_DB}"


async def _recreate_scratch(url: str) -> None:
    conn = await asyncpg.connect(dsn=_admin_dsn(url))
    try:
        # DROP/CREATE DATABASE can't run inside a transaction; asyncpg auto-commits each.
        await conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{_SCRATCH_DB}"')
    finally:
        await conn.close()


async def _drop_scratch(url: str) -> None:
    conn = await asyncpg.connect(dsn=_admin_dsn(url))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}" WITH (FORCE)')
    finally:
        await conn.close()


async def _tables(url: str) -> set[str]:
    """The migration's tables that currently exist (excludes alembic's own bookkeeping)."""
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        rows = await conn.fetch(
            "SELECT to_regclass('public.' || t) AS reg "
            "FROM unnest(ARRAY['thread', 'run', 'message', 'mcp_server']) AS t"
        )
        return {r["reg"] for r in rows if r["reg"] is not None}
    finally:
        await conn.close()


def test_migration_round_trip(pg_url: str) -> None:
    scratch = _scratch_url(pg_url)
    asyncio.run(_recreate_scratch(pg_url))
    # env.py reads DATABASE_URL; point it at the scratch DB and restore afterwards so
    # other tests (which read DATABASE_URL for inline apps) keep using the session DB.
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = scratch
        cfg = Config(str(_ALEMBIC_INI))
        # A clean round-trip — and assert each step actually moved the schema, so the test
        # can't pass vacuously on an empty/no-op chain (the round-trip must exercise 0001).
        assert asyncio.run(_tables(scratch)) == set()  # scratch starts bare
        command.upgrade(cfg, "head")
        assert asyncio.run(_tables(scratch)) == {"thread", "run", "message", "mcp_server"}  # built
        command.downgrade(cfg, "base")
        assert asyncio.run(_tables(scratch)) == set()  # torn fully back down
        command.upgrade(cfg, "head")
        assert asyncio.run(_tables(scratch)) == {
            "thread",
            "run",
            "message",
            "mcp_server",
        }  # rebuilt
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop_scratch(pg_url))
