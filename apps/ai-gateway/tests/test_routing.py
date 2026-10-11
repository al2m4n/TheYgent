"""Router mode: the policy's decisions, applied.

What must hold:

* a routed request differs from the original in its top-level ``model`` value only — every other
  byte arrives as Claude Code sent it — and a body that cannot be spliced unambiguously is not
  routed at all;
* the target answers it, and the record says so: applied, served by the target, priced there, with
  the requested model's cost as its baseline;
* a target that refuses before answering is never seen by Claude Code: the original bytes go to the
  requested model, whose answer Claude Code gets, and the conversation stays there until its next
  cold boundary; a credential refusal is not retried;
* everything a route does not apply to — never-routed traffic, local targets — arrives unchanged.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _fixtures import Exchange, all_exchanges, load
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.observe import Observation
from theygent_ai_gateway.routing import with_model
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.signals import read_request
from theygent_ir.gateway import FingerprintRule, GatewayConfig, PolicyConfig

PRICES = PriceBook.packaged()
SONNET = "claude-sonnet-5-5"


# ── the splice ───────────────────────────────────────────────────────────────


def test_only_the_model_value_changes() -> None:
    original = load("subagent-explore-1").request_bytes()
    routed = with_model(original, SONNET)
    assert routed is not None
    before, after = original.split(b'"claude-opus-5-5"', 1)
    assert routed == before + b'"claude-sonnet-5-5"' + after
    expected = json.loads(original)
    expected["model"] = SONNET
    assert json.loads(routed) == expected


def test_spacing_escapes_and_key_order_survive() -> None:
    original = (
        b'{ "system" : "caf\\u00e9 \xc3\xbc",\n  "messages":[{"role":"user","content":'
        b'"say \\"model\\": x","model":"nested"}],  "model" :  "claude-opus-5-5" ,"max_tokens":9}'
    )
    routed = with_model(original, SONNET)
    assert routed == original.replace(b'"claude-opus-5-5"', b'"claude-sonnet-5-5"')


@pytest.mark.parametrize(
    "body",
    [
        b'{"model":"a","model":"b"}',  # which one would the upstream read?
        b'{"model":7}',
        b'{"messages":[]}',
        b"{}",
        b'[{"model":"a"}]',
        b'{"model":"a"} trailing',
        b'{"model":"a",}',
        b'{"model": "\xff"}',
        b"",
        b"not json",
    ],
)
def test_an_ambiguous_body_is_not_routed(body: bytes) -> None:
    assert with_model(body, SONNET) is None


# ── a gateway in router mode ─────────────────────────────────────────────────


def router_config(
    *, local: str | None = None, rules: dict[str, str] | None = None
) -> GatewayConfig:
    return GatewayConfig(
        version=1,
        mode="router",
        policy=PolicyConfig(subagent_target=SONNET, main_target=SONNET, local_target=local),
        fingerprints=[
            FingerprintRule(fingerprint=f, status=s)  # type: ignore[arg-type]
            for f, s in (rules or {}).items()
        ],
        updated_at=datetime(2026, 10, 8, tzinfo=UTC),
    )


class Router:
    def __init__(self, upstream: FakeAnthropic, url: str, seen: list[Observation]) -> None:
        self.upstream, self.url, self.seen = upstream, url, seen

    def send(self, exchange: Exchange, **headers: str) -> httpx.Response:
        count = len(self.seen)
        response = httpx.request(
            exchange.method,
            f"{self.url}{exchange.target}",
            content=exchange.request_bytes(),
            headers=[
                *exchange.request_headers,
                ("x-fake-fixture", exchange.name),
                *((k.replace("_", "-"), v) for k, v in headers.items()),
            ],
            timeout=10,
        )
        deadline = time.monotonic() + 10
        while len(self.seen) <= count:
            assert time.monotonic() < deadline, "no observation"
            time.sleep(0.01)
        return response

    def last(self) -> Observation:
        return self.seen[-1]


@contextmanager
def router(tmp_path: Path, config: GatewayConfig | None = None) -> Iterator[Router]:
    seen: list[Observation] = []
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw", state_dir=tmp_path
        )
        app = create_app(settings, observe=seen.append, config=config or router_config())
        with serve(app) as url:
            yield Router(upstream, url, seen)


def test_a_subagent_is_served_by_the_target_and_recorded_so(tmp_path: Path) -> None:
    with router(tmp_path) as gw:
        gw.send(load("main-explore-1"))
        main = gw.last()
        spawn = load("subagent-explore-1")
        answer = gw.send(spawn)
        observed = gw.last()
    sent = gw.upstream.requests
    # The main conversation's first request moves too (a main target is set, its cache is cold).
    assert main.decision.applied and json.loads(sent[0].body)["model"] == SONNET
    assert sent[1].body == with_model(spawn.request_bytes(), SONNET)
    assert answer.status_code == 200 and SONNET.encode() in answer.content
    decision = observed.decision
    assert (decision.action, decision.applied, decision.counterfactual_usd) == ("route", True, None)
    assert decision.reasons == ["subagent_at_spawn"]
    assert observed.response.model == SONNET
    usage = observed.response.usage
    assert observed.cost_usd == pytest.approx(PRICES.cost(SONNET, usage))
    assert observed.baseline_usd == pytest.approx(PRICES.cost("claude-opus-5-5", usage))
    assert observed.baseline_usd > observed.cost_usd


def test_a_refusal_goes_to_the_requested_model_and_the_conversation_stays_there(
    tmp_path: Path,
) -> None:
    with router(tmp_path) as gw:
        gw.send(load("main-explore-1"))
        spawn = load("subagent-explore-1")
        answer = gw.send(spawn, x_fake_refuse_model=SONNET)
        refused = gw.last()
        follow = load("subagent-explore-2")
        gw.send(follow, x_fake_refuse_model=SONNET)
        pinned = gw.last()
    bodies = [r.body for r in gw.upstream.requests]
    # The routed attempt, then the original bytes; Claude Code saw only the original's answer.
    assert bodies[1:3] == [with_model(spawn.request_bytes(), SONNET), spawn.request_bytes()]
    assert answer.status_code == 200 and b"refuses" not in answer.content
    assert refused.decision.reasons == ["target_refused", "subagent_at_spawn"]
    assert (refused.decision.action, refused.decision.applied) == ("stay", False)
    assert refused.response.model == "claude-opus-5-5" and refused.baseline_usd is None
    # The tool loop that follows stays on the requested model: no second attempt at the target.
    assert bodies[3] == follow.request_bytes() and len(bodies) == 4
    assert pinned.decision.reasons == ["mid_conversation", "pinned_after_refusal"]


@pytest.mark.parametrize("status", [401, 403])
def test_a_credential_refusal_is_not_retried(tmp_path: Path, status: int) -> None:
    with router(tmp_path) as gw:
        gw.send(load("main-explore-1"))
        answer = gw.send(
            load("subagent-explore-1"),
            x_fake_refuse_model=SONNET,
            x_fake_refuse_status=str(status),
        )
    assert answer.status_code == status
    assert len(gw.upstream.requests) == 2  # the main request, and the one refused attempt


def test_only_applied_routes_change_a_request(tmp_path: Path) -> None:
    # Every recorded exchange, in order, through a router whose config routes all it can: each
    # request reaches the upstream byte-identical, or with only its model replaced — exactly when
    # the decision says it was applied. Never-routed traffic and local targets go unchanged.
    title = load("auxiliary-session-title")
    headers = dict(title.request_headers)
    signals = read_request(headers, title.request_bytes(), has_messages_body=True)
    assert signals.body is not None and signals.body.fingerprint is not None
    rules = {signals.body.fingerprint: "allow_local"}
    replayed = [e for e in all_exchanges() if e.abort_after_events is None]
    with router(tmp_path, router_config(local="aux-local", rules=rules)) as gw:
        for exchange in replayed:
            gw.send(exchange)
    received = {r.header("x-fake-fixture")[0]: r.body for r in gw.upstream.requests}
    by_name = dict(zip((e.name for e in replayed), gw.seen, strict=True))
    applied = 0
    for exchange in replayed:
        decision = by_name[exchange.name].decision
        if decision.applied:
            applied += 1
            assert decision.target is not None and decision.target.kind == "anthropic"
            assert received[exchange.name] == with_model(
                exchange.request_bytes(), decision.target.model
            )
        else:
            assert received[exchange.name] == exchange.request_bytes(), exchange.name
    assert applied, "the config routes main and subagent traffic"
    never = {"compaction", "quota_check", "safety_check", "shares_main_prefix"}
    assert not any(o.decision.applied and never & set(o.decision.reasons) for o in gw.seen), (
        "traffic that never routes was routed"
    )
