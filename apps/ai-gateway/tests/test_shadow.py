"""Shadow runs: after an exchange whose decision routed it somewhere it did not go, a copy of the
request goes to that target, to measure what the counterfactual only estimates.

What must hold:

* the real exchange is never delayed or altered: Claude Code has its whole answer before the
  shadow starts, and the bytes it sent and received are those of an exchange without one;
* the record says what the shadow found — outcome, usage, cost, time — and is delivered when the
  shadow ends, or without it when the shadow is cut short;
* local shadows run by default and cost nothing; a cloud shadow is real spend, so it is off unless
  chosen, never runs on a subscription seat or a team developer's own key, stops at the day's cap,
  and pauses when the target shows rate-limit pressure;
* an applied route, a stay, or a failed exchange is never shadowed;
* a local model serves real traffic first: a request it serves cancels a running local shadow, and
  no local shadow starts while it serves one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _fake_inference import FakeInference, fake_inference
from _fixtures import Exchange, load
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.identity import StaticKeySource, token_digest
from theygent_ai_gateway.observe import Observation
from theygent_ai_gateway.requestlog import to_record
from theygent_ai_gateway.routing import with_model
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.shadow import Shadows, _under_pressure
from theygent_ai_gateway.signals import read_request
from theygent_ir.gateway import (
    FingerprintRule,
    GatewayConfig,
    GatewayKey,
    GatewayKeySet,
    GatewayMode,
    LocalModel,
    LocalRouting,
    LocalShare,
    PolicyConfig,
    Target,
)

PRICES = PriceBook.packaged()
OPUS, SONNET, LOCAL = "claude-opus-5-5", "claude-sonnet-5-5", "aux-local"
API_KEY = "sk-ant-api03-developer-own-key"
ORG_API_KEY = "sk-ant-api03-organization-key"
MAIN, SPAWN, FOLLOW = load("main-explore-1"), load("subagent-explore-1"), load("subagent-explore-2")
TITLE = load("auxiliary-session-title")
WAIT_SEC = 10


def with_api_key(exchange: Exchange, key: str = API_KEY) -> Exchange:
    """The exchange as sent with an API key instead of a subscription seat's login."""
    headers = tuple(h for h in exchange.request_headers if h[0] != "authorization")
    return replace(exchange, request_headers=(*headers, ("x-api-key", key)))


def title_rule() -> FingerprintRule:
    headers = dict(TITLE.request_headers)
    signals = read_request(headers, TITLE.request_bytes(), has_messages_body=True)
    assert signals.body is not None and signals.body.fingerprint is not None
    return FingerprintRule(fingerprint=signals.body.fingerprint, status="allow_local")


def config(mode: GatewayMode = "observer", /, **policy: Any) -> GatewayConfig:
    return GatewayConfig(
        version=1,
        mode=mode,
        policy=PolicyConfig(subagent_target=SONNET, local_target=LOCAL, **policy),
        fingerprints=[title_rule()],
        updated_at=datetime(2026, 10, 11, tzinfo=UTC),
    )


CLOUD = {"shadow_cloud": True, "shadow_cloud_daily_usd": 50.0}


@dataclass
class Gateway:
    url: str
    upstream: FakeAnthropic
    inference: FakeInference
    seen: list[Observation]
    shadows: Shadows
    headers: tuple[tuple[str, str], ...] = ()

    def send(self, exchange: Exchange, *, wait: bool = True, **steer: str) -> httpx.Response:
        count = len(self.seen)
        response = httpx.post(
            f"{self.url}{exchange.target}",
            content=exchange.request_bytes(),
            headers=[
                *exchange.request_headers,
                *self.headers,
                ("x-fake-fixture", exchange.name),
                *((k.replace("_", "-"), v) for k, v in steer.items()),
            ],
            timeout=WAIT_SEC,
        )
        if wait:
            self.wait_for(count + 1)
        return response

    def wait_for(self, count: int) -> Observation:
        deadline = time.monotonic() + WAIT_SEC
        while len(self.seen) < count:
            assert time.monotonic() < deadline, "no observation"
            time.sleep(0.01)
        return self.seen[-1]

    def by_class(self, request_class: str) -> list[Observation]:
        return [o for o in self.seen if o.request.hints.request_class == request_class]

    def shadow_requests(self) -> list[bytes]:
        return [r.body for r in self.upstream.requests if f'"model":"{SONNET}"'.encode() in r.body]


@contextmanager
def gateway(tmp_path: Path, gateway_config: GatewayConfig, **settings: Any) -> Iterator[Gateway]:
    seen: list[Observation] = []
    with fake_anthropic() as upstream, fake_inference() as inference:
        app = create_app(
            GatewaySettings(
                upstream_url=upstream.base_url,
                inference_plane_url=inference.base_url,
                gateway_id="gw",
                state_dir=tmp_path,
                **settings,
            ),
            observe=seen.append,
            config=gateway_config,
            key_source=StaticKeySource(TEAM_KEYS) if settings.get("auth_mode") == "team" else None,
        )
        with serve(app) as url:
            deadline = time.monotonic() + WAIT_SEC
            while httpx.get(f"{url}/readyz").status_code != 200:
                assert time.monotonic() < deadline, "the gateway never got ready"
                time.sleep(0.02)
            yield Gateway(url, upstream, inference, seen, app.state.shadows)


# ── cloud shadows ────────────────────────────────────────────────────────────


def test_a_cloud_shadow_measures_the_target_after_the_exchange(tmp_path: Path) -> None:
    spawn = with_api_key(SPAWN)
    with gateway(tmp_path, config(**CLOUD)) as gw:
        gw.send(with_api_key(MAIN))
        answer = gw.send(spawn, x_fake_output_model=f"{SONNET}=40")
    observed = gw.seen[-1]
    # The real exchange: the original bytes, answered as without a shadow.
    original, shadow = gw.upstream.requests[1], gw.upstream.requests[2]
    assert original.body == spawn.request_bytes()
    assert answer.content == spawn.response_bytes()
    # The shadow: the routed request a router would have sent, on the same credential.
    assert shadow.body == with_model(spawn.request_bytes(), SONNET)
    assert shadow.header("x-api-key") == [API_KEY]
    assert observed.decision.action == "route" and not observed.decision.applied
    run = observed.shadow
    assert run is not None
    assert (run.target, run.outcome) == (Target(kind="anthropic", model=SONNET), "answered")
    assert run.usage.output_tokens == 40 != observed.response.usage.output_tokens
    assert run.cost_usd == pytest.approx(PRICES.cost(SONNET, run.usage))
    assert run.total_ms is not None and run.first_output_ms is not None
    # The target answered the replay with the same first tool: the same next action.
    assert run.agreed is True
    assert gw.shadows.spent_today == pytest.approx(run.cost_usd)
    # The record carries it to the control plane.
    record = to_record(observed, record_id="greq_shadow", gateway_id="gw")
    assert record.shadow == run


def test_a_shadow_never_delays_the_exchange(tmp_path: Path) -> None:
    with gateway(tmp_path, config(**CLOUD)) as gw:
        gw.send(with_api_key(MAIN))
        started = time.monotonic()
        answer = gw.send(
            with_api_key(SPAWN), wait=False, x_fake_slow_model=SONNET, x_fake_slow_seconds="3"
        )
        answered_in = time.monotonic() - started
        assert gw.upstream.slow_started.wait(WAIT_SEC)  # the shadow is running…
        assert len(gw.seen) == 1  # …and holds the record, not the answer
        observed = gw.wait_for(2)
    assert answer.content == SPAWN.response_bytes()
    assert answered_in < 2
    assert observed.shadow is not None and observed.shadow.outcome == "answered"
    assert observed.timing.total_ms is not None and observed.timing.total_ms < 2000


def test_cloud_shadows_are_off_by_default(tmp_path: Path) -> None:
    with gateway(tmp_path, config()) as gw:
        gw.send(with_api_key(MAIN))
        gw.send(with_api_key(SPAWN))
    assert gw.shadow_requests() == [] and gw.seen[-1].shadow is None


def test_a_seat_is_never_shadowed(tmp_path: Path) -> None:
    # A subscription seat's plan allowance is not the gateway's to spend.
    with gateway(tmp_path, config(**CLOUD)) as gw:
        gw.send(MAIN)
        gw.send(SPAWN)
    assert gw.shadow_requests() == [] and gw.seen[-1].shadow is None


SEAT_DEV, ORG_DEV = "tyg_seat_developer_key_0001", "tyg_org_developer_key_0002"
TEAM_KEYS = GatewayKeySet(
    version=1,
    generated_at=datetime(2026, 10, 11, tzinfo=UTC),
    keys=[
        GatewayKey(
            key_id="key_own",
            token_sha256=token_digest(SEAT_DEV),
            developer_id="usr_own",
            credential_profile="passthrough",
        ),
        GatewayKey(
            key_id="key_org",
            token_sha256=token_digest(ORG_DEV),
            developer_id="usr_org",
            credential_profile="org_key",
        ),
    ],
)
TEAM = {
    "auth_mode": "team",
    "control_plane_url": "http://127.0.0.1:1",
    "service_key": "tygs_service_key",
    "org_api_key": ORG_API_KEY,
}


def test_a_team_developers_own_key_is_never_shadowed(tmp_path: Path) -> None:
    with gateway(tmp_path, config(**CLOUD), **TEAM) as gw:
        gw.headers = (("x-theygent-gateway-key", SEAT_DEV),)
        gw.send(with_api_key(MAIN))
        gw.send(with_api_key(SPAWN))
    assert gw.shadow_requests() == [] and gw.seen[-1].shadow is None


def test_the_organizations_key_is_shadowed_with_that_key(tmp_path: Path) -> None:
    with gateway(tmp_path, config(**CLOUD), **TEAM) as gw:
        gw.headers = (("x-theygent-gateway-key", ORG_DEV),)
        gw.send(MAIN)
        gw.send(SPAWN)
    [shadow] = [r for r in gw.upstream.requests if f'"model":"{SONNET}"'.encode() in r.body]
    assert shadow.header("x-api-key") == [ORG_API_KEY]
    assert shadow.header("authorization") == []  # the developer's login never goes with it
    assert gw.seen[-1].shadow is not None


def test_the_daily_cap_stops_cloud_shadows(tmp_path: Path) -> None:
    capped = config(shadow_cloud=True, shadow_cloud_daily_usd=0.0001)
    with gateway(tmp_path, capped) as gw:
        gw.send(with_api_key(MAIN))
        gw.send(with_api_key(SPAWN))
        first = gw.seen[-1].shadow
        gw.send(with_api_key(FOLLOW))
    assert first is not None and (first.cost_usd or 0) > 0.0001  # the first one ran
    assert gw.seen[-1].shadow is None and len(gw.shadow_requests()) == 1


def test_a_cap_of_zero_runs_none(tmp_path: Path) -> None:
    with gateway(tmp_path, config(shadow_cloud=True, shadow_cloud_daily_usd=0.0)) as gw:
        gw.send(with_api_key(MAIN))
        gw.send(with_api_key(SPAWN))
    assert gw.shadow_requests() == []


def test_the_cap_starts_again_the_next_day() -> None:
    day = [date(2026, 10, 11)]
    shadows = Shadows(today=lambda: day[0])
    shadows._spent = 5.0
    assert shadows.spent_today == 5.0
    day[0] = date(2026, 10, 12)
    assert shadows.spent_today == 0.0


def test_rate_limit_pressure_pauses_cloud_shadows(tmp_path: Path) -> None:
    with gateway(tmp_path, config(**CLOUD)) as gw:
        gw.send(with_api_key(MAIN))
        gw.send(with_api_key(SPAWN), x_fake_pressure_model=SONNET)
        pressured = gw.seen[-1].shadow
        gw.send(with_api_key(FOLLOW))
    assert pressured is not None and pressured.outcome == "answered"
    assert gw.seen[-1].shadow is None and len(gw.shadow_requests()) == 1


@pytest.mark.parametrize(
    ("status", "outcome", "pauses"),
    [(400, "unusable", False), (422, "unusable", False), (529, "failed", True)],
)
def test_a_refused_shadow(tmp_path: Path, status: int, outcome: str, pauses: bool) -> None:
    with gateway(tmp_path, config(**CLOUD)) as gw:
        gw.send(with_api_key(MAIN))
        answer = gw.send(
            with_api_key(SPAWN), x_fake_refuse_model=SONNET, x_fake_refuse_status=str(status)
        )
        refused = gw.seen[-1].shadow
        gw.send(with_api_key(FOLLOW), x_fake_refuse_model=SONNET)
    assert answer.status_code == 200  # the real exchange never knew
    assert refused is not None and refused.outcome == outcome
    assert (gw.seen[-1].shadow is None) is pauses


def test_a_shadow_cut_short_still_delivers_its_record(tmp_path: Path) -> None:
    with gateway(tmp_path, config(**CLOUD)) as gw:
        gw.send(with_api_key(MAIN))
        gw.send(with_api_key(SPAWN), wait=False, x_fake_slow_model=SONNET, x_fake_slow_seconds="30")
        assert gw.upstream.slow_started.wait(WAIT_SEC)
        assert len(gw.seen) == 1
    # The gateway stopped: the shadow was cancelled, and the exchange's record delivered.
    assert len(gw.seen) == 2
    assert gw.seen[-1].request.hints.request_class == "subagent" and gw.seen[-1].shadow is None


def test_an_applied_route_is_never_shadowed(tmp_path: Path) -> None:
    with gateway(tmp_path, config("router", **CLOUD)) as gw:
        gw.send(with_api_key(MAIN))
        gw.send(with_api_key(SPAWN))
    observed = gw.seen[-1]
    assert observed.decision.applied and observed.shadow is None
    assert len(gw.upstream.requests) == 2


def test_a_stay_and_a_failed_exchange_are_never_shadowed(tmp_path: Path) -> None:
    with gateway(tmp_path, config(**CLOUD)) as gw:
        gw.send(with_api_key(MAIN))  # the main conversation stays (no main target)
        gw.send(with_api_key(SPAWN), x_fake_refuse_model=OPUS)  # the real request failed
    assert [o.shadow for o in gw.seen] == [None, None]
    assert gw.shadow_requests() == []


@pytest.mark.parametrize(
    ("headers", "pressure"),
    [
        ({}, False),
        (
            {
                "anthropic-ratelimit-tokens-limit": "100",
                "anthropic-ratelimit-tokens-remaining": "60",
            },
            False,
        ),
        (
            {
                "anthropic-ratelimit-tokens-limit": "100",
                "anthropic-ratelimit-tokens-remaining": "40",
            },
            True,
        ),
        (
            {
                "anthropic-ratelimit-requests-limit": "50",
                "anthropic-ratelimit-requests-remaining": "x",
            },
            False,
        ),
        (
            {
                "anthropic-ratelimit-requests-limit": "0",
                "anthropic-ratelimit-requests-remaining": "0",
            },
            False,
        ),
    ],
)
def test_what_counts_as_pressure(headers: dict[str, str], pressure: bool) -> None:
    assert _under_pressure(httpx.Response(200, headers=headers)) is pressure
    assert _under_pressure(httpx.Response(429)) and _under_pressure(httpx.Response(529))


# ── local shadows ────────────────────────────────────────────────────────────


def test_a_local_shadow_says_whether_the_local_answer_would_be_used(tmp_path: Path) -> None:
    with gateway(tmp_path, config()) as gw:
        answer = gw.send(TITLE)
    assert answer.content == TITLE.response_bytes()  # the Anthropic API answered Claude Code
    [asked] = gw.inference.requests
    assert asked["model"] == LOCAL
    run = gw.seen[-1].shadow
    assert run is not None
    assert (run.target, run.outcome, run.cost_usd) == (
        Target(kind="local", model=LOCAL),
        "answered",
        0.0,
    )
    assert (run.usage.input_tokens, run.usage.output_tokens) == (812, 9)
    assert run.agreed is None  # a side request offers no tools: no next action to compare


@pytest.mark.parametrize(
    ("content", "status", "outcome"),
    [("not json", 200, "unusable"), ('{"name": "x"}', 200, "unusable"), (None, 500, "failed")],
)
def test_a_local_answer_that_would_fall_back(
    tmp_path: Path, content: str | None, status: int, outcome: str
) -> None:
    with gateway(tmp_path, config()) as gw:
        gw.inference.content, gw.inference.status = content, status
        gw.send(TITLE)
    run = gw.seen[-1].shadow
    assert run is not None and run.outcome == outcome


def test_one_local_shadow_at_a_time(tmp_path: Path) -> None:
    with gateway(tmp_path, config()) as gw:
        gw.inference.delay = 1.5
        gw.send(TITLE, wait=False)
        assert gw.inference.asked.wait(WAIT_SEC)
        gw.send(TITLE)  # skipped: the local model is busy with the first shadow
        skipped = gw.seen[-1]
        first = gw.wait_for(2)
    assert skipped.shadow is None
    assert first.shadow is not None and len(gw.inference.requests) == 1


def test_local_shadows_can_be_turned_off(tmp_path: Path) -> None:
    with gateway(tmp_path, config(shadow_local=False)) as gw:
        gw.send(TITLE)
    assert gw.inference.requests == [] and gw.seen[-1].shadow is None


# ── a local model serves real traffic first ──────────────────────────────────

#: Router mode: the session title is served locally; every subagent is a local trial.
TRIAL = {
    "local_models": [LocalModel(model=LOCAL, context_tokens=200_000, tools=True)],
    "local_routing": LocalRouting(subagents=LocalShare(trial=1.0)),
}


def in_background(gw: Gateway, exchange: Exchange) -> threading.Thread:
    thread = threading.Thread(target=gw.send, args=(exchange,), kwargs={"wait": False})
    thread.start()
    return thread


def test_a_request_served_locally_cancels_a_running_trial(tmp_path: Path) -> None:
    with gateway(tmp_path, config("router", **TRIAL)) as gw:
        gw.send(MAIN)
        gw.inference.delay = 4
        gw.send(SPAWN, wait=False)
        assert gw.inference.asked.wait(WAIT_SEC)  # the trial is running…
        assert len(gw.seen) == 1  # …and holds the subagent's record
        started = time.monotonic()
        title = in_background(gw, TITLE)
        tried = gw.wait_for(2)
        made_way_in = time.monotonic() - started
        title.join(WAIT_SEC)
        served = gw.wait_for(3)
    assert tried.request.hints.request_class == "subagent" and tried.shadow is None
    assert made_way_in < 2  # cancelled, not waited for
    assert served.decision.applied and served.decision.target == Target(kind="local", model=LOCAL)
    assert len(gw.inference.requests) == 2


def test_no_trial_starts_while_a_local_model_serves(tmp_path: Path) -> None:
    with gateway(tmp_path, config("router", **TRIAL)) as gw:
        gw.send(MAIN)
        gw.inference.delay = 2
        title = in_background(gw, TITLE)
        assert gw.inference.asked.wait(WAIT_SEC)
        gw.send(SPAWN)  # Anthropic answers; the local model is busy serving the title
        skipped = gw.seen[-1]
        title.join(WAIT_SEC)
        gw.wait_for(3)
        gw.inference.delay = 0
        gw.send(FOLLOW)
        tried = gw.seen[-1]
    assert skipped.request.hints.request_class == "subagent" and skipped.shadow is None
    assert tried.shadow is not None and tried.shadow.trial


def test_the_decisions_own_shadow_comes_before_a_trial(tmp_path: Path) -> None:
    # Observer mode, cloud shadows on: the subagent's decision names the cheaper target, and its
    # shadow measures that; the local trial waits for an exchange without one.
    with gateway(tmp_path, config(**CLOUD, **TRIAL)) as gw:
        gw.send(with_api_key(MAIN))
        gw.send(with_api_key(SPAWN))
    run = gw.seen[-1].shadow
    assert run is not None and not run.trial
    assert run.target == Target(kind="anthropic", model=SONNET)
    assert gw.inference.requests == []
