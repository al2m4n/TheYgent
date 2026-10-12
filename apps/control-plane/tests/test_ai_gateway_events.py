"""``POST /gateway/events``: where AI gateways deliver their records.

What must hold:

* only a gateway's own service key opens it — no session, no personal key, no developer's
  gateway key, no key whose owner is disabled or no longer an admin;
* a re-sent batch is a no-op, and two deliveries of one batch racing each other count once;
* the per-day aggregates are ``GatewayTotals`` summed over the records that were new, so they
  never double-count and agree with the gateway's own report;
* auxiliary request kinds feed the fingerprint catalog's stats;
* anything that is not a valid batch (free text included) is refused with 422 without the
  refusal echoing what was sent, and an oversized body is a 413 the gateway answers by sending
  smaller batches.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import _auth
import asyncpg
import httpx
import pytest
import theygent_control_plane.app as app_module
from _auth import ADMIN_USERNAME
from _db import plain_dsn
from _fake_inference import FakeInference
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from theygent_control_plane.ai_gateway.store import DAILY_KEY_COLUMNS, daily_key
from theygent_control_plane.app import create_app
from theygent_control_plane.auth import ApiKeyKind, AuthStore, Role
from theygent_ir.gateway import GatewayRequestRecord, GatewayTotals

T0 = datetime(2026, 10, 10, 9, tzinfo=UTC)
CANARY = "CANARY-5d1e-never-echoed"


def rec(n: int, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": f"rec_{n:05d}",
        "gateway_id": "gw-test",
        "config_version": 3,
        "started_at": (T0 + timedelta(minutes=n)).isoformat(),
        "endpoint": "messages",
        "status": 200,
        "outcome": "complete",
        "developer_id": "usr_dev",
        "credential_kind": "oauth",
        "credential_profile": "passthrough",
        "client": "cli",
        "client_version": "2.1.285",
        "session_id": "sess-1",
        "request_class": "main",
        "requested_model": "claude-opus-5-5",
        "served_model": "claude-opus-5-5",
        "usage": {"input_tokens": 2, "output_tokens": 100, "cache_read_tokens": 1000},
        "cost_usd": 0.0022,
        "pricing_version": "2026-09-25",
        "decision": {"action": "stay", "reasons": ["mid_conversation"]},
    }
    return {**base, **fields}


def batch(*records: dict[str, Any], gateway_id: str = "gw-test") -> dict[str, Any]:
    return {"gateway_id": gateway_id, "records": list(records)}


async def _admin_id(pg_url: str) -> str:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        return await conn.fetchval(
            "SELECT id FROM user_account WHERE username = $1", ADMIN_USERNAME
        )
    finally:
        await conn.close()


async def _mint(pg_url: str, user_id: str, kind: ApiKeyKind, role: Role = "admin") -> str:
    """Mint through the real auth store, as the key-management endpoints will."""
    engine = create_async_engine(pg_url)
    try:
        async with async_sessionmaker(engine)() as session, session.begin():
            token, _ = await AuthStore().create_api_key(
                session, user_id=user_id, name="gateway", role=role, kind=kind
            )
        return token
    finally:
        await engine.dispose()


async def _rows(pg_url: str, query: str, *args: Any) -> list[dict[str, Any]]:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        return [dict(r) for r in await conn.fetch(query, *args)]
    finally:
        await conn.close()


def rows(pg_url: str, query: str, *args: Any) -> list[dict[str, Any]]:
    return asyncio.run(_rows(pg_url, query, *args))


@pytest.fixture
def service_key(client: TestClient, pg_url: str) -> str:
    return asyncio.run(_mint(pg_url, asyncio.run(_admin_id(pg_url)), "gateway_service"))


def post(client: TestClient, token: str | None, body: Any) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}" if token else ""}
    content = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return client.post(
        "/gateway/events",
        content=content,
        headers={**headers, "content-type": "application/json"},
    )


# ── storing records ──────────────────────────────────────────────────────────


def test_a_batch_is_stored_and_acknowledged(
    client: TestClient, pg_url: str, service_key: str
) -> None:
    route = {
        "action": "route",
        "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
        "reasons": ["subagent_at_spawn"],
        "counterfactual_usd": 0.001,
    }
    response = post(
        client,
        service_key,
        batch(
            rec(1),
            rec(
                2,
                request_class="subagent",
                agent_id="agent-1",
                decision=route,
                seat_allowance={"five_hour_utilization": 0.4},
                shape={"tool_count": 9, "deferred_tool_count": 1},
            ),
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"accepted": 2, "duplicates": 0}
    stored = rows(pg_url, "SELECT * FROM ai_gateway_request ORDER BY id")
    assert [r["id"] for r in stored] == ["rec_00001", "rec_00002"]
    routed = stored[1]
    assert routed["decision_action"] == "route"
    assert (routed["decision_target_kind"], routed["decision_target_model"]) == (
        "anthropic",
        "claude-sonnet-5-5",
    )
    assert json.loads(routed["decision_reasons"]) == ["subagent_at_spawn"]
    assert routed["counterfactual_usd"] == 0.001 and routed["decision_applied"] is False
    assert (routed["input_tokens"], routed["output_tokens"], routed["cache_read_tokens"]) == (
        2,
        100,
        1000,
    )
    assert json.loads(routed["seat_allowance"])["five_hour_utilization"] == 0.4
    assert json.loads(routed["shape"])["tool_count"] == 9
    assert stored[0]["shape"] is None and stored[0]["seat_allowance"] is None  # SQL NULL
    assert routed["service_key_id"] is not None and routed["received_at"] is not None
    assert routed["config_version"] == 3


def test_a_resent_batch_is_a_no_op(client: TestClient, pg_url: str, service_key: str) -> None:
    body = batch(rec(1), rec(2))
    assert post(client, service_key, body).json() == {"accepted": 2, "duplicates": 0}
    before = rows(pg_url, "SELECT * FROM ai_gateway_daily_usage")
    assert post(client, service_key, body).json() == {"accepted": 0, "duplicates": 2}
    assert len(rows(pg_url, "SELECT id FROM ai_gateway_request")) == 2
    after = rows(pg_url, "SELECT * FROM ai_gateway_daily_usage")
    assert [r["calls"] for r in after] == [r["calls"] for r in before] == [2]


def test_a_partly_new_batch_adds_only_the_new_records(
    client: TestClient, pg_url: str, service_key: str
) -> None:
    post(client, service_key, batch(rec(1), rec(2)))
    assert post(client, service_key, batch(rec(2), rec(3), rec(3))).json() == {
        "accepted": 1,
        "duplicates": 2,
    }
    [daily] = rows(pg_url, "SELECT calls, output_tokens FROM ai_gateway_daily_usage")
    assert daily == {"calls": 3, "output_tokens": 300}


def test_daily_usage_is_the_shared_totals(
    client: TestClient, pg_url: str, service_key: str
) -> None:
    local = {
        "action": "route",
        "target": {"kind": "local", "model": "aux-local"},
        "reasons": ["local_candidate"],
        "counterfactual_usd": 0.0,
    }
    records = [
        rec(1),
        rec(2, status=429, usage={}, cost_usd=None),
        rec(3, request_class="auxiliary", decision=local, cost_usd=0.0007),
        rec(4, served_model="claude-imaginary-9", cost_usd=None),
        rec(5, developer_id=None, client=None),
        rec(60 * 24, outcome="upstream_broke"),  # the next day
        # Router mode: an applied route, served by the target, with the requested model's cost.
        rec(
            6,
            request_class="subagent",
            served_model="claude-sonnet-5-5",
            cost_usd=0.001,
            baseline_usd=0.0024,
            decision={
                "action": "route",
                "target": {"kind": "anthropic", "model": "claude-sonnet-5-5"},
                "reasons": ["subagent_at_spawn"],
                "applied": True,
            },
        ),
    ]
    post(client, service_key, batch(*records))
    expected: dict[tuple[Any, ...], GatewayTotals] = {}
    for raw in records:
        record = GatewayRequestRecord.model_validate(raw)
        key = daily_key(record)
        expected[key] = expected.get(key, GatewayTotals()) + GatewayTotals.of(record)
    stored = rows(pg_url, "SELECT * FROM ai_gateway_daily_usage")
    assert len(stored) == len(expected)
    for row in stored:
        key = tuple(row[column] for column in DAILY_KEY_COLUMNS)
        totals = GatewayTotals.model_validate({f: row[f] for f in GatewayTotals.model_fields})
        assert totals.model_dump() == pytest.approx(expected[key].model_dump()), key
        # Each row spans exactly the requests it sums.
        starts = [
            GatewayRequestRecord.model_validate(raw).started_at
            for raw in records
            if daily_key(GatewayRequestRecord.model_validate(raw)) == key
        ]
        assert (row["first_at"], row["last_at"]) == (min(starts), max(starts)), key
    days = {row["day"] for row in stored}
    assert days == {date(2026, 10, 10), date(2026, 10, 11)}
    assert {row["decision_target"] for row in stored} == {"", "aux-local", "claude-sonnet-5-5"}
    [applied] = [row for row in stored if row["decision_target"] == "claude-sonnet-5-5"]
    assert (applied["applied_calls"], applied["baseline_usd"]) == (1, pytest.approx(0.0024))
    [stored_record] = rows(
        pg_url, "SELECT baseline_usd FROM ai_gateway_request WHERE id = $1", "rec_00006"
    )
    assert stored_record["baseline_usd"] == pytest.approx(0.0024)


def test_auxiliary_kinds_feed_the_catalog(
    client: TestClient, pg_url: str, service_key: str
) -> None:
    fingerprint = "0123456789abcdef"
    shape = {"tool_count": 0, "has_output_schema": True}
    aux = {"request_class": "auxiliary", "fingerprint": fingerprint, "shares_main_prefix": False}
    post(
        client,
        service_key,
        batch(
            rec(1, **aux, shape=shape, client_version="2.1.285"),
            rec(2, **aux, client_version="2.1.290"),
            rec(3, fingerprint="fedcba9876543210"),  # main traffic is not catalogued
        ),
    )
    post(client, service_key, batch(rec(9, **aux, client_version="2.1.285")))
    [entry] = rows(pg_url, "SELECT * FROM ai_gateway_fingerprint")
    assert entry["fingerprint"] == fingerprint
    assert entry["request_count"] == 3
    assert json.loads(entry["client_versions"]) == ["2.1.285", "2.1.290"]
    assert (entry["first_seen_at"], entry["last_seen_at"]) == (
        T0 + timedelta(minutes=1),
        T0 + timedelta(minutes=9),
    )
    # The newest sighting carried no shape, so the last known one is kept.
    assert json.loads(entry["shape"])["has_output_schema"] is True
    assert entry["shares_main_prefix"] is False


async def test_concurrent_deliveries_of_one_batch_count_once(
    fake_inference: FakeInference, pg_url: str
) -> None:
    body = batch(*(rec(n) for n in range(20)))
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            await _auth.attach_async(ac)
            token = await _mint(pg_url, await _admin_id(pg_url), "gateway_service")
            headers = {"Authorization": f"Bearer {token}"}
            answers = await asyncio.gather(
                *(ac.post("/gateway/events", json=body, headers=headers) for _ in range(5))
            )
    assert all(a.status_code == 200 for a in answers)
    assert sum(a.json()["accepted"] for a in answers) == 20
    assert sum(a.json()["duplicates"] for a in answers) == 80
    [daily] = await _rows(pg_url, "SELECT calls FROM ai_gateway_daily_usage")
    assert daily["calls"] == 20


# ── who may deliver ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["personal", "gateway"])
def test_other_key_kinds_are_refused(client: TestClient, pg_url: str, kind: ApiKeyKind) -> None:
    token = asyncio.run(_mint(pg_url, asyncio.run(_admin_id(pg_url)), kind))
    refused = post(client, token, batch(rec(1)))
    assert refused.status_code == 401
    assert refused.json()["error"]["code"] == "unauthorized"


def test_the_stored_kind_decides_not_the_prefix(
    client: TestClient, pg_url: str, service_key: str
) -> None:
    assert post(client, service_key, batch(rec(1))).status_code == 200
    asyncio.run(_rows(pg_url, "UPDATE api_key SET kind = 'personal' WHERE kind <> 'personal'"))
    assert post(client, service_key, batch(rec(2))).status_code == 401


def test_no_key_a_session_or_a_forged_key_is_refused(client: TestClient) -> None:
    session_bearer = client.headers["Authorization"].removeprefix("Bearer ")
    for token in (None, session_bearer, "tygs_forged"):
        assert post(client, token, batch(rec(1))).status_code == 401


def test_a_revoked_key_or_a_lapsed_owner_is_refused(client: TestClient, pg_url: str) -> None:
    owner = client.post(
        "/users", json={"username": "ops", "password": "ops-pass-1234", "role": "admin"}
    ).json()["id"]
    token = asyncio.run(_mint(pg_url, owner, "gateway_service"))
    assert post(client, token, batch(rec(1))).status_code == 200

    assert client.patch(f"/users/{owner}", json={"role": "editor"}).status_code == 200
    assert post(client, token, batch(rec(2))).status_code == 401  # no longer an admin's key
    assert client.patch(f"/users/{owner}", json={"role": "admin"}).status_code == 200
    assert post(client, token, batch(rec(3))).status_code == 200

    assert client.patch(f"/users/{owner}", json={"disabled": True}).status_code == 200
    assert post(client, token, batch(rec(4))).status_code == 401
    assert client.patch(f"/users/{owner}", json={"disabled": False}).status_code == 200

    asyncio.run(_rows(pg_url, "UPDATE api_key SET revoked_at = now() WHERE user_id = $1", owner))
    assert post(client, token, batch(rec(5))).status_code == 401


# ── what may be delivered ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "body",
    [
        pytest.param(batch(rec(1, prompt_text=CANARY)), id="unknown-field"),
        pytest.param(batch(rec(1, **{f"Field {CANARY}": 1})), id="unknown-field-named-in-text"),
        pytest.param(batch(rec(1, client=f"cli {CANARY} free text")), id="free-text"),
        pytest.param(batch(rec(1, usage={"input_tokens": 1, "text": CANARY})), id="nested"),
        pytest.param(batch(*(rec(n) for n in range(501))), id="too-many-records"),
        pytest.param(batch(), id="no-records"),
        pytest.param(batch(rec(1, gateway_id="gw-other")), id="another-gateway"),
        pytest.param(f'{{"gateway_id": "{CANARY}", "records": [', id="not-json"),
    ],
)
def test_anything_but_a_valid_batch_is_refused_without_echo(
    client: TestClient, pg_url: str, service_key: str, body: Any
) -> None:
    refused = post(client, service_key, body)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_event_batch"
    assert CANARY not in refused.text
    assert rows(pg_url, "SELECT id FROM ai_gateway_request") == []


def test_an_oversized_body_is_refused_so_the_gateway_splits_it(
    client: TestClient, service_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app_module, "_GATEWAY_EVENTS_MAX_BYTES", 2_000)
    assert post(client, service_key, batch(rec(1))).status_code == 200
    oversized = json.dumps(batch(*(rec(n) for n in range(2, 12)))).encode()
    too_big = post(client, service_key, oversized)
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "event_batch_too_large"
    # Streamed, the body declares no length: the size is checked once it has arrived.
    streamed = client.post(
        "/gateway/events",
        content=iter([oversized[:1000], oversized[1000:]]),
        headers={"Authorization": f"Bearer {service_key}", "content-type": "application/json"},
    )
    assert streamed.status_code == 413
