"""Migration chain round-trip — catch broken/un-reversible migrations early.

``alembic upgrade head`` then ``downgrade base`` must round-trip cleanly. Run against a
dedicated scratch database on the same Postgres server so the shared session schema is
untouched. Sync test on purpose: alembic's async ``env.py`` calls ``asyncio.run`` itself,
so the test must not already be inside an event loop.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import asyncpg
import pytest
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
            "'ai_gateway_state', 'ai_gateway_request_archive'"
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
            "ai_gateway_request_archive",
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
            "ai_gateway_request_archive",
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

    watched = AI_GATEWAY_TABLES | {"api_key", "ai_gateway_request_archive"}
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


def _retention_records() -> list[Any]:
    """Request records across two days, two sessions, three reasons and a shadow run."""
    from theygent_ir.gateway import GatewayRequestRecord

    day = datetime(2026, 10, 9, 10, tzinfo=UTC)
    route = {
        "action": "route",
        "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
        "reasons": ["subagent_at_spawn"],
        "counterfactual_usd": 0.001,
    }
    shadow = {
        "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
        "outcome": "answered",
        "usage": {"output_tokens": 40},
        "cost_usd": 0.0005,
    }

    def rec(n: int, when: datetime, **fields: Any) -> Any:
        return GatewayRequestRecord.model_validate(
            {
                "id": f"rec_{n:05d}",
                "gateway_id": "gw",
                "started_at": when,
                "endpoint": "messages",
                "status": 200,
                "outcome": "complete",
                "credential_kind": "api_key",
                "credential_profile": "passthrough",
                "request_class": "main",
                "requested_model": "claude-opus-5-5",
                "served_model": "claude-opus-5-5",
                "usage": {"input_tokens": 10 * n, "output_tokens": 50},
                "cost_usd": 0.01 * n,
                "decision": {"action": "stay", "reasons": ["mid_conversation"]},
                **fields,
            }
        )

    return [
        rec(1, day, session_id="s-1"),
        rec(2, day + timedelta(minutes=5), session_id="s-1"),
        rec(
            3,
            day + timedelta(minutes=6),
            session_id="s-1",
            request_class="subagent",
            decision=route,
            shadow=shadow,
        ),
        rec(4, day + timedelta(hours=2), session_id="s-2", outcome="upstream_broke"),
        rec(
            5,
            day + timedelta(days=1),
            session_id="s-2",
            decision={"action": "stay", "reasons": ["cold_cache_boundary"]},
        ),
        rec(6, day + timedelta(days=1, minutes=1), cost_usd=None),
    ]


async def _insert_requests(url: str, records: list[Any]) -> None:
    import json

    from theygent_control_plane.ai_gateway.store import _request_row

    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        for record in records:
            row = _request_row(record, service_key_id=None, received_at=record.started_at)
            columns = list(row)
            values = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in row.values()]
            placeholders = ", ".join(f"${i + 1}" for i in range(len(columns)))
            await conn.execute(
                f"INSERT INTO ai_gateway_request ({', '.join(columns)}) VALUES ({placeholders})",
                *values,
            )
    finally:
        await conn.close()


async def _daily_rows(url: str) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        return [dict(r) for r in await conn.fetch("SELECT * FROM ai_gateway_daily_usage")]
    finally:
        await conn.close()


def test_retention_migration_rebuilds_the_daily_aggregates_exactly(pg_url: str) -> None:
    # 0025 widens the daily key (session, reason) and rebuilds every row from the request rows; the
    # rebuild is SQL frozen in the migration, checked here against GatewayTotals.of.
    from theygent_control_plane.ai_gateway.store import DAILY_KEY_COLUMNS, daily_key
    from theygent_ir.gateway import GatewayTotals

    records = _retention_records()
    scratch = _scratch_url(pg_url)
    asyncio.run(_recreate_scratch(pg_url))
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = scratch
        cfg = Config(str(_ALEMBIC_INI))
        command.upgrade(cfg, "0024_ai_gateway_shadows")
        asyncio.run(_insert_requests(scratch, records))
        command.upgrade(cfg, "0025_ai_gateway_retention")
        stored = asyncio.run(_daily_rows(scratch))
        # The key and the fields this revision has; later ones add their own columns.
        columns = [c for c in DAILY_KEY_COLUMNS if c in stored[0]]

        def key_at(record: Any) -> tuple[Any, ...]:
            return tuple(
                v for c, v in zip(DAILY_KEY_COLUMNS, daily_key(record), strict=True) if c in columns
            )

        expected: dict[tuple[Any, ...], GatewayTotals] = {}
        for record in records:
            key = key_at(record)
            expected[key] = expected.get(key, GatewayTotals()) + GatewayTotals.of(record)
        assert len(stored) == len(expected) == 5
        for row in stored:
            key = tuple(row[c] for c in columns)
            present = [f for f in GatewayTotals.model_fields if f in row]
            want = expected[key].model_dump()
            assert {f: row[f] for f in present} == pytest.approx({f: want[f] for f in present})
            starts = [r.started_at for r in records if key_at(r) == key]
            assert (row["first_at"], row["last_at"]) == (min(starts), max(starts))
        # Down a step: the narrower key, the same sums.
        command.downgrade(cfg, "-1")
        narrow = asyncio.run(_daily_rows(scratch))
        assert "session_id" not in narrow[0] and len(narrow) == 3
        assert sum(r["calls"] for r in narrow) == len(records)
        assert sum(r["cost_usd"] for r in narrow) == pytest.approx(0.15)
        command.upgrade(cfg, "head")
        assert len(asyncio.run(_daily_rows(scratch))) == 5
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop_scratch(pg_url))


async def _archive_first_day(url: str, first: Any) -> None:
    """What the sweeper leaves of a day it archived: its daily rows, no request rows."""
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        await conn.execute(
            "DELETE FROM ai_gateway_request WHERE (started_at AT TIME ZONE 'UTC')::date = $1",
            first,
        )
        await conn.execute(
            "INSERT INTO ai_gateway_state (id, key_set_version, archived_before) "
            "VALUES (1, 1, $1) ON CONFLICT (id) DO UPDATE SET archived_before = $1",
            first + timedelta(days=1),
        )
    finally:
        await conn.close()


def test_shadow_model_migration_keys_the_daily_aggregates_by_it(pg_url: str) -> None:
    # 0027 adds the model a shadow ran on to the daily key: the days kept in detail are rebuilt
    # exactly from the request rows (a local trial's model is not its decision's target); an
    # archived day's rows with shadows take their decision's target, where every shadow ran then.
    from theygent_control_plane.ai_gateway.store import DAILY_KEY_COLUMNS, daily_key
    from theygent_ir.gateway import GatewayRequestRecord, GatewayTotals

    trial = GatewayRequestRecord.model_validate(
        {
            **_retention_records()[4].model_dump(mode="json"),
            "id": "rec_00007",
            "request_class": "subagent",
            "served_model": "claude-sonnet-5-5",
            "decision": {
                "action": "route",
                "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
                "reasons": ["subagent_at_spawn"],
                "applied": True,
            },
            "shadow": {
                "target": {"kind": "local", "model": "coder-medium"},
                "outcome": "answered",
                "agreed": True,
                "cost_usd": 0.0,
                "trial": True,
            },
        }
    )
    records = [*_retention_records(), trial]
    first = records[0].started_at.date()
    kept = [r for r in records if r.started_at.date() > first]
    archived = [r for r in records if r.started_at.date() == first]
    scratch = _scratch_url(pg_url)
    asyncio.run(_recreate_scratch(pg_url))
    previous = os.environ.get("DATABASE_URL")
    try:
        os.environ["DATABASE_URL"] = scratch
        cfg = Config(str(_ALEMBIC_INI))
        command.upgrade(cfg, "0024_ai_gateway_shadows")
        asyncio.run(_insert_requests(scratch, records[:-1]))
        command.upgrade(cfg, "0025_ai_gateway_retention")  # builds the daily rows
        command.upgrade(cfg, "0026_ai_gateway_shadow_agreement")
        asyncio.run(_insert_requests(scratch, [trial]))  # a request row only, as at 0026 a
        asyncio.run(_archive_first_day(scratch, first))  # trial could not yet be recorded
        command.upgrade(cfg, "0027_ai_gateway_shadow_model")
        expected: dict[tuple[Any, ...], GatewayTotals] = {}
        for record in kept:
            key = daily_key(record)
            expected[key] = expected.get(key, GatewayTotals()) + GatewayTotals.of(record)
        stored = {
            tuple(row[c] for c in DAILY_KEY_COLUMNS): row
            for row in asyncio.run(_daily_rows(scratch))
        }
        for key, totals in expected.items():
            row = stored[key]
            want = totals.model_dump()
            assert {f: row[f] for f in want} == pytest.approx(want)
        assert stored[daily_key(trial)]["shadow_agreed_calls"] == 1
        assert daily_key(trial)[-1] == "coder-medium"
        old = {k: r for k, r in stored.items() if k[0] == first}
        assert sum(r["calls"] for r in old.values()) == len(archived)
        shadowed = [k for k, r in old.items() if r["shadow_calls"]]
        assert [k[-1] for k in shadowed] == ["claude-sonnet-5-5"]
        assert len(stored) == len(expected) + len(old)
        # Down a step: the narrower key, the same sums.
        command.downgrade(cfg, "-1")
        narrow = asyncio.run(_daily_rows(scratch))
        assert "shadow_model" not in narrow[0]
        assert sum(r["calls"] for r in narrow) == len(records)
        command.upgrade(cfg, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
        asyncio.run(_drop_scratch(pg_url))
