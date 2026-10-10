"""Migration chain round-trip — catch broken/un-reversible migrations early.

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
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from theygent_control_plane.models import Base

_ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
_SCRATCH_DB = "theygent_migration_roundtrip"
AI_GATEWAY_TABLES = {
    "ai_gateway_request",
    "ai_gateway_daily_usage",
    "ai_gateway_instance",
    "ai_gateway_config",
    "ai_gateway_fingerprint",
    "ai_gateway_developer",
    "ai_gateway_state",
}


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
        names = (
            "'chat_session', 'run', 'message', 'mcp_server', 'agent', 'agent_version', "
            "'agent_draft', 'trigger', 'span', 'node_io', 'agent_io_policy', "
            "'bench_suite', 'bench_case', 'bench_run', 'bench_preset', "
            "'secret', 'connection', 'gate_counter', 'platform_setting', "
            "'ai_gateway_request', 'ai_gateway_daily_usage', 'ai_gateway_instance', "
            "'ai_gateway_config', 'ai_gateway_fingerprint', 'ai_gateway_developer', "
            "'ai_gateway_state'"
        )
        rows = await conn.fetch(
            f"SELECT to_regclass('public.' || t) AS reg FROM unnest(ARRAY[{names}]) AS t"
        )
        return {r["reg"] for r in rows if r["reg"] is not None}
    finally:
        await conn.close()


async def _run_columns(url: str) -> set[str]:
    """The columns on the ``run`` table — to assert the trigger-related columns (``trigger_id``,
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


async def _draft_columns(url: str) -> set[str]:
    """The columns on ``agent_draft`` — the draft columns asserted at the COLUMN level like the
    ``run`` columns above (a table-set check alone can't see a missing/extra column)."""
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'agent_draft'"
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
            "chat_session",
            "run",
            "message",
            "mcp_server",
            "agent",
            "agent_version",
            "agent_draft",
            "trigger",
            "span",
            "node_io",
            "agent_io_policy",
            "bench_suite",
            "bench_case",
            "bench_run",
            "bench_preset",
            "secret",
            "connection",
            "gate_counter",
            "platform_setting",
            *AI_GATEWAY_TABLES,
        }  # built
        # Column-level proof for the trigger-related run columns (a column add/drop is
        # invisible to the table-set check above, so the round-trip could silently regress
        # without this). ``session_id`` proves the thread→session column rename actually ran
        # (the table-set check alone would miss a half-applied rename).
        assert {"trigger_id", "completed_at", "awaiting_node", "session_id"} <= asyncio.run(
            _run_columns(scratch)
        )
        assert "thread_id" not in asyncio.run(_run_columns(scratch))
        # Same column-level proof for the draft table (the exact set — an extra or missing
        # column in the migration is caught here, not by the table-set check).
        assert asyncio.run(_draft_columns(scratch)) == {
            "id",
            "agent_id",
            "owner_id",
            "name",
            "node_count",
            "ir",
            "view",
            "created_at",
            "updated_at",
        }
        command.downgrade(cfg, "base")
        assert asyncio.run(_tables(scratch)) == set()  # torn fully back down
        command.upgrade(cfg, "head")
        assert asyncio.run(_tables(scratch)) == {
            "chat_session",
            "run",
            "message",
            "mcp_server",
            "agent",
            "agent_version",
            "agent_draft",
            "trigger",
            "span",
            "node_io",
            "agent_io_policy",
            "bench_suite",
            "bench_case",
            "bench_run",
            "bench_preset",
            "secret",
            "connection",
            "gate_counter",
            "platform_setting",
            *AI_GATEWAY_TABLES,
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
    # `downgrade base`: the `trigger` table and both new `run` columns must drop cleanly and re-add,
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
        # Peel off any migrations stacked above 0006 (0007 adds run.awaiting_node) so the `-1`
        # step below still isolates 0006 itself — the single-revision round-trip stays exact.
        command.downgrade(cfg, "0006_trigger")
        assert "awaiting_node" not in asyncio.run(_run_columns(scratch))
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


async def _api_keys(url: str) -> list[dict[str, object]]:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        columns = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'api_key'"
            )
        }
        kind = "kind" if "kind" in columns else "NULL AS kind"
        return [dict(r) for r in await conn.fetch(f"SELECT id, {kind} FROM api_key ORDER BY id")]
    finally:
        await conn.close()


async def _seed_key(url: str) -> None:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        await conn.execute(
            "INSERT INTO user_account (id, username, display_name, role, disabled, created_at, "
            "updated_at) VALUES ('usr_1', 'ada', 'Ada', 'admin', false, now(), now())"
        )
        await conn.execute(
            "INSERT INTO api_key (id, user_id, name, role, token_hash, token_prefix, created_at) "
            "VALUES ('key_1', 'usr_1', 'ci', 'admin', 'abc', 'tyk_abc', now())"
        )
    finally:
        await conn.close()


async def _key_set_version(url: str) -> int | None:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        return await conn.fetchval("SELECT key_set_version FROM ai_gateway_state WHERE id = 1")
    finally:
        await conn.close()


def test_ai_gateway_migration_single_step_down_up(pg_url: str) -> None:
    # 0020 on its own: existing API keys become personal (they keep opening the management API),
    # the key-set version starts at 1, and the step reverses without touching the keys.
    scratch = _scratch_url(pg_url)
    asyncio.run(_recreate_scratch(pg_url))
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = scratch
        cfg = Config(str(_ALEMBIC_INI))
        command.upgrade(cfg, "0019_user_avatar")
        asyncio.run(_seed_key(scratch))
        command.upgrade(cfg, "0020_ai_gateway")
        assert asyncio.run(_api_keys(scratch)) == [{"id": "key_1", "kind": "personal"}]
        assert AI_GATEWAY_TABLES <= asyncio.run(_tables(scratch))
        assert asyncio.run(_key_set_version(scratch)) == 1
        command.downgrade(cfg, "-1")
        assert asyncio.run(_tables(scratch)).isdisjoint(AI_GATEWAY_TABLES)
        assert asyncio.run(_api_keys(scratch)) == [{"id": "key_1", "kind": None}]
        command.upgrade(cfg, "0020_ai_gateway")
        assert asyncio.run(_api_keys(scratch)) == [{"id": "key_1", "kind": "personal"}]
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop_scratch(pg_url))


def test_the_ai_gateway_models_match_the_migrated_schema(pg_url: str) -> None:
    # Models and migrations are kept in step by hand; this catches a column, type, nullability or
    # index that one has and the other lacks, for the tables the AI gateway added or changed.
    def drift(sync_conn: Connection) -> list[object]:
        context = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return list(compare_metadata(context, Base.metadata))

    async def compare() -> list[object]:
        engine = create_async_engine(pg_url)
        try:
            async with engine.connect() as conn:
                return await conn.run_sync(drift)
        finally:
            await engine.dispose()

    watched = AI_GATEWAY_TABLES | {"api_key"}
    relevant = [d for d in asyncio.run(compare()) if _diff_tables(d) & watched]
    assert relevant == []
    # Autogenerate does not compare primary keys; the daily aggregates' composite key matters.
    keys = asyncio.run(_primary_keys(pg_url, watched))
    for table in watched:
        modelled = [c.name for c in Base.metadata.tables[table].primary_key.columns]
        assert keys[table] == modelled, table


async def _primary_keys(url: str, tables: set[str]) -> dict[str, list[str]]:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        rows = await conn.fetch(
            "SELECT tc.table_name, kcu.column_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_name = tc.constraint_name AND kcu.table_schema = tc.table_schema "
            "WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public' "
            "AND tc.table_name = ANY($1::text[]) ORDER BY tc.table_name, kcu.ordinal_position",
            sorted(tables),
        )
    finally:
        await conn.close()
    keys: dict[str, list[str]] = {table: [] for table in tables}
    for row in rows:
        keys[row["table_name"]].append(row["column_name"])
    return keys


def _diff_tables(diff: object) -> set[str]:
    """The table names an autogenerate diff entry touches (entries are tuples, or lists of
    tuples for column modifications)."""
    if isinstance(diff, list):
        return set().union(*(_diff_tables(d) for d in diff))
    names: set[str] = set()
    for part in diff if isinstance(diff, tuple) else ():
        if isinstance(part, str) and part in _ALL_TABLES:
            names.add(part)
        table = getattr(part, "table", None) or (part if hasattr(part, "columns") else None)
        name = getattr(table, "name", None)
        if isinstance(name, str):
            names.add(name)
    return names


_ALL_TABLES = set(Base.metadata.tables)


_KEY_SET_TRIGGERS = {
    "ai_gateway_key_inserted",
    "ai_gateway_key_deleted",
    "ai_gateway_key_updated",
    "ai_gateway_owner_enabled_changed",
    "ai_gateway_profile_changed",
}


async def _key_set_machinery(url: str) -> tuple[set[str], bool]:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        triggers = {
            r["tgname"]
            for r in await conn.fetch("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        }
        function = await conn.fetchval(
            "SELECT count(*) FROM pg_proc WHERE proname = 'ai_gateway_key_set_changed'"
        )
        return triggers & _KEY_SET_TRIGGERS, bool(function)
    finally:
        await conn.close()


def test_key_set_trigger_migration_single_step_down_up(pg_url: str) -> None:
    scratch = _scratch_url(pg_url)
    asyncio.run(_recreate_scratch(pg_url))
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = scratch
        cfg = Config(str(_ALEMBIC_INI))
        command.upgrade(cfg, "0021_ai_gateway_key_set")
        assert asyncio.run(_key_set_machinery(scratch)) == (_KEY_SET_TRIGGERS, True)
        command.downgrade(cfg, "0020_ai_gateway")
        assert asyncio.run(_key_set_machinery(scratch)) == (set(), False)
        assert AI_GATEWAY_TABLES <= asyncio.run(_tables(scratch))  # 0020's tables stay
        command.upgrade(cfg, "head")
        assert asyncio.run(_key_set_machinery(scratch)) == (_KEY_SET_TRIGGERS, True)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop_scratch(pg_url))


async def _catalog_columns(url: str) -> set[str]:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'ai_gateway_fingerprint'"
        )
        return {r["column_name"] for r in rows}
    finally:
        await conn.close()


def test_catalog_stats_migration_single_step_down_up(pg_url: str) -> None:
    # 0022 moves review status out of the catalog (the config holds it); a downgrade restores
    # the columns, with every kind unreviewed.
    review = {"status", "label", "reviewed_at", "reviewed_by"}
    scratch = _scratch_url(pg_url)
    asyncio.run(_recreate_scratch(pg_url))
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = scratch
        cfg = Config(str(_ALEMBIC_INI))
        command.upgrade(cfg, "0022_ai_gateway_catalog_stats")
        assert asyncio.run(_catalog_columns(scratch)).isdisjoint(review)
        command.downgrade(cfg, "0021_ai_gateway_key_set")
        assert review <= asyncio.run(_catalog_columns(scratch))
        command.upgrade(cfg, "head")
        assert asyncio.run(_catalog_columns(scratch)).isdisjoint(review)
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop_scratch(pg_url))
