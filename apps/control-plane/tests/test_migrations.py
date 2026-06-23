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
        names = "'thread', 'run', 'message', 'mcp_server', 'agent', 'agent_version', 'trigger'"
        rows = await conn.fetch(
            f"SELECT to_regclass('public.' || t) AS reg FROM unnest(ARRAY[{names}]) AS t"
        )
        return {r["reg"] for r in rows if r["reg"] is not None}
    finally:
        await conn.close()


async def _run_columns(url: str) -> set[str]:
    """The columns on the ``run`` table — to assert the M12-added columns (``trigger_id``,
    ``completed_at``) round-trip at the COLUMN level, not just the table level (a column add/drop is
    invisible to ``_tables``, so the round-trip could silently regress without this)."""
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'run'"
        )
        return {r["column_name"] for r in rows}
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
        assert asyncio.run(_tables(scratch)) == {
            "thread",
            "run",
            "message",
            "mcp_server",
            "agent",
            "agent_version",
            "trigger",
        }  # built
        # Column-level proof for the M12-added run columns (a column add/drop is invisible to the
        # table-set check above, so the round-trip could silently regress without this).
        assert {"trigger_id", "completed_at"} <= asyncio.run(_run_columns(scratch))
        command.downgrade(cfg, "base")
        assert asyncio.run(_tables(scratch)) == set()  # torn fully back down
        command.upgrade(cfg, "head")
        assert asyncio.run(_tables(scratch)) == {
            "thread",
            "run",
            "message",
            "mcp_server",
            "agent",
            "agent_version",
            "trigger",
        }  # rebuilt
        assert {"trigger_id", "completed_at"} <= asyncio.run(_run_columns(scratch))  # cols rebuilt
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop_scratch(pg_url))


def test_trigger_migration_single_step_down_up(pg_url: str) -> None:
    # Exercise 0006's OWN downgrade in isolation (downgrade -1 from head), not only as a step inside
    # `downgrade base`: the `trigger` table and both new `run` columns must drop cleanly and re-add.
    # This is the M12 §5 "migration chain round-trips incl. trigger + run.trigger_id" check, made
    # explicit at the column level so a regression in 0006 alone is caught.
    scratch = _scratch_url(pg_url)
    asyncio.run(_recreate_scratch(pg_url))
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = scratch
        cfg = Config(str(_ALEMBIC_INI))
        command.upgrade(cfg, "head")
        assert "trigger" in asyncio.run(_tables(scratch))
        assert {"trigger_id", "completed_at"} <= asyncio.run(_run_columns(scratch))
        # One step back: 0006 down. trigger table gone; the two run columns gone; run itself stays.
        command.downgrade(cfg, "-1")
        assert "trigger" not in asyncio.run(_tables(scratch))
        assert "run" in asyncio.run(_tables(scratch))
        cols = asyncio.run(_run_columns(scratch))
        assert "trigger_id" not in cols and "completed_at" not in cols
        # And forward again: 0006 up re-adds them (idempotent up/down on the single revision).
        command.upgrade(cfg, "head")
        assert "trigger" in asyncio.run(_tables(scratch))
        assert {"trigger_id", "completed_at"} <= asyncio.run(_run_columns(scratch))
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop_scratch(pg_url))
