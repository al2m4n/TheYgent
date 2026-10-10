"""Gateway config versions, heartbeats and the instance list.

What must hold:

* a config is saved as the next version and never changes afterwards; every version stays
  readable, with who saved it and when;
* the version is the ETag: a gateway holding it gets ``304``, and before any config exists the
  answer is ``404`` (the gateway then forwards in passthrough);
* ``If-Match`` refuses a write based on a version that is no longer current; concurrent saves
  take distinct versions;
* a config the gateway would refuse is refused here, without the refusal echoing it;
* gateways and signed-in users read the config; only admins write it;
* each heartbeat updates its instance, and the acknowledgement names the latest config and
  key-set versions; an instance that stopped beating shows offline.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import _auth
import asyncpg
import httpx
import pytest
from _db import plain_dsn
from _fake_inference import FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_ir.gateway import GatewayConfig, GatewayHeartbeatAck

CANARY = "CANARY-31c9-never-echoed"
PASSWORD = "user-pass-1234"
OBSERVE: dict[str, Any] = {"mode": "observer"}


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_user(client: TestClient, username: str, role: str) -> str:
    client.post("/users", json={"username": username, "password": PASSWORD, "role": role})
    return client.post("/auth/login", json={"username": username, "password": PASSWORD}).json()[
        "token"
    ]


@pytest.fixture
def service_key(client: TestClient) -> str:
    return client.post("/gateway/service-keys", json={"name": "gw"}).json()["token"]


def put(client: TestClient, body: Any, **headers: str) -> httpx.Response:
    return client.put("/gateway/config", json=body, headers=headers)


def heartbeat(gateway_id: str = "gw-1", config_version: int | None = None) -> dict[str, Any]:
    return {
        "gateway_id": gateway_id,
        "software_version": "0.1.0",
        "mode": "observer",
        "config_version": config_version,
        "key_set_version": 3,
        "started_at": datetime(2026, 10, 10, 8, tzinfo=UTC).isoformat(),
        "spool_backlog": 12,
        "upstream_reachable": True,
    }


# ── versions ─────────────────────────────────────────────────────────────────


def test_before_any_config_the_answer_is_404(client: TestClient, service_key: str) -> None:
    for headers in ({}, bearer(service_key)):
        missing = client.get("/gateway/config", headers=headers)
        assert (missing.status_code, missing.json()["error"]["code"]) == (
            404,
            "gateway_config_not_found",
        )


def test_a_saved_config_is_the_next_version_and_never_changes(client: TestClient) -> None:
    first = put(client, {**OBSERVE, "policy": {"subagent_target": "claude-sonnet-5-5"}})
    assert first.status_code == 201, first.text
    assert first.headers["etag"] == '"1"'
    saved = GatewayConfig.model_validate_json(first.content)
    assert saved.version == 1 and saved.updated_by is not None
    second = put(client, {"mode": "passthrough"})
    assert GatewayConfig.model_validate_json(second.content).version == 2

    current = client.get("/gateway/config")
    assert current.headers["etag"] == '"2"'
    assert current.json()["mode"] == "passthrough"
    history = client.get("/gateway/config/versions").json()["versions"]
    assert [(v["version"], v["mode"]) for v in history] == [(2, "passthrough"), (1, "observer")]
    assert all(v["created_by"] == saved.updated_by for v in history)
    assert GatewayConfig.model_validate(client.get("/gateway/config/versions/1").json()) == saved
    assert client.get("/gateway/config/versions/9").status_code == 404


@pytest.mark.parametrize(
    ("tag", "status"), [('"1"', 304), ('W/"1"', 304), ('"0", "1"', 304), ('"7"', 200)]
)
def test_the_version_is_the_etag(
    client: TestClient, service_key: str, tag: str, status: int
) -> None:
    put(client, OBSERVE)
    answer = client.get("/gateway/config", headers={**bearer(service_key), "If-None-Match": tag})
    assert answer.status_code == status
    assert answer.headers["etag"] == '"1"'


def test_if_match_refuses_a_stale_write(client: TestClient) -> None:
    assert put(client, OBSERVE, **{"If-Match": '"1"'}).status_code == 412  # nothing to match yet
    assert put(client, OBSERVE).status_code == 201
    assert put(client, OBSERVE, **{"If-Match": '"1"'}).status_code == 201
    stale = put(client, {"mode": "passthrough"}, **{"If-Match": '"1"'})
    assert (stale.status_code, stale.json()["error"]["code"]) == (412, "config_version_conflict")
    assert client.get("/gateway/config").json()["version"] == 2


async def test_concurrent_saves_take_distinct_versions(
    fake_inference: FakeInference, pg_url: str
) -> None:
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            await _auth.attach_async(ac)
            answers = await asyncio.gather(
                *(ac.put("/gateway/config", json=OBSERVE) for _ in range(5))
            )
    assert all(a.status_code == 201 for a in answers)
    assert sorted(a.json()["version"] for a in answers) == [1, 2, 3, 4, 5]


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({**OBSERVE, "note": CANARY}, id="unknown-field"),
        pytest.param({"mode": CANARY}, id="unknown-mode"),
        pytest.param({**OBSERVE, "policy": {"local_target": "llamacpp"}}, id="engine-name"),
        pytest.param(
            {
                **OBSERVE,
                "fingerprints": [
                    {"fingerprint": "0123456789abcdef", "status": "allow_local"},
                    {"fingerprint": "0123456789abcdef", "status": "never_route"},
                ],
            },
            id="duplicate-rule",
        ),
        pytest.param({**OBSERVE, "version": 99}, id="client-chosen-version"),
        pytest.param({**OBSERVE, "pricing_overrides": {f"model {CANARY}": {}}}, id="bad-override"),
    ],
)
def test_a_config_the_gateway_would_refuse_is_refused(client: TestClient, body: Any) -> None:
    refused = put(client, body)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_gateway_config"
    assert CANARY not in refused.text
    assert client.get("/gateway/config").status_code == 404


def test_signed_in_users_and_gateways_read_only_admins_write(
    client: TestClient, service_key: str
) -> None:
    put(client, OBSERVE)
    viewer = make_user(client, "vic", "viewer")
    editor = make_user(client, "eve", "editor")
    gateway_key = client.post("/gateway/keys", json={"name": "laptop"}).json()["token"]
    assert client.get("/gateway/config", headers=bearer(viewer)).status_code == 200
    assert client.get("/gateway/config", headers=bearer(service_key)).status_code == 200
    assert client.get("/gateway/config", headers=bearer(gateway_key)).status_code == 401
    assert client.get("/gateway/config", headers={"Authorization": ""}).status_code == 401
    assert put(client, OBSERVE, **bearer(editor)).status_code == 403
    assert put(client, OBSERVE, **bearer(service_key)).status_code == 401
    assert client.get("/gateway/config/versions", headers=bearer(service_key)).status_code == 401


# ── heartbeats and instances ─────────────────────────────────────────────────


def beat(client: TestClient, service_key: str, body: dict[str, Any]) -> httpx.Response:
    return client.post("/gateway/heartbeat", json=body, headers=bearer(service_key))


def test_a_heartbeat_names_the_latest_versions(
    client: TestClient, service_key: str, pg_url: str
) -> None:
    first = beat(client, service_key, heartbeat())
    assert first.status_code == 200, first.text
    key_set_version = GatewayHeartbeatAck.model_validate_json(first.content).key_set_version
    assert GatewayHeartbeatAck.model_validate_json(first.content).config_version is None
    put(client, OBSERVE)
    ack = GatewayHeartbeatAck.model_validate_json(beat(client, service_key, heartbeat()).content)
    assert ack.config_version == 1
    client.post("/gateway/keys", json={"name": "laptop"})
    ack = GatewayHeartbeatAck.model_validate_json(beat(client, service_key, heartbeat()).content)
    assert key_set_version is not None and (ack.key_set_version or 0) > key_set_version


def test_instances_show_their_latest_heartbeat(
    client: TestClient, service_key: str, pg_url: str
) -> None:
    put(client, OBSERVE)
    beat(client, service_key, heartbeat("gw-1", config_version=1))
    beat(client, service_key, heartbeat("gw-2", config_version=None))
    first_seen = {i["gateway_id"]: i["first_seen_at"] for i in instances(client)}
    beat(client, service_key, {**heartbeat("gw-1", config_version=1), "spool_backlog": 0})

    listed = {i["gateway_id"]: i for i in instances(client)}
    gw1, gw2 = listed["gw-1"], listed["gw-2"]
    assert (gw1["config_current"], gw2["config_current"]) == (True, False)
    assert gw1["spool_backlog"] == 0 and gw1["first_seen_at"] == first_seen["gw-1"]
    assert gw1["online"] and gw1["upstream_reachable"] is True
    assert gw1["service_key_id"] is not None

    sql(
        pg_url,
        "UPDATE ai_gateway_instance SET last_seen_at = now() - interval '10 minutes' "
        "WHERE gateway_id = 'gw-2'",
    )
    assert {i["gateway_id"]: i["online"] for i in instances(client)} == {
        "gw-1": True,
        "gw-2": False,
    }


def instances(client: TestClient) -> list[dict[str, Any]]:
    response = client.get("/gateway/instances")
    assert response.status_code == 200, response.text
    return response.json()["instances"]


def test_only_a_gateway_beats(client: TestClient, service_key: str) -> None:
    session = client.headers["Authorization"].removeprefix("Bearer ")
    assert beat(client, session, heartbeat()).status_code == 401
    invalid = beat(client, service_key, {**heartbeat(), "gateway_id": f"gw {CANARY}"})
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (422, "invalid_heartbeat")
    assert CANARY not in invalid.text
    assert instances(client) == []


def sql(pg_url: str, query: str) -> None:
    async def run() -> None:
        conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
        try:
            await conn.execute(query)
        finally:
            await conn.close()

    asyncio.run(run())
