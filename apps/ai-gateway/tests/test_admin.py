"""The gateway's admin surface: who may use it, what it shows, the org key, and CORS.

What must hold:

* team mode admits only callers the control plane vouches for as admins, and refuses — never
  admits — when the control plane cannot answer; a standalone personal gateway is open, like the
  gateway itself on loopback;
* answers are cached briefly by the bearer's digest;
* no response or log line carries a secret;
* a single-instance org key applies at once, survives a restart, and can be cleared; a key the
  deployment set cannot be replaced; personal mode has no org key;
* browsers get CORS on ``/admin/*`` from the interface's origins only, never on proxied paths.
"""

from __future__ import annotations

import hashlib
import json
import logging
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _support import TEAM_KEY, FakeSink, team_keys
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from theygent_ai_gateway.admin import CachedTokenCheck, ControlPlaneTokenCheck
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.configsync import StaticConfigSource
from theygent_ai_gateway.credentials import ORG_KEY_FILE
from theygent_ai_gateway.identity import StaticKeySource
from theygent_ai_gateway.settings import (
    ENV_ADMIN_ORIGINS,
    GatewaySettings,
    SettingsError,
    load_settings,
)
from theygent_ir.gateway import GatewayKey, GatewayKeySet, GatewayTokenCheckResult

ADMIN = "tys_admin-session-bearer"
VIEWER = "tys_viewer-session-bearer"
SERVICE_KEY = "tygs_service_key_for_tests"
ORG_KEY = "sk-ant-api03-ORGKEY-0123456789abcdef"
INTERFACE = "http://localhost:5174"
ORG_DEV_KEY = "tyg_org_billed_developer_key"


class FakeCheck:
    """The control plane's answer per bearer, counting how often it was asked."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.fail = fail
        self.answers = {
            ADMIN: GatewayTokenCheckResult(active=True, user_id="usr_admin", role="admin"),
            VIEWER: GatewayTokenCheckResult(active=True, user_id="usr_viewer", role="viewer"),
        }

    async def check(self, token: str) -> GatewayTokenCheckResult:
        self.calls.append(token)
        if self.fail is not None:
            raise self.fail
        return self.answers.get(token, GatewayTokenCheckResult(active=False))


def key_set_with_org_developer() -> GatewayKeySet:
    keys = team_keys()
    org = GatewayKey(
        key_id="key_org",
        token_sha256=hashlib.sha256(ORG_DEV_KEY.encode()).hexdigest(),
        developer_id="usr_org",
        credential_profile="org_key",
    )
    return GatewayKeySet(version=1, generated_at=keys.generated_at, keys=[*keys.keys, org])


def team_settings(tmp_path: Path, upstream: str, org_key: str | None = None) -> GatewaySettings:
    return GatewaySettings(
        upstream_url=upstream,
        auth_mode="team",
        control_plane_url="http://127.0.0.1:1",
        service_key=SERVICE_KEY,
        org_api_key=org_key,
        gateway_id="gw-team",
        state_dir=tmp_path,
    )


def team_app(settings: GatewaySettings, check: FakeCheck) -> Any:
    return create_app(
        settings,
        key_source=StaticKeySource(key_set_with_org_developer()),
        config_source=StaticConfigSource(),
        event_sink=FakeSink(),
        token_check=check,
    )


@pytest.fixture
def upstream() -> Iterator[FakeAnthropic]:
    with fake_anthropic() as state:
        yield state


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ── who may administer ───────────────────────────────────────────────────────


def test_a_standalone_personal_gateway_is_open(tmp_path: Path) -> None:
    settings = GatewaySettings(state_dir=tmp_path, gateway_id="gw-me")
    with TestClient(create_app(settings)) as client:
        status = client.get("/admin/status")
        assert status.status_code == 200
        body = status.json()
        assert (body["gatewayId"], body["authMode"], body["mode"]) == (
            "gw-me",
            "personal",
            "observer",
        )
        assert body["keySet"] is None and body["configVersion"] is None
        assert body["requestLog"]["backlog"] == 0 and body["startedAt"] is not None
        assert body["credentials"] == {
            "orgApiKey": {"set": False, "source": None, "updatedAt": None, "writable": False},
            "serviceKey": {"set": False},
        }
        refused = client.put("/admin/credentials/org-api-key", json={"api_key": ORG_KEY})
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            409,
            "org_key_not_applicable",
        )


@pytest.mark.parametrize(
    ("headers", "status", "code"),
    [
        ({}, 401, "admin_auth_required"),
        (bearer("tys_unknown"), 401, "admin_auth_invalid"),
        (bearer(VIEWER), 403, "admin_required"),
        ({"Authorization": f"Basic {ADMIN}"}, 401, "admin_auth_required"),
    ],
)
def test_team_mode_admits_admins_only(
    tmp_path: Path, upstream: FakeAnthropic, headers: dict[str, str], status: int, code: str
) -> None:
    with TestClient(team_app(team_settings(tmp_path, upstream.base_url), FakeCheck())) as client:
        refused = client.get("/admin/status", headers=headers)
        assert (refused.status_code, refused.json()["error"]["code"]) == (status, code)
        allowed = client.get("/admin/status", headers=bearer(ADMIN))
        assert allowed.status_code == 200
        assert allowed.json()["keySet"] == {"loaded": True, "version": 1}


def test_the_surface_fails_closed_when_the_control_plane_cannot_answer(
    tmp_path: Path, upstream: FakeAnthropic
) -> None:
    check = FakeCheck(fail=httpx.ConnectError("control plane down"))
    with TestClient(team_app(team_settings(tmp_path, upstream.base_url), check)) as client:
        refused = client.get("/admin/credentials", headers=bearer(ADMIN))
        assert (refused.status_code, refused.json()["error"]["code"]) == (
            503,
            "control_plane_unavailable",
        )
        assert refused.headers["retry-after"] == "5"


def test_a_team_gateway_without_a_checker_refuses_rather_than_opens(
    tmp_path: Path, upstream: FakeAnthropic
) -> None:
    # Before startup has connected the control plane there is no checker; only a standalone
    # personal gateway may be open then.
    app = create_app(team_settings(tmp_path, upstream.base_url))
    refused = TestClient(app).get("/admin/status")  # no lifespan: nothing connected yet
    assert (refused.status_code, refused.json()["error"]["code"]) == (
        503,
        "control_plane_unavailable",
    )


async def test_answers_are_cached_briefly_by_digest() -> None:
    now = [0.0]
    inner = FakeCheck()
    cached = CachedTokenCheck(inner, allow_seconds=30, deny_seconds=5, clock=lambda: now[0])
    for _ in range(3):
        assert (await cached.check(ADMIN)).role == "admin"
        assert (await cached.check(VIEWER)).role == "viewer"
    assert inner.calls == [ADMIN, VIEWER]
    now[0] = 6  # a refused (non-admin) answer expires first
    await cached.check(ADMIN)
    await cached.check(VIEWER)
    assert inner.calls == [ADMIN, VIEWER, VIEWER]
    now[0] = 31
    await cached.check(ADMIN)
    assert inner.calls[-1] == ADMIN
    assert ADMIN not in repr(cached._answers)  # keyed by digest, never the bearer


# ── the org key ──────────────────────────────────────────────────────────────


def org_request(client: TestClient) -> httpx.Response:
    return client.post(
        "/v1/messages",
        content=json.dumps({"model": "claude-opus-5-5", "max_tokens": 1, "messages": []}),
        headers={"x-theygent-gateway-key": ORG_DEV_KEY},
    )


def test_a_stored_org_key_applies_at_once_and_survives_a_restart(
    tmp_path: Path, upstream: FakeAnthropic, caplog: pytest.LogCaptureFixture
) -> None:
    settings = team_settings(tmp_path, upstream.base_url)
    with caplog.at_level(logging.DEBUG), TestClient(team_app(settings, FakeCheck())) as client:
        before = client.get("/admin/credentials", headers=bearer(ADMIN)).json()
        assert before["orgApiKey"] == {
            "set": False,
            "source": None,
            "updatedAt": None,
            "writable": True,
        }
        assert (
            org_request(client).headers.get("x-theygent-gateway-error") == "org_key_not_configured"
        )

        stored = client.put(
            "/admin/credentials/org-api-key", json={"api_key": ORG_KEY}, headers=bearer(ADMIN)
        )
        assert stored.status_code == 200
        assert stored.json()["orgApiKey"]["set"] is True
        assert stored.json()["orgApiKey"]["source"] == "stored"
        assert ORG_KEY not in stored.text
        assert org_request(client).status_code == 200
        assert upstream.last().header("x-api-key") == [ORG_KEY]
        for path in ("/admin/status", "/admin/credentials"):
            text = client.get(path, headers=bearer(ADMIN)).text
            assert ORG_KEY not in text and SERVICE_KEY not in text
    assert ORG_KEY not in caplog.text and ADMIN not in caplog.text
    path = tmp_path / ORG_KEY_FILE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    with TestClient(team_app(settings, FakeCheck())) as client:  # a restart
        assert org_request(client).status_code == 200
        cleared = client.delete("/admin/credentials/org-api-key", headers=bearer(ADMIN))
        assert cleared.json()["orgApiKey"]["set"] is False
        assert (
            org_request(client).headers.get("x-theygent-gateway-error") == "org_key_not_configured"
        )
    assert not path.exists()


def test_a_key_the_deployment_set_cannot_be_replaced(
    tmp_path: Path, upstream: FakeAnthropic
) -> None:
    settings = team_settings(tmp_path, upstream.base_url, org_key=ORG_KEY)
    with TestClient(team_app(settings, FakeCheck())) as client:
        view = client.get("/admin/credentials", headers=bearer(ADMIN)).json()
        assert view["orgApiKey"] == {
            "set": True,
            "source": "env",
            "updatedAt": None,
            "writable": False,
        }
        assert view["serviceKey"] == {"set": True}
        for call in (
            lambda: client.put(
                "/admin/credentials/org-api-key",
                json={"api_key": "sk-ant-api03-another-key-0000"},
                headers=bearer(ADMIN),
            ),
            lambda: client.delete("/admin/credentials/org-api-key", headers=bearer(ADMIN)),
        ):
            refused = call()
            assert (refused.status_code, refused.json()["error"]["code"]) == (409, "org_key_pinned")
        assert org_request(client).status_code == 200
        assert upstream.last().header("x-api-key") == [ORG_KEY]


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": "short"},
        {"api_key": "sk-ant has a space"},
        {"api_key": ORG_KEY, "extra": ORG_KEY},
        {"key": ORG_KEY},
    ],
)
def test_a_bad_key_is_refused_without_echo(
    tmp_path: Path, upstream: FakeAnthropic, body: dict[str, Any]
) -> None:
    with TestClient(team_app(team_settings(tmp_path, upstream.base_url), FakeCheck())) as client:
        refused = client.put("/admin/credentials/org-api-key", json=body, headers=bearer(ADMIN))
        assert (refused.status_code, refused.json()["error"]["code"]) == (422, "invalid_api_key")
        assert body.get("api_key", body.get("key", "")) not in refused.text
        assert not (tmp_path / ORG_KEY_FILE).exists()


# ── CORS and routing ─────────────────────────────────────────────────────────


def preflight(client: TestClient, path: str, origin: str) -> httpx.Response:
    return client.options(
        path,
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization",
        },
    )


def test_browsers_reach_the_admin_surface_from_the_interface_only(
    tmp_path: Path, upstream: FakeAnthropic
) -> None:
    settings = GatewaySettings(upstream_url=upstream.base_url, state_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        allowed = preflight(client, "/admin/status", INTERFACE)
        assert allowed.headers["access-control-allow-origin"] == INTERFACE
        assert (
            "access-control-allow-origin"
            not in preflight(client, "/admin/status", "https://evil.example").headers
        )
        simple = client.get("/admin/status", headers={"Origin": INTERFACE})
        assert simple.headers["access-control-allow-origin"] == INTERFACE
        # A preflight to a proxied path is forwarded like any request, never answered here: the
        # 405 is the upstream's (it serves POST there), where CORS would have answered 200.
        forwarded = preflight(client, "/v1/messages", INTERFACE)
        assert forwarded.status_code == 405
        assert "access-control-allow-origin" not in forwarded.headers


def test_admin_paths_are_never_forwarded(tmp_path: Path, upstream: FakeAnthropic) -> None:
    settings = GatewaySettings(upstream_url=upstream.base_url, state_dir=tmp_path)
    with TestClient(create_app(settings)) as client:
        missing = client.get("/admin/secrets")
        assert missing.status_code == 404
    assert upstream.requests == []


# ── the control plane's check, over HTTP ─────────────────────────────────────


async def test_the_control_plane_check_sends_the_token_with_the_service_key() -> None:
    seen: list[tuple[str | None, dict[str, Any]]] = []
    fail = [False]

    async def introspect(request: Request) -> Response:
        seen.append((request.headers.get("authorization"), json.loads(await request.body())))
        if fail[0]:
            return Response(status_code=500)
        return JSONResponse({"active": True, "user_id": "usr_admin", "role": "admin"})

    app = Starlette(routes=[Route("/gateway/introspect", introspect, methods=["POST"])])
    with serve(app) as url:
        async with httpx.AsyncClient() as client:
            check = ControlPlaneTokenCheck(client, url, SERVICE_KEY)
            assert (await check.check(ADMIN)).role == "admin"
            fail[0] = True
            with pytest.raises(httpx.HTTPStatusError):
                await check.check(ADMIN)
    assert seen[0] == (f"Bearer {SERVICE_KEY}", {"token": ADMIN})


# ── settings ─────────────────────────────────────────────────────────────────


def test_admin_origins_default_to_the_interface_and_can_be_set() -> None:
    assert load_settings({}).admin_origins == ("http://localhost:5174", "http://127.0.0.1:5174")
    assert load_settings({ENV_ADMIN_ORIGINS: ""}).admin_origins == load_settings({}).admin_origins
    custom = load_settings({ENV_ADMIN_ORIGINS: "https://ygent.example.com/, http://ops:5174"})
    assert custom.admin_origins == ("https://ygent.example.com", "http://ops:5174")
    with pytest.raises(SettingsError, match=ENV_ADMIN_ORIGINS):
        load_settings({ENV_ADMIN_ORIGINS: "not a url"})


def test_the_team_key_still_reaches_the_upstream_untouched(
    tmp_path: Path, upstream: FakeAnthropic
) -> None:
    # The admin surface changes nothing for passthrough developers.
    with TestClient(team_app(team_settings(tmp_path, upstream.base_url), FakeCheck())) as client:
        sent = client.post(
            "/v1/messages",
            content=json.dumps({"model": "claude-opus-5-5", "max_tokens": 1, "messages": []}),
            headers={"x-theygent-gateway-key": TEAM_KEY, "authorization": "Bearer sk-ant-oat01-x"},
        )
        assert sent.status_code == 200
    assert upstream.last().header("x-api-key") == []
