"""Fast suite for the trigger / deploy layer — invoke, schedule, webhook over a real
(fake-model) inference plane and a REAL ephemeral Postgres applied through REAL Alembic migrations.
No SQLite, no create_all.

The trigger layer gives a saved agent stable, non-interactive entry points so it runs unattended —
the minimum viable "deploy". So these prove: a token-authed invoke runs the agent and a missing/bad
token is 401; a schedule fires the PINNED version through the dispatcher tick and a disabled one
does not; schedules persist + rehydrate across a restart; a hash pin runs that content after a newer
version publishes; a webhook fires on a valid signature and rejects a bad one; runs carry trigger
lineage (NULL for interactive); and a fired run whose inference plane is unreachable fails honestly
(no hang). The dispatcher loop is OFF in these tests (start_dispatcher=False) so scheduled fires are
driven deterministically via ``dispatcher.tick``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from _fake_inference import FULL_MESSAGE, FakeInference
from _ir import trivial_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_ir import content_hash, parse_document

TOKEN = "deploy-token-abc123"
_AUTH = {"Authorization": f"Bearer {TOKEN}"}
# Far enough past any cron window that a just-created "* * * * *" schedule is due on the next tick.
_DUE = datetime.now(UTC) + timedelta(hours=1)


def _agent_ir(version: str = "0.1.0", prompt: str = "$in", agent_id: str | None = None) -> dict:
    doc = trivial_ir()
    doc["version"] = version
    doc["nodes"][1]["config"]["messages"][0]["content"] = prompt
    if agent_id is not None:
        doc["id"] = agent_id
    return doc


@pytest.fixture
def authed_client(fake_inference: FakeInference, pg_url: str) -> Iterator[TestClient]:
    """A control-plane with a configured invoke token, schedule loop OFF (driven manually)."""
    app = create_app(
        inference_base_url=fake_inference.v1_url,
        database_url=pg_url,
        invoke_token=TOKEN,
        start_dispatcher=False,
    )
    with TestClient(app) as client:
        yield client


def _save_agent(client: TestClient, ir: dict | None = None) -> str:
    ir = ir or _agent_ir()
    resp = client.post("/agents", json={"ir": ir})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── HTTP trigger: token-authed invoke ────────────────────────────────────────────


def test_invoke_with_token_runs_agent(authed_client: TestClient) -> None:
    agent_id = _save_agent(authed_client)
    resp = authed_client.post(
        f"/agents/{agent_id}/invoke", json={"input": "hi", "stream": False}, headers=_AUTH
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["output"] == FULL_MESSAGE
    # The awaited result carries a run handle correlatable via GET /runs/{id}.
    got = authed_client.get(f"/runs/{body['runId']}").json()
    assert got["status"] == "completed"
    # An invoke is unattended but NOT trigger-fired — trigger_id stays NULL.
    assert got["trigger_id"] is None


def test_invoke_missing_token_401(authed_client: TestClient) -> None:
    agent_id = _save_agent(authed_client)
    resp = authed_client.post(f"/agents/{agent_id}/invoke", json={"input": "hi", "stream": False})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_invoke_bad_token_401(authed_client: TestClient) -> None:
    agent_id = _save_agent(authed_client)
    resp = authed_client.post(
        f"/agents/{agent_id}/invoke",
        json={"input": "hi", "stream": False},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


def test_invoke_unknown_agent_404_after_auth(authed_client: TestClient) -> None:
    # Auth passes (valid token) but the agent is unknown → a clean 404, never a 500.
    resp = authed_client.post(
        "/agents/nope/invoke", json={"input": "x", "stream": False}, headers=_AUTH
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_not_found"


def test_invoke_deny_by_default_when_no_token_configured(
    fake_inference: FakeInference, pg_url: str
) -> None:
    # An unattended invoke surface with NO token configured is CLOSED (deny-by-default): even a
    # request bearing some token is 401, because no token can match an unset one.
    app = create_app(
        inference_base_url=fake_inference.v1_url, database_url=pg_url, start_dispatcher=False
    )
    with TestClient(app) as client:
        agent_id = _save_agent(client)
        resp = client.post(
            f"/agents/{agent_id}/invoke", json={"input": "hi", "stream": False}, headers=_AUTH
        )
        assert resp.status_code == 401


# ── Trigger CRUD + validation ─────────────────────────────────────────────────────


def test_create_trigger_requires_exactly_one_pin(authed_client: TestClient) -> None:
    agent_id = _save_agent(authed_client)
    # No pin → 400 (an unattended deploy must pin an immutable artifact).
    no_pin = authed_client.post(
        "/triggers",
        json={"agent_id": agent_id, "kind": "schedule", "config": {"cron": "* * * * *"}},
    )
    assert no_pin.status_code == 400
    assert no_pin.json()["error"]["code"] == "invalid_trigger"
    # Both pins → 400 (ambiguous which immutable artifact).
    both = authed_client.post(
        "/triggers",
        json={
            "agent_id": agent_id,
            "kind": "schedule",
            "version": "0.1.0",
            "content_hash": "sha256:abc",
            "config": {"cron": "* * * * *"},
        },
    )
    assert both.status_code == 400


def test_create_schedule_requires_valid_cron(authed_client: TestClient) -> None:
    agent_id = _save_agent(authed_client)
    bad = authed_client.post(
        "/triggers",
        json={
            "agent_id": agent_id,
            "kind": "schedule",
            "version": "0.1.0",
            "config": {"cron": "nope"},
        },
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_trigger"


def test_create_webhook_requires_secret(authed_client: TestClient) -> None:
    agent_id = _save_agent(authed_client)
    bad = authed_client.post(
        "/triggers",
        json={"agent_id": agent_id, "kind": "webhook", "version": "0.1.0", "config": {}},
    )
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_trigger"


def test_create_trigger_dangling_pin_404(authed_client: TestClient) -> None:
    # Registering a deploy of a nonexistent version is a clean 404 at create time — never a
    # surprise at fire time; a dangling pin must fail honestly at registration, not at fire time.
    agent_id = _save_agent(authed_client)
    resp = authed_client.post(
        "/triggers",
        json={
            "agent_id": agent_id,
            "kind": "schedule",
            "version": "9.9.9",
            "config": {"cron": "* * * * *"},
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_version_not_found"


def test_create_trigger_unknown_agent_404(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/triggers",
        json={
            "agent_id": "nope",
            "kind": "schedule",
            "version": "0.1.0",
            "config": {"cron": "* * * * *"},
        },
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_not_found"


def test_webhook_secret_redacted_everywhere(authed_client: TestClient) -> None:
    # A webhook signing secret is the user's credential: list its presence, never echo
    # its value — mirrors how _mcp_view lists env keys without values.
    agent_id = _save_agent(authed_client)
    created = authed_client.post(
        "/triggers",
        json={
            "agent_id": agent_id,
            "kind": "webhook",
            "version": "0.1.0",
            "config": {"secret": "super-secret"},
        },
    )
    assert created.status_code == 201
    tid = created.json()["id"]
    assert created.json()["config"]["secret"] == "***"
    assert authed_client.get(f"/triggers/{tid}").json()["config"]["secret"] == "***"
    assert authed_client.get("/triggers").json()["triggers"][0]["config"]["secret"] == "***"


def test_trigger_crud_roundtrip(authed_client: TestClient) -> None:
    agent_id = _save_agent(authed_client)
    created = authed_client.post(
        "/triggers",
        json={
            "agent_id": agent_id,
            "kind": "schedule",
            "version": "0.1.0",
            "config": {"cron": "* * * * *"},
            "enabled": True,
        },
    )
    assert created.status_code == 201
    tid = created.json()["id"]
    # PATCH disable.
    patched = authed_client.patch(f"/triggers/{tid}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    # DELETE.
    assert authed_client.delete(f"/triggers/{tid}").status_code == 204
    assert authed_client.get(f"/triggers/{tid}").status_code == 404


def test_get_unknown_trigger_404(authed_client: TestClient) -> None:
    resp = authed_client.get("/triggers/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "trigger_not_found"


# ── Webhook trigger: signature-authed inbound ────────────────────────────────────


def _sign(secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def _webhook_trigger(client: TestClient, secret: str = "shh", version: str = "0.1.0") -> str:
    # An agent that drills a field of the inbound payload, so the test can prove payload → input.
    agent_id = _save_agent(client, _agent_ir(version=version, prompt="$in.in.text"))
    created = client.post(
        "/triggers",
        json={
            "agent_id": agent_id,
            "kind": "webhook",
            "version": version,
            "config": {"secret": secret},
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_webhook_valid_signature_fires_with_payload_as_input(
    authed_client: TestClient, fake_inference: FakeInference
) -> None:
    tid = _webhook_trigger(authed_client)
    raw = json.dumps({"text": "hello from webhook"}).encode()
    resp = authed_client.post(
        f"/hooks/{tid}",
        content=raw,
        headers={"X-Theygent-Signature": _sign("shh", raw), "Content-Type": "application/json"},
    )
    assert resp.status_code == 202, resp.text  # accepted + fired
    body = resp.json()
    assert body["status"] == "completed"
    # The payload mapped mechanically to the agent's input — drilled to the `text` field.
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "hello from webhook"}
    ]
    # The run carries trigger lineage — it was webhook-fired, not interactive.
    assert authed_client.get(f"/runs/{body['runId']}").json()["trigger_id"] == tid


def test_webhook_bad_signature_rejected(authed_client: TestClient) -> None:
    tid = _webhook_trigger(authed_client)
    raw = json.dumps({"text": "x"}).encode()
    resp = authed_client.post(
        f"/hooks/{tid}",
        content=raw,
        headers={"X-Theygent-Signature": "sha256=deadbeef", "Content-Type": "application/json"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_signature"


def test_webhook_missing_signature_rejected(authed_client: TestClient) -> None:
    tid = _webhook_trigger(authed_client)
    raw = json.dumps({"text": "x"}).encode()
    resp = authed_client.post(f"/hooks/{tid}", content=raw)
    assert resp.status_code == 401


def test_webhook_unknown_404(authed_client: TestClient) -> None:
    raw = b"{}"
    resp = authed_client.post(
        "/hooks/nope", content=raw, headers={"X-Theygent-Signature": _sign("shh", raw)}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "trigger_not_found"


def test_webhook_disabled_rejected(authed_client: TestClient) -> None:
    tid = _webhook_trigger(authed_client)
    authed_client.patch(f"/triggers/{tid}", json={"enabled": False})
    raw = json.dumps({"text": "x"}).encode()
    resp = authed_client.post(
        f"/hooks/{tid}", content=raw, headers={"X-Theygent-Signature": _sign("shh", raw)}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "trigger_disabled"


# ── Run lineage ───────────────────────────────────────────────────────────────────


def test_interactive_run_has_null_trigger_id(authed_client: TestClient) -> None:
    # A /graphs/runs run is interactive — trigger_id is NULL, distinguishing it from a fired run.
    resp = authed_client.post(
        "/graphs/runs", json={"ir": trivial_ir(), "input": "x", "stream": False}
    )
    assert resp.status_code == 200
    assert authed_client.get(f"/runs/{resp.json()['runId']}").json()["trigger_id"] is None


# ── Schedule trigger: the dispatcher tick ────────────────────────────────────────
# Async tests drive the lifespan in-loop (so the async engine + dispatcher live in the test's loop)
# and call dispatcher.tick directly — deterministic, no wall-clock waiting.


def _app(fake_url: str, pg_url: str, *, token: str = TOKEN):
    return create_app(
        inference_base_url=fake_url,
        database_url=pg_url,
        invoke_token=token,
        start_dispatcher=False,
    )


async def _save_and_schedule(
    ac: httpx.AsyncClient,
    *,
    ir: dict,
    version: str | None = None,
    content_hash_pin: str | None = None,
) -> tuple[str, str]:
    created = await ac.post("/agents", json={"ir": ir})
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    pin: dict = {"version": version} if version else {"content_hash": content_hash_pin}
    trig = await ac.post(
        "/triggers",
        json={
            "agent_id": agent_id,
            "kind": "schedule",
            "config": {"cron": "* * * * *", "input": "q"},
            **pin,
        },
    )
    assert trig.status_code == 201, trig.text
    return agent_id, trig.json()["id"]


async def test_schedule_tick_fires_pinned_version_with_trigger_id(
    fake_inference: FakeInference, pg_url: str
) -> None:
    app = _app(fake_inference.v1_url, pg_url)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            _agent, tid = await _save_and_schedule(
                ac, ir=_agent_ir(prompt="SCHED: $in"), version="0.1.0"
            )
            fired = await app.state.dispatcher.tick(_DUE)
            assert fired == [tid]
            runs = (await ac.get("/runs")).json()["runs"]
            mine = [r for r in runs if r["trigger_id"] == tid]
            assert len(mine) == 1
            assert mine[0]["status"] == "completed"
            assert mine[0]["graph_version"] == "0.1.0"
        # The schedule's input came from config.input, rendered through the pinned agent's prompt.
        assert fake_inference.captured["messages"] == [{"role": "user", "content": "SCHED: q"}]


async def test_disabled_schedule_does_not_fire(fake_inference: FakeInference, pg_url: str) -> None:
    app = _app(fake_inference.v1_url, pg_url)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            _agent, tid = await _save_and_schedule(ac, ir=_agent_ir(), version="0.1.0")
            assert (await ac.patch(f"/triggers/{tid}", json={"enabled": False})).status_code == 200
            fired = await app.state.dispatcher.tick(_DUE)
            assert fired == []
            runs = (await ac.get("/runs")).json()["runs"]
            assert [r for r in runs if r["trigger_id"] == tid] == []


async def test_schedule_persists_and_fires_across_restart(
    fake_inference: FakeInference, pg_url: str
) -> None:
    # Register the schedule through one app, then "restart" (a fresh app on the SAME Postgres) and
    # tick — the dispatcher re-reads it from the DB (rehydration is free) and fires it. The same
    # lesson learned for the MCP registry applies here: schedules persist and rehydrate for free.
    app1 = _app(fake_inference.v1_url, pg_url)
    async with app1.router.lifespan_context(app1):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app1), base_url="http://test"
        ) as ac:
            _agent, tid = await _save_and_schedule(ac, ir=_agent_ir(), version="0.1.0")

    app2 = _app(fake_inference.v1_url, pg_url)  # "restart": new app/session, same durable DB
    async with app2.router.lifespan_context(app2):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2), base_url="http://test"
        ) as ac:
            fired = await app2.state.dispatcher.tick(_DUE)
            assert fired == [tid]
            runs = (await ac.get("/runs")).json()["runs"]
            assert [r["trigger_id"] for r in runs if r["trigger_id"]] == [tid]


async def test_hash_pinned_schedule_runs_that_content_after_newer_publishes(
    fake_inference: FakeInference, pg_url: str
) -> None:
    # Pin a schedule to a contentHash, publish a NEWER version, then fire: it runs the pinned
    # content (immutability through the deploy path). Distinct path from version pinning.
    app = _app(fake_inference.v1_url, pg_url)
    ir_a = _agent_ir(version="0.1.0", prompt="VERSION_A: $in")
    hash_a = content_hash(parse_document(ir_a))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as ac:
            agent_id, tid = await _save_and_schedule(ac, ir=ir_a, content_hash_pin=hash_a)
            # Publish a newer version AFTER the pin was taken.
            newer = await ac.post(
                f"/agents/{agent_id}/versions",
                json={"ir": _agent_ir(version="0.2.0", prompt="VERSION_B: $in")},
            )
            assert newer.status_code == 201
            fired = await app.state.dispatcher.tick(_DUE)
            assert fired == [tid]
        # The pinned (VERSION_A) content ran, not the newer VERSION_B (config.input = "q").
        assert fake_inference.captured["messages"] == [{"role": "user", "content": "VERSION_A: q"}]


async def test_schedule_does_not_double_fire_on_immediate_retick(
    fake_inference: FakeInference, pg_url: str
) -> None:
    # No double-fire WITHIN one instance (the dispatcher's serial-tick guarantee): a fire stamps
    # last_fired_at, an immediate re-tick at the SAME instant sees the advanced cursor and is no
    # longer due. (Cross-instance double-fire is a recorded known constraint — dispatcher.py — that
    # the durable runtime's native schedules resolve; this dispatcher runs on exactly one instance.)
    app = _app(fake_inference.v1_url, pg_url)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as ac:
            _agent, tid = await _save_and_schedule(ac, ir=_agent_ir(), version="0.1.0")
            assert await app.state.dispatcher.tick(_DUE) == [tid]  # fires once
            assert await app.state.dispatcher.tick(_DUE) == []  # same instant → not due again
            runs = (await ac.get("/runs")).json()["runs"]
            assert len([r for r in runs if r["trigger_id"] == tid]) == 1  # exactly one run


async def test_completed_at_stamped_for_evidence_gate(
    fake_inference: FakeInference, pg_url: str
) -> None:
    # A fired run carries a real completed_at — duration is exact (completed_at - created_at) and
    # run intervals yield system-wide concurrency. An interactive completed run gets it too;
    # the value is >= created_at.
    app = _app(fake_inference.v1_url, pg_url)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as ac:
            _agent, tid = await _save_and_schedule(ac, ir=_agent_ir(), version="0.1.0")
            await app.state.dispatcher.tick(_DUE)
            run = next(r for r in (await ac.get("/runs")).json()["runs"] if r["trigger_id"] == tid)
            assert run["status"] == "completed"
            assert run["completed_at"] is not None
            assert run["completed_at"] >= run["created_at"]  # a non-negative duration


async def test_schedule_fire_against_unreachable_inference_fails_honestly(pg_url: str) -> None:
    # A fired run whose bound inference plane is unreachable ends `failed` cleanly — no hang.
    # The control-plane never resolves egress; it surfaces the failure honestly via the run path.
    app = _app("http://127.0.0.1:1/v1", pg_url)  # a dead inference port
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as ac:
            _agent, tid = await _save_and_schedule(ac, ir=_agent_ir(), version="0.1.0")
            fired = await app.state.dispatcher.tick(_DUE)
            assert fired == [tid]  # the schedule fired (and advanced) even though the run failed
            runs = (await ac.get("/runs")).json()["runs"]
            mine = [r for r in runs if r["trigger_id"] == tid]
            assert len(mine) == 1
            assert mine[0]["status"] == "failed"  # honest failure, recorded — not retried
