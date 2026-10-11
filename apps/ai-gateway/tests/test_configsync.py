"""Config and heartbeat: the config in effect, pulled from the control plane and applied live.

What must hold:

* versions only move forward, and each exchange is decided under one snapshot whose version its
  record carries;
* an unchanged config costs a ``304`` (the version is the ETag), and a heartbeat that names the
  applied version costs no pull at all;
* the last known good config survives a restart with the control plane down;
* a gateway attached to a control plane records nothing until it has been configured;
* the heartbeat reports what the control plane's instance list shows.
"""

from __future__ import annotations

import asyncio
import dataclasses
import stat
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from _fake_anthropic import fake_anthropic, serve
from _fixtures import load
from _support import FakeSink, observation, send, spool_records, team_keys, until, wait_for
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.configsync import (
    ConfigState,
    ControlPlaneConfigSource,
    StaticConfigSource,
    UpstreamHealth,
    sync_config_forever,
    sync_config_once,
)
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.identity import StaticKeySource
from theygent_ai_gateway.observe import Observation
from theygent_ai_gateway.requestlog import ExportRefused
from theygent_ai_gateway.settings import AuthMode, GatewaySettings
from theygent_ir.gateway import (
    GatewayConfig,
    GatewayHeartbeat,
    GatewayHeartbeatAck,
    GatewayMode,
    ModelRates,
    Outcome,
)

WAIT_SEC = 10
T0 = datetime(2026, 10, 10, 9, tzinfo=UTC)
SERVICE_KEY = "tygs_service_key"
PRICES = PriceBook.packaged()
#: Contracted Sonnet rates: half the list price.
SONNET_HALF = ModelRates(
    input=1.0, output=5.0, cache_write_5m=1.25, cache_write_1h=2.0, cache_read=0.1
)


def config(version: int, mode: GatewayMode = "observer", **fields: object) -> GatewayConfig:
    return GatewayConfig.model_validate(
        {"version": version, "mode": mode, "updated_at": T0, **fields}
    )


def beat(config_version: int | None = None) -> GatewayHeartbeat:
    return GatewayHeartbeat(
        gateway_id="gw-test",
        software_version="0.1.0",
        mode="observer",
        config_version=config_version,
        started_at=T0,
    )


def readyz(url: str) -> dict[str, object]:
    return httpx.get(f"{url}/readyz", timeout=WAIT_SEC).json()


# ── the config in effect ─────────────────────────────────────────────────────


def test_an_unconfigured_state_has_no_version_and_its_given_mode() -> None:
    state = ConfigState(PRICES, unconfigured_mode="passthrough")
    snapshot = state.current
    assert (snapshot.config, snapshot.version, snapshot.mode) == (None, None, "passthrough")
    assert snapshot.prices is PRICES


def test_versions_only_move_forward() -> None:
    state = ConfigState(PRICES, unconfigured_mode="passthrough")
    assert state.apply(config(2), persist=False)
    assert (state.current.version, state.current.mode) == (2, "observer")
    assert not state.apply(config(1, "passthrough"), persist=False)
    assert not state.apply(config(2, "passthrough"), persist=False)
    assert (state.current.version, state.current.mode) == (2, "observer")


def test_a_configs_contracted_rates_price_its_snapshot() -> None:
    state = ConfigState(PRICES, unconfigured_mode="observer")
    state.apply(config(1, pricing_overrides={"claude-sonnet-5-5": SONNET_HALF}), persist=False)
    prices = state.current.prices
    assert prices.version == f"{PRICES.version}+overrides"
    rates = prices.rates("claude-sonnet-5-5")
    assert rates is not None and rates.output == 5.0
    list_rates = PRICES.rates("claude-sonnet-5-5")
    assert list_rates is not None and list_rates.output == 10.0  # list prices untouched


def test_the_last_known_good_config_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    ConfigState(PRICES, unconfigured_mode="passthrough", path=path).apply(config(3))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    restarted = ConfigState(PRICES, unconfigured_mode="passthrough", path=path)
    assert restarted.load_from_disk()
    assert (restarted.current.version, restarted.current.mode) == (3, "observer")


@pytest.mark.parametrize("content", [b"", b"{not json", b'{"version": 0}'])
def test_an_unreadable_persisted_config_is_no_config(tmp_path: Path, content: bytes) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(content)
    state = ConfigState(PRICES, unconfigured_mode="passthrough", path=path)
    assert not state.load_from_disk()
    assert state.current.version is None


def test_upstream_health_follows_the_exchanges_that_reached_it() -> None:
    def ended(outcome: Outcome, status: int | None) -> Observation:
        return dataclasses.replace(observation(), outcome=outcome, status=status)

    health = UpstreamHealth()
    assert health.reachable is None
    health.observe(ended("transport_error", None))
    assert health.reachable is False
    health.observe(ended("rejected", 401))  # refused by the gateway: never reached the upstream
    assert health.reachable is False
    health.observe(ended("complete", 529))  # an answer, even an error, means it is reachable
    assert health.reachable is True


# ── the control plane's config and heartbeat ─────────────────────────────────


class FakeControlPlane:
    """``GET /gateway/config`` (version as ETag; ``404`` before any config) and
    ``POST /gateway/heartbeat``, as the control plane serves them to a gateway."""

    def __init__(self) -> None:
        self.config: GatewayConfig | None = None
        self.beats: list[GatewayHeartbeat] = []
        self.pulls: list[tuple[str | None, str | None]] = []
        self.status = 200

    def app(self) -> Starlette:
        def denied(request: Request) -> Response | None:
            if self.status != 200:
                return Response(status_code=self.status)
            if request.headers.get("authorization") != f"Bearer {SERVICE_KEY}":
                return Response(status_code=401)
            return None

        async def get_config(request: Request) -> Response:
            self.pulls.append(
                (request.headers.get("authorization"), request.headers.get("if-none-match"))
            )
            if (refusal := denied(request)) is not None:
                return refusal
            if self.config is None:
                return JSONResponse({"detail": "no config yet"}, status_code=404)
            etag = f'"{self.config.version}"'
            if request.headers.get("if-none-match") == etag:
                return Response(status_code=304, headers={"etag": etag})
            return JSONResponse(self.config.model_dump(mode="json"), headers={"etag": etag})

        async def heartbeat(request: Request) -> Response:
            if (refusal := denied(request)) is not None:
                return refusal
            self.beats.append(GatewayHeartbeat.model_validate_json(await request.body()))
            ack = GatewayHeartbeatAck(config_version=self.config.version if self.config else None)
            return JSONResponse(ack.model_dump(mode="json"))

        return Starlette(
            routes=[
                Route("/gateway/config", get_config, methods=["GET"]),
                Route("/gateway/heartbeat", heartbeat, methods=["POST"]),
            ]
        )


@pytest.fixture
def control_plane() -> Iterator[tuple[FakeControlPlane, str]]:
    fake = FakeControlPlane()
    with serve(fake.app()) as url:
        yield fake, url


async def test_the_control_plane_source_pulls_with_the_version_as_etag(
    control_plane: tuple[FakeControlPlane, str],
) -> None:
    fake, url = control_plane
    async with httpx.AsyncClient() as client:
        source = ControlPlaneConfigSource(client, url, SERVICE_KEY)
        assert await source.fetch(None) is None  # nothing saved yet
        fake.config = config(4)
        assert await source.fetch(None) == config(4)
        assert await source.fetch(4) is None  # unchanged: 304
        assert await source.heartbeat(beat()) == GatewayHeartbeatAck(config_version=4)
        fake.status = 500
        with pytest.raises(httpx.HTTPStatusError):
            await source.fetch(4)
        with pytest.raises(httpx.HTTPStatusError):
            await source.heartbeat(beat())
    assert fake.pulls[:3] == [
        (f"Bearer {SERVICE_KEY}", None),
        (f"Bearer {SERVICE_KEY}", None),
        (f"Bearer {SERVICE_KEY}", '"4"'),
    ]
    assert fake.beats == [beat()]


class CountingSource(StaticConfigSource):
    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        heartbeat_error: Exception | None = None,
        fetch_error: Exception | None = None,
    ) -> None:
        super().__init__(config)
        self.fetches = 0
        self._heartbeat_error = heartbeat_error
        self._fetch_error = fetch_error

    async def fetch(self, since_version: int | None) -> GatewayConfig | None:
        self.fetches += 1
        if (error := self._fetch_error) is not None:
            self._fetch_error = None  # fails once
            raise error
        return await super().fetch(since_version)

    async def heartbeat(self, beat: GatewayHeartbeat) -> GatewayHeartbeatAck:
        if (error := self._heartbeat_error) is not None:
            self._heartbeat_error = None  # fails once
            raise error
        return await super().heartbeat(beat)


async def _beat() -> GatewayHeartbeat:
    return beat()


async def test_a_heartbeat_naming_the_applied_version_costs_no_pull() -> None:
    state = ConfigState(PRICES, unconfigured_mode="passthrough")
    state.apply(config(2), persist=False)
    source = CountingSource(config(2))
    await sync_config_once(state, source, _beat)
    assert source.fetches == 0
    assert len(source.beats) == 1


async def test_a_new_version_named_by_the_heartbeat_is_pulled_and_applied() -> None:
    state = ConfigState(PRICES, unconfigured_mode="passthrough")
    state.apply(config(2), persist=False)
    source = CountingSource(config(3, "passthrough"))
    await sync_config_once(state, source, _beat)
    assert source.fetches == 1
    assert (state.current.version, state.current.mode) == (3, "passthrough")


async def test_a_failed_heartbeat_still_pulls() -> None:
    state = ConfigState(PRICES, unconfigured_mode="passthrough")
    source = CountingSource(config(1), heartbeat_error=httpx.ConnectError("no heartbeat route"))
    await sync_config_once(state, source, _beat)
    assert state.current.version == 1


@pytest.mark.parametrize(
    "error",
    [httpx.ConnectError("down"), httpx.ReadTimeout("slow"), ValueError("not a config")],
)
async def test_a_failed_pull_keeps_the_config_in_effect(error: Exception) -> None:
    state = ConfigState(PRICES, unconfigured_mode="passthrough")
    state.apply(config(2), persist=False)
    source = CountingSource(config(3), fetch_error=error)
    await sync_config_once(state, source, _beat)
    assert state.current.version == 2


async def test_an_unexpected_error_never_ends_the_sync() -> None:
    state = ConfigState(PRICES, unconfigured_mode="passthrough")
    source = CountingSource(config(1), heartbeat_error=RuntimeError("a bug"))
    loop = asyncio.create_task(sync_config_forever(state, source, _beat, 0.001))
    try:
        await until(lambda: state.current.version == 1, "the sync to carry on")
    finally:
        loop.cancel()
    assert len(source.beats) >= 1  # a later beat got through


# ── through the gateway ──────────────────────────────────────────────────────


def _settings(
    upstream: str, cp_url: str, tmp_path: Path, *, auth_mode: AuthMode = "personal"
) -> GatewaySettings:
    return GatewaySettings(
        upstream_url=upstream,
        control_plane_url=cp_url,
        service_key=SERVICE_KEY,
        gateway_id="gw-test",
        state_dir=tmp_path,
        heartbeat_seconds=1,
        auth_mode=auth_mode,
    )


def test_attached_and_never_configured_records_nothing(
    tmp_path: Path, control_plane: tuple[FakeControlPlane, str]
) -> None:
    _, cp_url = control_plane
    seen: list[Observation] = []
    with fake_anthropic() as upstream:
        app = create_app(_settings(upstream.base_url, cp_url, tmp_path), observe=seen.append)
        with serve(app) as url:
            status = readyz(url)
            assert (status["mode"], status["configVersion"]) == ("passthrough", None)
            send(url, load("subagent-explore-sonnet-1"))
            wait_for(lambda: bool(seen), "the observation")
    assert seen[0].mode == "passthrough"
    assert seen[0].decision.reasons == ["mode_passthrough"]
    assert spool_records(tmp_path / "requests") == []


def test_config_changes_apply_live(
    tmp_path: Path, control_plane: tuple[FakeControlPlane, str]
) -> None:
    fake, cp_url = control_plane
    exchange = load("subagent-explore-sonnet-1")
    fake.config = config(1)
    with fake_anthropic() as upstream:
        with serve(create_app(_settings(upstream.base_url, cp_url, tmp_path))) as url:
            wait_for(lambda: readyz(url)["configVersion"] == 1, "version 1")
            send(url, exchange)
            fake.config = config(2, pricing_overrides={"claude-sonnet-5-5": SONNET_HALF})
            wait_for(lambda: readyz(url)["configVersion"] == 2, "version 2")
            send(url, exchange)
            fake.config = config(3, "passthrough")
            wait_for(lambda: readyz(url)["mode"] == "passthrough", "version 3")
            send(url, exchange)
    first, second = spool_records(tmp_path / "requests")  # passthrough recorded nothing
    assert (first.config_version, second.config_version) == (1, 2)
    listed = PriceBook.packaged().version
    assert (first.pricing_version, second.pricing_version) == (listed, f"{listed}+overrides")
    assert first.cost_usd is not None and second.cost_usd is not None
    assert second.cost_usd == pytest.approx(first.cost_usd / 2)
    assert fake.beats[0].config_version is None
    assert {b.config_version for b in fake.beats} >= {1, 2}
    # Each version was pulled once; every other beat named the version already applied.
    assert [etag for _, etag in fake.pulls] == [None, '"1"', '"2"']


def test_a_restart_with_the_control_plane_down_keeps_the_last_known_good(
    tmp_path: Path, control_plane: tuple[FakeControlPlane, str]
) -> None:
    fake, cp_url = control_plane
    fake.config = config(5)
    with fake_anthropic() as upstream:
        with serve(create_app(_settings(upstream.base_url, cp_url, tmp_path))) as url:
            wait_for(lambda: readyz(url)["configVersion"] == 5, "version 5")
        down = _settings(upstream.base_url, "http://127.0.0.1:1", tmp_path)
        with serve(create_app(down)) as url:
            status = readyz(url)
            assert (status["mode"], status["configVersion"]) == ("observer", 5)
            send(url, load("subagent-explore-sonnet-1"))
    [record] = spool_records(tmp_path / "requests")
    assert record.config_version == 5


def test_the_heartbeat_reports_the_gateway(tmp_path: Path) -> None:
    source = StaticConfigSource(config(1))
    headers = {"x-theygent-gateway-key": "tyg_seat_developer_key"}
    with fake_anthropic() as upstream:
        settings = _settings(upstream.base_url, "http://127.0.0.1:1", tmp_path, auth_mode="team")
        app = create_app(
            settings,
            key_source=StaticKeySource(team_keys()),
            config_source=source,
            # The control plane refuses every batch, so the records wait in the spool.
            event_sink=FakeSink([ExportRefused(503)] * 1000),
        )
        with serve(app) as url:
            wait_for(lambda: readyz(url)["ready"] is True, "keys")
            for _ in range(2):
                exchange = load("subagent-explore-sonnet-1")
                httpx.request(
                    exchange.method,
                    f"{url}{exchange.target}",
                    content=exchange.request_bytes(),
                    headers=[
                        *exchange.request_headers,
                        *headers.items(),
                        ("x-fake-fixture", exchange.name),
                    ],
                    timeout=WAIT_SEC,
                )
            wait_for(lambda: bool(source.beats) and source.beats[-1].spool_backlog == 2, "a beat")
    last = source.beats[-1]
    assert (last.gateway_id, last.mode, last.config_version) == ("gw-test", "observer", 1)
    assert last.software_version == "0.1.0"
    assert last.key_set_version == 1
    assert last.upstream_reachable is True
    assert last.started_at.tzinfo is not None
    assert {b.started_at for b in source.beats} == {last.started_at}
