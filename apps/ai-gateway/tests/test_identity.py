"""Developer identity and credential profiles.

Personal mode forwards the caller's own credential. Team mode authenticates every request with a
gateway key and fails closed; the passthrough profile forwards the developer's own Anthropic
credential, the org_key profile replaces every client credential with the organization's key. A
gateway key never reaches the upstream, and the org key never reaches the client or the logs.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.identity import (
    ControlPlaneKeySource,
    KeyRing,
    StaticKeySource,
    presented_gateway_key,
    sync_once,
    token_digest,
)
from theygent_ai_gateway.observe import Observation
from theygent_ai_gateway.proxy import GATEWAY_ERROR_HEADER, upstream_request_headers
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ir.gateway import CredentialProfile, GatewayKey, GatewayKeySet

WAIT_SEC = 10.0
SEAT_KEY = "tyg_seat_developer_key_0001"
ORG_KEY_DEV = "tyg_org_developer_key_0002"
REVOKED = "tyg_revoked_key_0003"
ORG_API_KEY = "sk-ant-api03-ORG-SECRET-KEY"
OAUTH = "Bearer sk-ant-oat01-developer-login"
BODY = b'{"model":"claude-opus-5-5","max_tokens":10,"messages":[{"role":"user","content":"hi"}]}'


def key_set(version: int, *keys: tuple[str, str, CredentialProfile]) -> GatewayKeySet:
    return GatewayKeySet(
        version=version,
        generated_at=datetime.now(UTC),
        keys=[
            GatewayKey(
                key_id=f"key_{n}",
                token_sha256=token_digest(token),
                developer_id=developer,
                credential_profile=profile,
            )
            for n, (token, developer, profile) in enumerate(keys)
        ],
    )


TEAM_KEYS = key_set(1, (SEAT_KEY, "usr_seat", "passthrough"), (ORG_KEY_DEV, "usr_org", "org_key"))


# ── presenting a key ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("headers", "token"),
    [
        ({"x-theygent-gateway-key": SEAT_KEY, "authorization": OAUTH}, SEAT_KEY),
        ({"authorization": f"Bearer {ORG_KEY_DEV}"}, ORG_KEY_DEV),
        ({"x-api-key": ORG_KEY_DEV}, ORG_KEY_DEV),
        ({"x-theygent-gateway-key": SEAT_KEY, "x-api-key": ORG_KEY_DEV}, SEAT_KEY),
        ({"authorization": OAUTH}, None),
        ({"x-api-key": "sk-ant-api03-personal"}, None),
        ({}, None),
    ],
)
def test_a_presented_key_is_found_where_claude_code_puts_it(
    headers: dict[str, str], token: str | None
) -> None:
    assert presented_gateway_key(headers) == token


# ── the key ring ─────────────────────────────────────────────────────────────


def test_the_ring_resolves_keys_by_digest() -> None:
    ring = KeyRing()
    ring.replace(TEAM_KEYS)
    identity = ring.lookup(SEAT_KEY)
    assert identity is not None
    assert (identity.developer_id, identity.credential_profile) == ("usr_seat", "passthrough")
    assert ring.lookup("tyg_unknown") is None


def test_the_ring_never_moves_backwards() -> None:
    ring = KeyRing()
    ring.replace(key_set(5, (SEAT_KEY, "usr_seat", "passthrough")))
    assert not ring.replace(key_set(4))
    assert ring.version == 5 and ring.lookup(SEAT_KEY) is not None


def test_the_ring_persists_digests_only_and_reloads(tmp_path: Path) -> None:
    KeyRing(tmp_path).replace(TEAM_KEYS)
    stored = tmp_path / "keyset.json"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert SEAT_KEY not in stored.read_text()
    fresh = KeyRing(tmp_path)
    assert fresh.load_from_disk()
    assert fresh.lookup(SEAT_KEY) is not None


def test_a_corrupt_persisted_ring_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "keyset.json").write_text("{not json")
    ring = KeyRing(tmp_path)
    assert not ring.load_from_disk()
    assert not ring.loaded


class FailingSource:
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def fetch(self, since_version: int | None) -> GatewayKeySet | None:
        raise self.error


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("down"), ValueError("bad json"), httpx.ReadTimeout("slow")]
)
async def test_a_failed_sync_keeps_the_known_keys(error: Exception) -> None:
    ring = KeyRing()
    ring.replace(TEAM_KEYS)
    await sync_once(ring, FailingSource(error))
    assert ring.lookup(SEAT_KEY) is not None


# ── the control-plane key source ─────────────────────────────────────────────


class FakeControlPlane:
    def __init__(self) -> None:
        self.key_set = TEAM_KEYS
        self.status = 200
        self.body: bytes | None = None
        self.seen: list[tuple[str | None, str | None]] = []

    def app(self) -> Starlette:
        async def sync(request: Request) -> Response:
            since = request.query_params.get("since")
            self.seen.append((request.headers.get("authorization"), since))
            if self.status != 200:
                return Response(status_code=self.status)
            if self.body is not None:
                return Response(self.body, media_type="application/json")
            if since is not None and int(since) == self.key_set.version:
                return Response(status_code=304)
            return JSONResponse(self.key_set.model_dump(mode="json"))

        return Starlette(routes=[Route("/gateway/keys/sync", sync)])


@pytest.fixture
def control_plane() -> Iterator[tuple[FakeControlPlane, str]]:
    fake = FakeControlPlane()
    with serve(fake.app()) as url:
        yield fake, url


async def test_the_control_plane_source_fetches_and_honours_not_modified(
    control_plane: tuple[FakeControlPlane, str],
) -> None:
    fake, url = control_plane
    ring = KeyRing()
    async with httpx.AsyncClient() as client:
        source = ControlPlaneKeySource(client, url, "tygs_service_key")
        await sync_once(ring, source)
        assert ring.version == 1
        await sync_once(ring, source)  # unchanged: 304
        fake.key_set = key_set(2, (SEAT_KEY, "usr_seat", "passthrough"))
        await sync_once(ring, source)
    assert ring.version == 2
    assert ring.lookup(ORG_KEY_DEV) is None  # dropped from the new set: revoked
    assert fake.seen == [
        ("Bearer tygs_service_key", None),
        ("Bearer tygs_service_key", "1"),
        ("Bearer tygs_service_key", "1"),
    ]


@pytest.mark.parametrize("problem", ["server_error", "not_a_key_set"])
async def test_a_bad_control_plane_answer_keeps_the_known_keys(
    control_plane: tuple[FakeControlPlane, str], problem: str
) -> None:
    fake, url = control_plane
    ring = KeyRing()
    ring.replace(TEAM_KEYS)
    if problem == "server_error":
        fake.status = 500
    else:
        fake.body = b'{"version": 9, "keys": [{"token": "tyg_plaintext"}]}'
    async with httpx.AsyncClient() as client:
        await sync_once(ring, ControlPlaneKeySource(client, url, "tygs_service_key"))
    assert ring.version == 1


# ── what the upstream sees, per profile ──────────────────────────────────────


def _names(headers: list[tuple[bytes, bytes]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name, value in headers:
        out.setdefault(name.decode().lower(), []).append(value.decode())
    return out


def test_passthrough_keeps_the_developers_credential_and_drops_gateway_tokens() -> None:
    raw = [
        (b"Authorization", OAUTH.encode()),
        (b"X-TheYgent-Gateway-Key", SEAT_KEY.encode()),
        (b"anthropic-beta", b"oauth-2025-04-20"),
    ]
    sent = _names(upstream_request_headers(raw))
    assert sent["authorization"] == [OAUTH]
    assert "x-theygent-gateway-key" not in sent
    assert sent["anthropic-beta"] == ["oauth-2025-04-20"]


@pytest.mark.parametrize("header", [b"Authorization", b"x-api-key"])
def test_a_gateway_token_in_a_credential_header_never_goes_upstream(header: bytes) -> None:
    value = f"Bearer {ORG_KEY_DEV}" if header == b"Authorization" else ORG_KEY_DEV
    sent = _names(upstream_request_headers([(header, value.encode())]))
    assert all(ORG_KEY_DEV not in v for values in sent.values() for v in values)


def test_org_key_replaces_every_client_credential() -> None:
    raw = [
        (b"Authorization", OAUTH.encode()),
        (b"x-api-key", b"sk-ant-api03-personal"),
        (b"content-type", b"application/json"),
    ]
    sent = _names(upstream_request_headers(raw, profile="org_key", org_api_key=ORG_API_KEY))
    assert "authorization" not in sent
    assert sent["x-api-key"] == [ORG_API_KEY]
    assert sent["content-type"] == ["application/json"]


# ── through the gateway: every mode and profile───────────────────────────────


class Collector:
    def __init__(self) -> None:
        self.items: list[Observation] = []
        self.lock = threading.Lock()

    def __call__(self, observation: Observation) -> None:
        with self.lock:
            self.items.append(observation)

    def last(self) -> Observation:
        deadline = time.monotonic() + WAIT_SEC
        while time.monotonic() < deadline:
            with self.lock:
                if self.items:
                    return self.items[-1]
            time.sleep(0.01)
        raise AssertionError("no observation arrived")


@pytest.fixture
def upstream() -> Iterator[FakeAnthropic]:
    with fake_anthropic() as state:
        yield state


@pytest.fixture
def collector() -> Collector:
    return Collector()


def _team_settings(
    upstream: FakeAnthropic, tmp_path: Path, *, org_key: str | None
) -> GatewaySettings:
    return GatewaySettings(
        upstream_url=upstream.base_url,
        auth_mode="team",
        control_plane_url="http://127.0.0.1:1",
        service_key="tygs_service_key",
        org_api_key=org_key,
        key_sync_seconds=1,
        gateway_id="gw-team",
        state_dir=tmp_path,
    )


def _serve_gateway(settings: GatewaySettings, source, collector) -> Iterator[str]:
    with serve(create_app(settings, key_source=source, observe=collector)) as url:
        deadline = time.monotonic() + WAIT_SEC
        while httpx.get(f"{url}/readyz").status_code != 200:
            assert time.monotonic() < deadline, "the gateway never loaded its keys"
            time.sleep(0.02)
        yield url


@pytest.fixture
def source() -> StaticKeySource:
    return StaticKeySource(TEAM_KEYS)


@pytest.fixture
def team(
    upstream: FakeAnthropic, tmp_path: Path, source: StaticKeySource, collector: Collector
) -> Iterator[str]:
    yield from _serve_gateway(
        _team_settings(upstream, tmp_path, org_key=ORG_API_KEY), source, collector
    )


@pytest.fixture
def personal(upstream: FakeAnthropic, tmp_path: Path, collector: Collector) -> Iterator[str]:
    settings = GatewaySettings(
        upstream_url=upstream.base_url, gateway_id="gw-me", state_dir=tmp_path
    )
    with serve(create_app(settings, observe=collector)) as url:
        yield url


def _post(url: str, headers: dict[str, str], path: str = "/v1/messages") -> httpx.Response:
    return httpx.post(f"{url}{path}", content=BODY, headers=headers, timeout=WAIT_SEC)


def _assert_no_gateway_token_upstream(upstream: FakeAnthropic) -> None:
    for request in upstream.requests:
        for _, value in request.headers:
            assert "tyg_" not in value


def _assert_refused(
    response: httpx.Response, status: int, code: str, upstream: FakeAnthropic
) -> None:
    assert response.status_code == status
    assert response.headers[GATEWAY_ERROR_HEADER] == code
    body = response.json()
    assert body["type"] == "error" and body["error"]["type"] == "api_error"
    assert upstream.requests == [], "a refused request must not be forwarded"


def test_personal_mode_forwards_the_callers_own_credential(
    personal: str, upstream: FakeAnthropic, collector: Collector
) -> None:
    response = _post(personal, {"authorization": OAUTH, "x-theygent-gateway-key": SEAT_KEY})
    assert response.status_code == 200
    assert upstream.last().header("authorization") == [OAUTH]
    _assert_no_gateway_token_upstream(upstream)
    assert collector.last().identity is None


def test_team_passthrough_forwards_the_seat_login(
    team: str, upstream: FakeAnthropic, collector: Collector
) -> None:
    response = _post(team, {"authorization": OAUTH, "x-theygent-gateway-key": SEAT_KEY})
    assert response.status_code == 200
    assert upstream.last().header("authorization") == [OAUTH]
    assert upstream.last().header("x-api-key") == []
    _assert_no_gateway_token_upstream(upstream)
    identity = collector.last().identity
    assert identity is not None
    assert (identity.developer_id, identity.credential_profile) == ("usr_seat", "passthrough")


def test_team_passthrough_without_an_upstream_credential_is_refused(
    team: str, upstream: FakeAnthropic
) -> None:
    response = _post(team, {"authorization": f"Bearer {SEAT_KEY}"})
    _assert_refused(response, 401, "upstream_credential_missing", upstream)


@pytest.mark.parametrize("header", ["authorization", "x-api-key"])
def test_team_org_key_injects_the_organizations_key(
    team: str, upstream: FakeAnthropic, collector: Collector, header: str
) -> None:
    value = f"Bearer {ORG_KEY_DEV}" if header == "authorization" else ORG_KEY_DEV
    response = _post(team, {header: value})
    assert response.status_code == 200
    assert upstream.last().header("x-api-key") == [ORG_API_KEY]
    assert upstream.last().header("authorization") == []
    _assert_no_gateway_token_upstream(upstream)
    assert ORG_API_KEY not in response.text
    identity = collector.last().identity
    assert identity is not None and identity.credential_profile == "org_key"


def test_team_org_key_drops_a_leftover_seat_login(team: str, upstream: FakeAnthropic) -> None:
    response = _post(team, {"authorization": OAUTH, "x-theygent-gateway-key": ORG_KEY_DEV})
    assert response.status_code == 200
    assert upstream.last().header("authorization") == []
    assert upstream.last().header("x-api-key") == [ORG_API_KEY]


def test_the_org_key_serves_claude_codes_endpoints_only(team: str, upstream: FakeAnthropic) -> None:
    headers = {"x-theygent-gateway-key": ORG_KEY_DEV}
    _assert_refused(_post(team, headers, "/v1/files"), 403, "endpoint_not_allowed", upstream)
    assert httpx.get(f"{team}/v1/models", headers=headers, timeout=WAIT_SEC).status_code == 200
    assert _post(team, headers, "/v1/messages/count_tokens").status_code == 200


def test_team_org_key_without_a_configured_key_is_refused(
    upstream: FakeAnthropic, tmp_path: Path, source: StaticKeySource, collector: Collector
) -> None:
    for url in _serve_gateway(_team_settings(upstream, tmp_path, org_key=None), source, collector):
        response = _post(url, {"x-theygent-gateway-key": ORG_KEY_DEV})
    _assert_refused(response, 503, "org_key_not_configured", upstream)


def test_a_missing_key_is_refused(team: str, upstream: FakeAnthropic, collector: Collector) -> None:
    response = _post(team, {"authorization": OAUTH})
    _assert_refused(response, 401, "gateway_key_missing", upstream)
    assert response.headers["x-should-retry"] == "false"
    observation = collector.last()
    assert (observation.outcome, observation.status, observation.identity) == (
        "rejected",
        401,
        None,
    )


@pytest.mark.parametrize("key", ["tyg_never_issued", REVOKED])
def test_an_unknown_key_is_refused(team: str, upstream: FakeAnthropic, key: str) -> None:
    response = _post(team, {"authorization": OAUTH, "x-theygent-gateway-key": key})
    _assert_refused(response, 401, "gateway_key_invalid", upstream)


def test_a_revoked_key_is_refused_within_one_sync_interval(
    team: str, upstream: FakeAnthropic, source: StaticKeySource
) -> None:
    headers = {"authorization": OAUTH, "x-theygent-gateway-key": SEAT_KEY}
    assert _post(team, headers).status_code == 200
    source.key_set = key_set(2, (ORG_KEY_DEV, "usr_org", "org_key"))
    revoked_at = time.monotonic()
    while _post(team, headers).status_code == 200:
        assert time.monotonic() - revoked_at < 2 * 1 + 1, "still accepted after a sync interval"
        time.sleep(0.05)
    assert _post(team, headers).headers[GATEWAY_ERROR_HEADER] == "gateway_key_invalid"


def test_no_keys_yet_means_no_service(
    upstream: FakeAnthropic, tmp_path: Path, collector: Collector
) -> None:
    app = create_app(
        _team_settings(upstream, tmp_path, org_key=ORG_API_KEY),
        key_source=StaticKeySource(None),
        observe=collector,
    )
    with serve(app) as url:
        assert httpx.get(f"{url}/readyz").status_code == 503
        response = _post(url, {"x-theygent-gateway-key": SEAT_KEY, "authorization": OAUTH})
    _assert_refused(response, 503, "gateway_keys_unavailable", upstream)
    assert response.headers["x-should-retry"] == "true"


def test_a_restart_without_the_control_plane_still_authenticates(
    upstream: FakeAnthropic, tmp_path: Path, source: StaticKeySource, collector: Collector
) -> None:
    settings = _team_settings(upstream, tmp_path, org_key=ORG_API_KEY)
    for _ in _serve_gateway(settings, source, collector):
        pass  # first run: keys synced and persisted
    for url in _serve_gateway(settings, FailingSource(httpx.ConnectError("down")), collector):
        response = _post(url, {"authorization": OAUTH, "x-theygent-gateway-key": SEAT_KEY})
    assert response.status_code == 200


def test_team_answers_the_warm_up_probe_without_credentials(
    team: str, upstream: FakeAnthropic
) -> None:
    assert httpx.head(f"{team}/api/hello", timeout=WAIT_SEC).status_code == 200
    assert upstream.requests == []


def test_readyz_reports_the_key_set(team: str) -> None:
    body = httpx.get(f"{team}/readyz").json()
    assert body["ready"] is True
    assert body["keySetLoaded"] is True and body["keySetVersion"] == 1


def test_secrets_stay_out_of_the_logs(
    team: str, upstream: FakeAnthropic, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        _post(team, {"x-theygent-gateway-key": ORG_KEY_DEV})
        _post(team, {"x-theygent-gateway-key": "tyg_never_issued", "authorization": OAUTH})
    for secret in (ORG_API_KEY, ORG_KEY_DEV, SEAT_KEY, "tyg_never_issued", OAUTH):
        assert secret not in caplog.text
