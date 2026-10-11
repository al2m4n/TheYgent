"""Retention: old AI gateway request rows move to a compressed archive without changing a single
report figure, and run I/O past its retention is deleted.

What must hold:

* every report — totals, every grouping, the session list and each session's totals — reads the
  same before and after its days are archived;
* the archive holds each record exactly as the gateway sent it, downloadable per day;
* reports switch to the daily aggregates before any row moves, and a late record for an archived
  day counts at once and is archived on the next sweep;
* retention 0 keeps everything, and one instance sweeps at a time;
* run I/O older than its agent's retention (or the platform's, where the agent sets none) goes; the
  rest stays.
"""

from __future__ import annotations

import asyncio
import gzip
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
import pytest
from _db import plain_dsn
from _ir import trivial_ir
from fastapi.testclient import TestClient
from test_ai_gateway_events import rows
from test_ai_gateway_reports import bearer, make_user, route, seed
from theygent_control_plane import retention
from theygent_control_plane.retention import (
    IO_RETENTION_KEY,
    REQUEST_RETENTION_KEY,
    RetentionSweeper,
    SweepResult,
)
from theygent_control_plane.run import new_ulid
from theygent_ir.gateway import GatewayRequestRecord

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)
SONNET = {"kind": "anthropic", "model": "claude-sonnet-5-5"}
LOCAL = {"kind": "local", "model": "aux-local"}
WINDOW = {"since": "2026-09-20T00:00:00Z", "until": "2026-10-12T00:00:00Z"}
DIMENSIONS = "class,client,session,model,served-model,developer,decision,reason,day"


def record(n: int, when: datetime, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": f"rec_{n:05d}",
        "gateway_id": "gw-test",
        "started_at": when.isoformat(),
        "endpoint": "messages",
        "status": 200,
        "outcome": "complete",
        "credential_kind": "api_key",
        "credential_profile": "passthrough",
        "client": "cli",
        "request_class": "main",
        "requested_model": "claude-opus-5-5",
        "served_model": "claude-opus-5-5",
        "usage": {"input_tokens": 3 * n, "output_tokens": 40 + n, "cache_read_tokens": 900},
        "cost_usd": round(0.001 * n, 6),
        "pricing_version": "2026-10-11",
        "decision": {"action": "stay", "reasons": ["mid_conversation"]},
    }
    return {**base, **fields}


def history(ada: str, bob: str) -> list[dict[str, Any]]:
    """Twenty days of traffic: sessions spanning days, routes, a local candidate, an applied
    route, a shadow, errors, a request without a session."""
    out: list[dict[str, Any]] = []
    start = datetime(2026, 9, 21, 9, tzinfo=UTC)
    for day in range(20):
        when = start + timedelta(days=day)
        who = ada if day % 2 else bob
        session = f"s-{who[-4:]}-{day // 3}"  # sessions span up to three days
        n = 10 * day
        out += [
            record(n + 1, when, developer_id=who, session_id=session),
            record(
                n + 2,
                when + timedelta(minutes=3),
                developer_id=who,
                session_id=session,
                request_class="subagent",
                decision=route(SONNET, 0.0006, "subagent_at_spawn"),
                shadow={
                    "target": SONNET,
                    "outcome": "answered",
                    "usage": {"output_tokens": 30},
                    "cost_usd": 0.0002,
                }
                if day % 3 == 0
                else None,
            ),
            record(
                n + 3,
                when + timedelta(minutes=5),
                developer_id=who,
                session_id=session,
                request_class="auxiliary",
                client="sdk-cli",
                decision=route(LOCAL, 0.0, "local_candidate"),
            ),
            record(
                n + 4,
                when + timedelta(minutes=7),
                developer_id=who,
                session_id=session,
                served_model="claude-sonnet-5-5",
                cost_usd=0.0009,
                baseline_usd=0.0021,
                decision={
                    "action": "route",
                    "target": SONNET,
                    "reasons": ["cold_cache_boundary"],
                    "applied": True,
                },
            ),
            record(
                n + 5,
                when + timedelta(minutes=9),
                status=429,
                usage={},
                cost_usd=None,
                developer_id=who,
                session_id=session,
            ),
            record(n + 6, when + timedelta(hours=3), developer_id=None, client=None),
        ]
    return [
        {k: v for k, v in r.items() if v is not None or k in ("developer_id", "client")}
        for r in out
    ]


def sweeper(client: TestClient, *, days: int, io_days: int = 0, now: datetime = NOW) -> Any:
    values = {REQUEST_RETENTION_KEY: days, IO_RETENTION_KEY: io_days}

    async def setting(key: str) -> Any:
        return values[key]

    return RetentionSweeper(client.app.state.sessionmaker, setting, clock=lambda: now)


def sweep(client: TestClient, **kwargs: Any) -> SweepResult:
    return client.portal.call(sweeper(client, **kwargs).sweep)


def everything(client: TestClient) -> dict[str, Any]:
    """Every figure the reports show for the window: totals, all groupings, the sessions."""
    report = client.get("/gateway/report", params={**WINDOW, "by": DIMENSIONS}).json()
    sessions = client.get("/gateway/sessions", params={**WINDOW, "limit": "200"}).json()["sessions"]
    details = {
        s["session_id"]: client.get(f"/gateway/sessions/{s['session_id']}").json()["totals"]
        for s in sessions
    }
    return {
        "total": report["total"],
        "first": report["first"],
        "last": report["last"],
        "served_differs": report["served_differs"],
        "groups": {d: sorted(rows, key=lambda r: r["key"]) for d, rows in report["groups"].items()},
        "sessions": sorted(
            (
                {k: v for k, v in s.items() if k != "totals"}
                | {"calls": s["totals"]["calls"], "cost": s["totals"]["cost_usd"]}
                for s in sessions
            ),
            key=lambda s: s["session_id"],
        ),
        "details": details,
    }


def approx(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: approx(v) for k, v in value.items()}
    if isinstance(value, list):
        return [approx(v) for v in value]
    return pytest.approx(value) if isinstance(value, float) else value


def test_archiving_changes_no_figure(client: TestClient, pg_url: str) -> None:
    ada, _ = make_user(client, "ada", "editor")
    bob, _ = make_user(client, "bob", "editor")
    seed(client, history(ada, bob))
    before = everything(client)
    assert before["total"]["calls"] == 120
    # Keep ten days in detail: the eleven days before 2026-10-01 move to the archive.
    result = sweep(client, days=10)
    assert result.archived_requests == 6 * 10 and result.archive_chunks == 10
    after = everything(client)
    assert after == approx(before)
    # The rows are gone from the hot table, and the archive holds them.
    [kept] = rows(pg_url, "SELECT count(*) AS n FROM ai_gateway_request")
    assert kept["n"] == 60
    [state] = rows(pg_url, "SELECT archived_before FROM ai_gateway_state")
    assert state["archived_before"] == date(2026, 10, 1)


def test_reports_stay_exact_in_the_middle_of_a_sweep(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Reports switch to the aggregates before the first row moves, so a sweep cut short — here,
    # seven rows a tick — never shows a figure that is neither before nor after.
    ada, _ = make_user(client, "ada", "editor")
    bob, _ = make_user(client, "bob", "editor")
    seed(client, history(ada, bob))
    before = everything(client)
    monkeypatch.setattr(retention, "BATCH", 7)
    monkeypatch.setattr(retention, "MAX_BATCHES", 1)
    moved = 0
    while (step := sweep(client, days=10).archived_requests) > 0:
        moved += step
        assert everything(client) == approx(before)
    assert moved == 60


def test_the_archive_holds_each_record_as_sent(client: TestClient) -> None:
    ada, _ = make_user(client, "ada", "editor")
    bob, _ = make_user(client, "bob", "editor")
    records = history(ada, bob)
    seed(client, records)
    sweep(client, days=10)
    listing = client.get("/gateway/archive").json()
    assert listing["archived_before"] == "2026-10-01"
    days = {d["day"]: d for d in listing["days"]}
    assert set(days) == {(date(2026, 9, 21) + timedelta(days=i)).isoformat() for i in range(10)}
    assert all(d["records"] == 6 and d["chunks"] == 1 and d["bytes"] > 0 for d in days.values())
    response = client.get("/gateway/archive/2026-09-22")
    assert response.headers["content-type"] == "application/gzip"
    assert "ai-gateway-requests-2026-09-22.jsonl.gz" in response.headers["content-disposition"]
    lines = gzip.decompress(response.content).decode().splitlines()
    archived = [GatewayRequestRecord.model_validate_json(line) for line in lines]
    originals = {
        r["id"]: GatewayRequestRecord.model_validate(r)
        for r in records
        if r["started_at"].startswith("2026-09-22")
    }
    assert {r.id: r for r in archived} == originals
    assert client.get("/gateway/archive/2026-10-05").status_code == 404
    assert client.get("/gateway/archive/yesterday").status_code == 400


def test_only_admins_read_the_archive(client: TestClient) -> None:
    _, token = make_user(client, "ada", "editor")
    assert client.get("/gateway/archive", headers=bearer(token)).status_code == 403
    assert client.get("/gateway/archive/2026-09-22", headers=bearer(token)).status_code == 403


def test_a_late_record_for_an_archived_day_counts_at_once(client: TestClient) -> None:
    ada, _ = make_user(client, "ada", "editor")
    bob, _ = make_user(client, "bob", "editor")
    seed(client, history(ada, bob))
    sweep(client, days=10)
    total = everything(client)["total"]
    # An offline gateway delivers a record for 2026-09-25 after that day was archived.
    late = record(
        9_999, datetime(2026, 9, 25, 15, tzinfo=UTC), developer_id=ada, session_id="s-late"
    )
    seed(client, [late])
    now = everything(client)
    assert now["total"]["calls"] == total["calls"] + 1
    assert now["total"]["cost_usd"] == pytest.approx(total["cost_usd"] + late["cost_usd"])
    assert "s-late" in now["details"]
    result = sweep(client, days=10)
    assert result.archived_requests == 1
    assert everything(client) == approx(now)


def test_a_partly_archived_session_keeps_its_totals(client: TestClient) -> None:
    ada, _ = make_user(client, "ada", "editor")
    bob, _ = make_user(client, "bob", "editor")
    seed(client, history(ada, bob))
    # A session spanning 2026-09-30 (archived) and 2026-10-01..02 (kept).
    sessions = client.get("/gateway/sessions", params={**WINDOW, "limit": "200"}).json()["sessions"]
    spanning = next(
        s
        for s in sessions
        if s["first_at"].startswith("2026-09-30") and s["last_at"].startswith("2026-10-0")
    )
    session = spanning["session_id"]
    before = client.get(f"/gateway/sessions/{session}").json()
    sweep(client, days=10)
    after = client.get(f"/gateway/sessions/{session}").json()
    assert after["totals"] == approx(before["totals"])
    archived = [r for r in before["requests"] if r["started_at"].startswith("2026-09-30")]
    assert after["archived_requests"] == len(archived) > 0
    assert [r["id"] for r in after["requests"]] == [
        r["id"] for r in before["requests"] if r not in archived
    ]


def test_retention_zero_keeps_every_row(client: TestClient, pg_url: str) -> None:
    ada, _ = make_user(client, "ada", "editor")
    bob, _ = make_user(client, "bob", "editor")
    seed(client, history(ada, bob))
    assert sweep(client, days=0) == SweepResult()
    [kept] = rows(pg_url, "SELECT count(*) AS n FROM ai_gateway_request")
    assert kept["n"] == 120
    assert client.get("/gateway/archive").json() == {"archived_before": None, "days": []}


def test_one_instance_sweeps_at_a_time(client: TestClient, pg_url: str) -> None:
    ada, _ = make_user(client, "ada", "editor")
    bob, _ = make_user(client, "bob", "editor")
    seed(client, history(ada, bob))

    async def held() -> SweepResult:
        conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
        try:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext('theygent_retention'))")
                return await sweeper(client, days=10).sweep()
        finally:
            await conn.close()

    assert client.portal.call(held) == SweepResult()
    assert sweep(client, days=10).archived_requests == 60


# ── run I/O ──────────────────────────────────────────────────────────────────


def _agent_with_io(client: TestClient, retention_seconds: int | None) -> tuple[str, str]:
    """A saved agent with full I/O capture (and its own retention, if given), run once."""
    saved = client.post("/agents", json={"ir": {**trivial_ir(), "id": f"agt_{new_ulid()}"}})
    assert saved.status_code == 201, saved.text
    agent_id = saved.json()["id"]
    policy: dict[str, Any] = {"io_capture": "full"}
    if retention_seconds is not None:
        policy["io_retention_seconds"] = retention_seconds
    assert client.put(f"/agents/{agent_id}/io-policy", json=policy).status_code == 200
    run = client.post(f"/agents/{agent_id}/runs", json={"input": "hi", "stream": False})
    assert run.status_code == 200, run.text
    return agent_id, run.json()["runId"]


def _age_io(pg_url: str, run_id: str, age: timedelta) -> None:
    async def go() -> None:
        conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
        try:
            await conn.execute(
                "UPDATE node_io SET created_at = $1 WHERE run_id = $2", NOW - age, run_id
            )
        finally:
            await conn.close()

    asyncio.run(go())


def _io_runs(pg_url: str) -> dict[str, int]:
    found = rows(pg_url, "SELECT run_id, count(*) AS n FROM node_io GROUP BY run_id")
    return {r["run_id"]: r["n"] for r in found}


def test_run_io_past_its_agents_retention_is_deleted(client: TestClient, pg_url: str) -> None:
    _, short_old = _agent_with_io(client, 3600)
    _, short_new = _agent_with_io(client, 3600)
    _, keep_old = _agent_with_io(client, None)
    _age_io(pg_url, short_old, timedelta(hours=2))
    _age_io(pg_url, short_new, timedelta(minutes=10))
    _age_io(pg_url, keep_old, timedelta(days=30))
    before = _io_runs(pg_url)
    assert before[short_old] > 0
    result = sweep(client, days=0, io_days=0)
    # Only I/O older than its own agent's hour goes; no platform retention keeps the rest.
    assert result.expired_io == before[short_old]
    assert _io_runs(pg_url) == {short_new: before[short_new], keep_old: before[keep_old]}
    # The run itself, and its spans, stay.
    assert client.get(f"/runs/{short_old}/trace").json()["spans"]


def test_the_platform_retention_covers_agents_without_their_own(
    client: TestClient, pg_url: str
) -> None:
    _, own_new = _agent_with_io(client, 30 * 86_400)
    _, plain_old = _agent_with_io(client, None)
    _, plain_new = _agent_with_io(client, None)
    _age_io(pg_url, own_new, timedelta(days=5))  # the agent's own 30 days win
    _age_io(pg_url, plain_old, timedelta(days=5))
    _age_io(pg_url, plain_new, timedelta(hours=1))
    sweep(client, days=0, io_days=2)
    assert set(_io_runs(pg_url)) == {own_new, plain_new}


@pytest.mark.parametrize("seconds", [0, -5])
def test_an_agents_own_retention_must_be_positive(client: TestClient, seconds: int) -> None:
    saved = client.post("/agents", json={"ir": trivial_ir()})
    answer = client.put(
        f"/agents/{saved.json()['id']}/io-policy",
        json={"io_capture": "full", "io_retention_seconds": seconds},
    )
    assert answer.status_code == 422
