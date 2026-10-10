"""The fingerprint catalog: observed auxiliary kinds, their rules, and known-kind suggestions.

What must hold:

* every observed kind is listed with its stats, ``unreviewed`` until a rule says otherwise;
* the Phase 0 kinds are recognised by shape and *suggested* a status, never given one;
* setting a status saves a new config version that changes that rule and nothing else (mode,
  policy, other rules, prices kept); concurrent changes lose nothing;
* rules for kinds not seen yet are listed too;
* only admins change rules, and a refused rule never echoes what was sent.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import _auth
import httpx
import pytest
from _fake_inference import FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app

T0 = datetime(2026, 10, 6, 9, tzinfo=UTC)
PASSWORD = "user-pass-1234"
CANARY = "CANARY-77ab-never-echoed"
TITLE = "1111111111111111"
QUOTA = "2222222222222222"
SIDE = "3333333333333333"
OTHER = "4444444444444444"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def aux(n: int, fingerprint: str, shape: dict[str, Any], shares: bool | None) -> dict[str, Any]:
    return {
        "id": f"rec_{n:05d}",
        "gateway_id": "gw-test",
        "started_at": (T0 + timedelta(minutes=n)).isoformat(),
        "endpoint": "messages",
        "status": 200,
        "outcome": "complete",
        "credential_kind": "oauth",
        "credential_profile": "passthrough",
        "client": "cli",
        "client_version": "2.1.285",
        "request_class": "auxiliary",
        "fingerprint": fingerprint,
        "shape": shape,
        "shares_main_prefix": shares,
        "decision": {"action": "stay", "reasons": ["fingerprint_unreviewed"]},
    }


PHASE_0 = [
    aux(1, QUOTA, {"max_tokens": 1, "message_count": 1}, False),
    aux(2, SIDE, {"tool_count": 14, "deferred_tool_count": 1, "thinking": "adaptive"}, True),
    aux(3, TITLE, {"has_output_schema": True, "max_tokens": 32000}, False),
    aux(4, OTHER, {"tool_count": 3}, False),
    aux(5, TITLE, {"has_output_schema": True, "max_tokens": 32000}, False),
]


def seed(client: TestClient, records: list[dict[str, Any]]) -> None:
    key = client.post("/gateway/service-keys", json={"name": "gw"}).json()["token"]
    answer = client.post(
        "/gateway/events",
        json={"gateway_id": "gw-test", "records": records},
        headers=bearer(key),
    )
    assert answer.status_code == 200, answer.text


def catalog(client: TestClient, token: str | None = None) -> dict[str, Any]:
    response = client.get("/gateway/fingerprints", headers=bearer(token) if token else {})
    assert response.status_code == 200, response.text
    return response.json()


def by_fingerprint(listed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["fingerprint"]: e for e in listed["fingerprints"]}


def review(
    client: TestClient, fingerprint: str, status: str, label: str | None = None, **headers: str
) -> httpx.Response:
    body: dict[str, Any] = {"status": status, **({"label": label} if label else {})}
    return client.put(f"/gateway/fingerprints/{fingerprint}", json=body, headers=headers)


# ── listing ──────────────────────────────────────────────────────────────────


def test_observed_kinds_are_listed_unreviewed_with_their_stats(client: TestClient) -> None:
    seed(client, PHASE_0)
    listed = catalog(client)
    assert listed["config_version"] is None
    assert [e["fingerprint"] for e in listed["fingerprints"]] == [TITLE, OTHER, SIDE, QUOTA]
    title = by_fingerprint(listed)[TITLE]
    assert title["status"] == "unreviewed" and title["label"] is None
    assert title["request_count"] == 2
    assert title["client_versions"] == ["2.1.285"]
    assert (title["first_seen"], title["last_seen"]) == (
        (T0 + timedelta(minutes=3)).isoformat().replace("+00:00", "Z"),
        (T0 + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    )


def test_the_phase_0_kinds_are_recognised_and_only_suggested(client: TestClient) -> None:
    seed(client, PHASE_0)
    entries = by_fingerprint(catalog(client))
    suggestions = {
        fp: (e["known_kind"], e["suggested_status"], e["status"]) for fp, e in entries.items()
    }
    assert suggestions == {
        QUOTA: ("quota_check", "never_route", "unreviewed"),
        SIDE: ("main_prefix_side_request", "never_route", "unreviewed"),
        TITLE: ("session_title", "allow_local", "unreviewed"),
        OTHER: (None, None, "unreviewed"),
    }


# ── reviewing ────────────────────────────────────────────────────────────────


def test_a_review_saves_a_config_version_changing_only_that_rule(client: TestClient) -> None:
    seed(client, PHASE_0)
    first = review(client, TITLE, "allow_local", "session-title")
    assert first.status_code == 200, first.text
    assert first.json()["config_version"] == 1 and first.headers["etag"] == '"1"'
    assert first.json()["fingerprint"]["status"] == "allow_local"
    # No config existed: the new one keeps the unconfigured behaviour, passthrough.
    assert client.get("/gateway/config").json()["mode"] == "passthrough"

    base = {
        "mode": "observer",
        "policy": {"local_target": "aux-local"},
        "pricing_overrides": {
            "claude-opus-5-5": {
                "input": 3.6,
                "output": 18,
                "cache_write_5m": 4.5,
                "cache_write_1h": 7.2,
                "cache_read": 0.18,
            }
        },
        "fingerprints": [{"fingerprint": TITLE, "status": "allow_local", "label": "session-title"}],
    }
    assert client.put("/gateway/config", json=base).status_code == 201  # version 2
    assert review(client, QUOTA, "never_route").json()["config_version"] == 3

    current = client.get("/gateway/config").json()
    assert current["mode"] == "observer"
    assert current["policy"]["local_target"] == "aux-local"
    assert current["pricing_overrides"]["claude-opus-5-5"]["output"] == 18
    assert [(r["fingerprint"], r["status"]) for r in current["fingerprints"]] == [
        (TITLE, "allow_local"),
        (QUOTA, "never_route"),
    ]
    assert client.get("/gateway/config/versions/2").json()["fingerprints"] == base["fingerprints"]
    entries = by_fingerprint(catalog(client))
    assert (entries[QUOTA]["status"], entries[TITLE]["label"]) == ("never_route", "session-title")


def test_unreviewing_removes_the_rule_unless_it_keeps_a_label(client: TestClient) -> None:
    seed(client, PHASE_0)
    review(client, TITLE, "allow_local")
    review(client, TITLE, "unreviewed")
    assert client.get("/gateway/config").json()["fingerprints"] == []
    review(client, OTHER, "unreviewed", "classifier")
    assert client.get("/gateway/config").json()["fingerprints"] == [
        {"fingerprint": OTHER, "status": "unreviewed", "label": "classifier"}
    ]


def test_a_rule_can_come_before_its_kind_is_seen(client: TestClient) -> None:
    unseen = "abcdefabcdefabcd"
    answer = review(client, unseen, "never_route", "resume-summary")
    assert answer.json()["fingerprint"] == {
        "fingerprint": unseen,
        "status": "never_route",
        "label": "resume-summary",
        "first_seen": None,
        "last_seen": None,
        "request_count": 0,
        "client_versions": [],
        "shape": None,
        "shares_main_prefix": None,
        "known_kind": None,
        "suggested_status": None,
    }
    assert [e["fingerprint"] for e in catalog(client)["fingerprints"]] == [unseen]


def test_if_match_refuses_a_review_based_on_an_old_version(client: TestClient) -> None:
    review(client, TITLE, "allow_local")
    stale = review(client, QUOTA, "never_route", **{"If-Match": '"0"'})
    assert (stale.status_code, stale.json()["error"]["code"]) == (412, "config_version_conflict")
    assert review(client, QUOTA, "never_route", **{"If-Match": '"1"'}).status_code == 200


async def test_concurrent_reviews_lose_nothing(fake_inference: FakeInference, pg_url: str) -> None:
    fingerprints = [f"{n:016x}" for n in range(1, 9)]
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            await _auth.attach_async(ac)
            answers = await asyncio.gather(
                *(
                    ac.put(f"/gateway/fingerprints/{fp}", json={"status": "never_route"})
                    for fp in fingerprints
                )
            )
            assert all(a.status_code == 200 for a in answers)
            final = (await ac.get("/gateway/config")).json()
    assert sorted(a.json()["config_version"] for a in answers) == list(range(1, 9))
    assert sorted(r["fingerprint"] for r in final["fingerprints"]) == fingerprints


@pytest.mark.parametrize(
    ("fingerprint", "body"),
    [
        pytest.param("not-a-fingerprint", {"status": "never_route"}, id="fingerprint"),
        pytest.param(TITLE, {"status": "approved"}, id="status"),
        pytest.param(TITLE, {"status": "never_route", "label": f"free text {CANARY}"}, id="label"),
        pytest.param(TITLE, {"status": "never_route", CANARY: 1}, id="unknown-field"),
    ],
)
def test_a_bad_rule_is_refused_without_echo(
    client: TestClient, fingerprint: str, body: dict[str, Any]
) -> None:
    refused = client.put(f"/gateway/fingerprints/{fingerprint}", json=body)
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "invalid_fingerprint_rule"
    assert CANARY not in refused.text
    assert client.get("/gateway/config").status_code == 404  # nothing saved


def test_viewers_read_the_catalog_and_only_admins_review(client: TestClient) -> None:
    seed(client, PHASE_0)
    for username, role in (("vic", "viewer"), ("eve", "editor")):
        client.post("/users", json={"username": username, "password": PASSWORD, "role": role})
    viewer = client.post("/auth/login", json={"username": "vic", "password": PASSWORD}).json()
    editor = client.post("/auth/login", json={"username": "eve", "password": PASSWORD}).json()
    assert len(catalog(client, viewer["token"])["fingerprints"]) == 4
    assert review(client, TITLE, "allow_local", **bearer(editor["token"])).status_code == 403
    gateway_key = client.post("/gateway/keys", json={"name": "laptop"}).json()["token"]
    assert client.get("/gateway/fingerprints", headers=bearer(gateway_key)).status_code == 401
