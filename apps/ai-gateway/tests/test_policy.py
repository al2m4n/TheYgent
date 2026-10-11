"""The dry-run policy on real Claude Code traffic.

Every rule is pinned against the fixtures: what is never routed, where a subagent or the main
conversation could move, how a decision holds until the conversation's next cold boundary (and
when a warm move pays), which targets a request's shape rules out, and what the counterfactual
costs. Whatever the policy decides, observer
mode forwards the request unchanged.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from _fake_anthropic import fake_anthropic, serve
from _fixtures import Exchange, all_exchanges, load
from _prices import FROZEN_PRICES
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.conversations import ConversationTracker, Position
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.observe import Observation, ResponseObserver
from theygent_ai_gateway.policy import (
    Plan,
    Policy,
    accepts,
    costs_less,
    counterfactual_usd,
    fits,
    to_decision,
)
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.signals import RequestSignals, read_request
from theygent_ir.gateway import (
    FingerprintRule,
    FingerprintStatus,
    GatewayConfig,
    GatewayMode,
    LocalModel,
    LocalRouting,
    LocalShare,
    ModelRates,
    PolicyConfig,
    Target,
    Usage,
)

PRICES = PriceBook.packaged()
NOW = datetime(2026, 10, 8, tzinfo=UTC)


def config(
    *,
    mode: GatewayMode = "observer",
    subagent: str | None = "claude-sonnet-5-5",
    main: str | None = None,
    local: str | None = None,
    rules: dict[str, FingerprintStatus] | None = None,
    margin: float = 1.5,
) -> GatewayConfig:
    return GatewayConfig(
        version=1,
        mode=mode,
        policy=PolicyConfig(
            subagent_target=subagent,
            main_target=main,
            local_target=local,
            warm_switch_margin=margin,
        ),
        fingerprints=[FingerprintRule(fingerprint=f, status=s) for f, s in (rules or {}).items()],
        updated_at=NOW,
    )


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def signals_of(exchange: Exchange, **headers: str) -> RequestSignals:
    merged = dict(exchange.request_headers)
    merged.update({k.replace("_", "-"): v for k, v in headers.items()})
    return read_request(merged, exchange.request_bytes(), has_messages_body=True)


def usage_of(exchange: Exchange) -> Usage:
    observer = ResponseObserver(exchange.response_header("content-type"))
    observer.feed(exchange.response_bytes())
    return observer.finish().usage


class Run:
    """A tracker and policy fed requests in order, as the gateway does."""

    def __init__(self, cfg: GatewayConfig | None = None, prices: PriceBook = PRICES) -> None:
        self.clock = Clock()
        self.tracker = ConversationTracker(clock=self.clock)
        self.policy = Policy()
        self.cfg = cfg
        self.prices = prices

    def __call__(
        self, name: str, *, advance: float = 2.0, model: str | None = None, **headers: str
    ) -> tuple[Plan, Position]:
        self.clock.now += advance
        exchange = load(name)
        signals = signals_of(exchange, **headers)
        if model is not None:
            # Claude Code asking for another model in the same conversation.
            body = exchange.request_bytes().replace(b'"claude-opus-5-5"', f'"{model}"'.encode())
            merged = dict(exchange.request_headers)
            merged.update({k.replace("_", "-"): v for k, v in headers.items()})
            signals = read_request(merged, body, has_messages_body=True)
        position = self.tracker.begin(signals, None)
        plan = self.policy.decide(
            signals, position, self.cfg, endpoint_is_messages=True, prices=self.prices
        )
        observer = ResponseObserver(exchange.response_header("content-type"))
        observer.feed(exchange.response_bytes())
        self.tracker.complete(position, exchange.status, observer.finish())
        return plan, position


SONNET = Target(kind="anthropic", model="claude-sonnet-5-5")


# ── never routed ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "reasons"),
    [
        ("auxiliary-with-safeguards", ("safety_check",)),
        ("auxiliary-quota-check-429", ("quota_check",)),
        ("auxiliary-session-title", ("fingerprint_unreviewed", "local_candidate")),
    ],
)
def test_traffic_that_is_never_routed(name: str, reasons: tuple[str, ...]) -> None:
    plan, _ = Run(config(main="claude-sonnet-5-5"))(name)
    assert (plan.action, plan.reasons) == ("stay", reasons)


def test_compaction_stays_on_the_main_model() -> None:
    run = Run(config(main="claude-sonnet-5-5"))
    run("main-bugfix-1")
    plan, _ = run("compaction-manual")
    assert (plan.action, plan.reasons) == ("stay", ("compaction",))


def test_a_side_request_riding_the_main_cache_stays() -> None:
    plan = Policy().decide(
        signals_of(load("auxiliary-session-title")),
        Position(boundary="side_request", shares_main_prefix=True),
        config(rules={}),
        endpoint_is_messages=True,
    )
    assert plan.reasons == ("shares_main_prefix",)


def test_an_unrecognized_class_stays() -> None:
    plan, _ = Run(config())("main-bugfix-1", x_claude_code_request_class="speculative")
    assert plan.reasons == ("unrecognized_request_class",)


def test_unlabeled_traffic_stays() -> None:
    exchange = load("main-bugfix-1")
    headers = {k: v for k, v in exchange.request_headers if k != "x-claude-code-request-class"}
    signals = read_request(headers, exchange.request_bytes(), has_messages_body=True)
    plan = Policy().decide(signals, None, config(), endpoint_is_messages=True)
    assert plan.reasons == ("unlabeled_request",)


def test_other_endpoints_and_passthrough_mode_stay() -> None:
    signals = signals_of(load("main-bugfix-1"))
    assert Policy().decide(signals, None, config(), endpoint_is_messages=False).reasons == (
        "not_a_message_request",
    )
    assert Policy().decide(
        signals, None, config(mode="passthrough"), endpoint_is_messages=True
    ).reasons == ("mode_passthrough",)


# ── side requests and the catalog ────────────────────────────────────────────


def title_rule(status: FingerprintStatus, *, local: str | None = None) -> GatewayConfig:
    body = signals_of(load("auxiliary-session-title")).body
    assert body is not None
    return config(rules={body.fingerprint: status}, local=local)


@pytest.mark.parametrize(
    ("status", "local", "expected"),
    [
        ("never_route", None, ("stay", ("fingerprint_never_route",), None)),
        ("observe_only", None, ("stay", ("fingerprint_observe_only", "local_candidate"), None)),
        ("allow_local", None, ("stay", ("local_candidate", "no_target_configured"), None)),
        (
            "allow_local",
            "aux-local",
            ("route", ("local_candidate",), Target(kind="local", model="aux-local")),
        ),
        ("allow_cheaper", None, ("route", ("fingerprint_allow_cheaper",), SONNET)),
    ],
)
def test_reviewed_side_requests_follow_the_catalog(
    status: FingerprintStatus, local: str | None, expected: tuple
) -> None:
    plan, _ = Run(title_rule(status, local=local))("auxiliary-session-title")
    assert (plan.action, plan.reasons, plan.target) == expected


# ── subagents ────────────────────────────────────────────────────────────────


def test_a_subagent_moves_at_spawn_and_its_tool_loop_follows() -> None:
    run = Run(config())
    run("main-explore-1")
    spawn, _ = run("subagent-explore-1")
    second, _ = run("subagent-explore-2")
    third, _ = run("subagent-explore-3")
    assert (spawn.action, spawn.target, spawn.reasons) == ("route", SONNET, ("subagent_at_spawn",))
    for plan in (second, third):
        assert (plan.action, plan.target) == ("route", SONNET)
        assert plan.reasons == ("mid_conversation", "subagent_at_spawn")


def test_haiku_is_refused_for_claude_codes_subagent_turns() -> None:
    # The spike's measured rejections: adaptive thinking, effort, 128K output, system messages.
    plan, _ = Run(config(subagent="claude-haiku-4-5"))("subagent-explore-1")
    assert (plan.action, plan.reasons) == (
        "stay",
        ("subagent_at_spawn", "target_rejects_request_shape"),
    )


def test_a_subagent_already_on_the_target_stays() -> None:
    plan, _ = Run(config(subagent="claude-opus-5-5"))("subagent-explore-1")
    assert plan.reasons == ("subagent_at_spawn", "already_on_target")


def test_a_fast_mode_request_goes_only_to_a_target_with_fast_mode() -> None:
    # Sonnet 5.5 has no fast mode: a fast request to it would be refused.
    spawn = load("subagent-explore-1")
    fast = replace(spawn, body={**spawn.body, "speed": "fast"})
    signals = signals_of(fast)
    assert signals.fast and signals.body is not None and signals.body.shape.speed == "fast"
    assert not accepts("claude-sonnet-5-5", signals)
    assert accepts("claude-opus-5", signals)  # has a fast mode, takes it
    assert accepts("claude-sonnet-5-5", signals_of(spawn))


def test_a_request_for_a_cheaper_model_is_never_moved_up() -> None:
    # A subagent asking for Haiku 4.5 with Sonnet 5.5 as the target: moving would cost more.
    plan, _ = Run(config())("subagent-explore-1", model="claude-haiku-4-5")
    assert (plan.action, plan.reasons) == ("stay", ("subagent_at_spawn", "target_not_cheaper"))


def test_a_model_without_known_prices_is_not_moved() -> None:
    # Nothing shows that moving it would save anything.
    plan, _ = Run(config())("subagent-explore-1", model="claude-imaginary-9")
    assert (plan.action, plan.reasons) == ("stay", ("subagent_at_spawn", "target_not_cheaper"))


def _rates(model: str, **changes: float) -> ModelRates:
    rates = PRICES.rates(model)
    assert rates is not None
    return ModelRates(**{**asdict(rates), **changes})


def test_contracted_prices_decide_what_is_cheaper() -> None:
    # Contracted rates under which Sonnet's output costs more than Opus's: no saving to show.
    opus = PRICES.rates("claude-opus-5-5")
    assert opus is not None
    dearer = PRICES.with_overrides(
        {"claude-sonnet-5-5": _rates("claude-sonnet-5-5", output=opus.output + 1)}
    )
    plan, _ = Run(config(), dearer)("subagent-explore-1")
    assert plan.reasons == ("subagent_at_spawn", "target_not_cheaper")


@pytest.mark.parametrize(
    ("changes", "cheaper"),
    [
        ({}, False),  # the same rates: nothing to save
        ({"input": 1.0}, True),
        ({"input": 1.0, "cache_read": 99.0}, False),  # one rate higher outweighs the rest
    ],
)
def test_a_target_costs_less_only_with_no_rate_higher_and_one_lower(
    changes: dict[str, float], cheaper: bool
) -> None:
    prices = PRICES.with_overrides({"claude-twin-1": _rates("claude-opus-5-5", **changes)})
    assert costs_less(prices, "claude-twin-1", "claude-opus-5-5") is cheaper
    assert costs_less(PRICES, "claude-sonnet-5-5", "claude-opus-5-5")
    assert not costs_less(PRICES, "claude-sonnet-5-5", None)


def test_no_target_configured() -> None:
    plan, _ = Run(config(subagent=None))("subagent-explore-1")
    assert plan.reasons == ("subagent_at_spawn", "no_target_configured")


# ── the main conversation ────────────────────────────────────────────────────


def test_without_a_main_target_the_main_loop_stays() -> None:
    # The default dry-run policy names a subagent target only.
    plan, _ = Run(None)("main-bugfix-1")
    assert plan.reasons == ("cold_cache_boundary", "no_target_configured")


def test_the_main_conversation_moves_only_where_its_cache_is_cold() -> None:
    run = Run(config(main="claude-sonnet-5-5"))
    start, _ = run("main-bugfix-1")
    loop, _ = run("main-bugfix-2")
    new_prompt, _ = run("main-bugfix-3", advance=60, x_claude_code_prompt_id="next-prompt")
    after_prompt, _ = run("main-bugfix-3", advance=2, x_claude_code_prompt_id="next-prompt")
    expired, _ = run("main-bugfix-3", advance=3700, x_claude_code_prompt_id="next-prompt")
    assert (start.action, start.reasons) == ("route", ("cold_cache_boundary",))
    assert (loop.action, loop.reasons) == ("route", ("mid_conversation", "cold_cache_boundary"))
    # The routed conversation's cache is on the target now: a warm new prompt stays there.
    assert (new_prompt.action, new_prompt.target, new_prompt.reasons) == (
        "route",
        SONNET,
        ("follows_warm_cache",),
    )
    assert not new_prompt.cold
    assert after_prompt.reasons == ("mid_conversation", "follows_warm_cache")
    assert (expired.action, expired.reasons) == ("route", ("cold_cache_boundary",))


def test_a_main_conversation_in_its_trial_keeps_it_through_warm_prompts() -> None:
    medium = LocalModel(model="coder-medium", context_tokens=131_072, tools=True)
    cfg = GatewayConfig(
        version=1,
        mode="router",
        policy=PolicyConfig(
            local_models=[medium], local_routing=LocalRouting(main=LocalShare(trial=1.0))
        ),
        updated_at=NOW,
    )
    run = Run(cfg)
    start, _ = run("main-bugfix-1")
    loop, _ = run("main-bugfix-2")
    warm, _ = run("main-bugfix-3", advance=60, x_claude_code_prompt_id="next-prompt")
    tried = Target(kind="local", model="coder-medium")
    assert (start.action, start.trial) == ("stay", tried)
    assert (loop.trial, warm.trial) == (tried, tried)
    assert warm.reasons == ("no_target_configured",)  # the decision itself is unchanged


def test_a_warm_prompt_without_a_candidate_names_the_real_reason() -> None:
    run = Run(config(main=None))
    run("main-bugfix-1")
    plan, _ = run("main-bugfix-3", advance=60, x_claude_code_prompt_id="next-prompt")
    assert (plan.action, plan.reasons) == ("stay", ("no_target_configured",))


def test_a_target_added_mid_conversation_waits_for_a_cold_boundary() -> None:
    run = Run(config(main=None))
    run("main-bugfix-1")
    run.cfg = config(main="claude-sonnet-5-5")
    warm, _ = run("main-bugfix-3", advance=60, x_claude_code_prompt_id="next-prompt")
    expired, _ = run("main-bugfix-3", advance=3700, x_claude_code_prompt_id="later")
    assert (warm.action, warm.reasons) == ("stay", ("warm_cache_switch_cost",))
    assert (expired.action, expired.target) == ("route", SONNET)


def test_a_removed_target_does_not_pull_a_warm_conversation_back() -> None:
    # Moving back to the requested model is a switch too: it rebuilds the cache there.
    run = Run(config(main="claude-sonnet-5-5"))
    run("main-bugfix-1")
    run.cfg = config(main=None)
    warm, _ = run("main-bugfix-3", advance=60, x_claude_code_prompt_id="next-prompt")
    expired, _ = run("main-bugfix-3", advance=3700, x_claude_code_prompt_id="later")
    assert (warm.action, warm.target, warm.reasons) == (
        "route",
        SONNET,
        ("warm_cache_switch_cost",),
    )
    assert (expired.action, expired.reasons) == (
        "stay",
        ("cold_cache_boundary", "no_target_configured"),
    )


def test_a_warm_move_happens_only_where_the_margin_says_it_pays() -> None:
    # Contracted rates that make rebuilding on Sonnet nearly free: staying on Opus's warm cache
    # costs more than twice the rebuild, so a margin of 2 moves it and a margin of 50 does not.
    cheap = ModelRates(input=0.1, output=1, cache_write_5m=0.01, cache_write_1h=0.01, cache_read=0)
    prices = PRICES.with_overrides({"claude-sonnet-5-5": cheap})
    for margin, moves in ((2.0, True), (50.0, False)):
        run = Run(config(main=None, margin=margin), prices)
        run("main-bugfix-1")
        run("main-bugfix-2")
        run.cfg = config(main="claude-sonnet-5-5", margin=margin)
        plan, position = run("main-bugfix-3", advance=60, x_claude_code_prompt_id="next")
        assert position.cache_warm and position.cached_prefix_tokens > 0
        if moves:
            assert (plan.action, plan.target, plan.reasons) == (
                "route",
                SONNET,
                ("warm_switch_pays",),
            )
            assert plan.cold
            usage = usage_of(load("main-bugfix-3"))
            assert counterfactual_usd(plan, usage, position, prices) == prices.cost_cold(
                "claude-sonnet-5-5", usage, "1h"
            )
        else:
            assert (plan.action, plan.reasons) == ("stay", ("warm_cache_switch_cost",))


def test_claude_code_switching_model_itself_decides_afresh() -> None:
    run = Run(config(main="claude-sonnet-5-5"))
    run("main-bugfix-1")
    plan, position = run(
        "main-bugfix-3",
        advance=60,
        model="claude-fable-5-1",
        x_claude_code_prompt_id="next-prompt",
    )
    assert position.boundary == "new_prompt"
    assert (plan.action, plan.target, plan.reasons) == ("route", SONNET, ("cold_cache_boundary",))


def test_after_compaction_is_a_free_switch_point() -> None:
    run = Run(config(main="claude-sonnet-5-5"))
    run("main-bugfix-1")
    run("main-bugfix-2", advance=60, x_claude_code_prompt_id="p2")  # warm new prompt: follows
    plan, position = run("main-bugfix-3", x_claude_code_context_compacted="auto")
    assert position.boundary == "after_compaction"
    assert (plan.action, plan.reasons) == ("route", ("cold_cache_boundary",))


def test_conversation_memory_is_bounded() -> None:
    policy = Policy(max_conversations=2)
    tracker = ConversationTracker(clock=Clock())
    for n in range(4):
        signals = signals_of(load("main-bugfix-1"), x_claude_code_session_id=f"s{n}")
        policy.decide(signals, tracker.begin(signals, None), config(), endpoint_is_messages=True)
    assert len(policy._held) == 2


# ── capability and context checks ────────────────────────────────────────────


def test_capabilities() -> None:
    main = signals_of(load("main-bugfix-1"))
    assert accepts("claude-sonnet-5-5", main)
    assert accepts("claude-opus-5-5[1m]", main)
    assert not accepts("claude-haiku-4-5", main)
    assert not accepts("claude-sonnet-5", signals_of(load("subagent-haiku-system-role-400")))
    assert not accepts("claude-unknown-model", main)


def test_context_must_fit_the_target() -> None:
    signals = signals_of(load("main-bugfix-1"))
    assert fits(
        "claude-sonnet-5-5",
        signals,
        Position(boundary="continuation", cached_prefix_tokens=900_000),
    )
    assert not fits(
        "claude-haiku-4-5", signals, Position(boundary="continuation", cached_prefix_tokens=250_000)
    )


# ── counterfactual cost ──────────────────────────────────────────────────────


def test_a_stay_has_no_counterfactual() -> None:
    assert (
        counterfactual_usd(Plan.stay("compaction"), Usage(output_tokens=10), None, PRICES) is None
    )


def test_a_local_route_costs_nothing_in_api_spend() -> None:
    plan = Plan.route(Target(kind="local", model="aux-local"), "local_candidate")
    assert counterfactual_usd(plan, Usage(input_tokens=1200, output_tokens=16), None, PRICES) == 0.0


def test_only_a_warm_switch_pays_to_rebuild_the_cache() -> None:
    usage = Usage(output_tokens=600, cache_read_tokens=120_000, cache_write_1h_tokens=3000)
    warm = Position(boundary="new_prompt", cache_warm=True, ttl_seconds=3600)
    switch = replace(Plan.route(SONNET, "warm_switch_pays"), cold=True)
    assert counterfactual_usd(switch, usage, warm, PRICES) == PRICES.cost_cold(
        "claude-sonnet-5-5", usage, "1h"
    )
    # A conversation already on the target reads its own warm cache there.
    follows = Plan.route(SONNET, "follows_warm_cache")
    assert counterfactual_usd(follows, usage, warm, PRICES) == PRICES.cost(
        "claude-sonnet-5-5", usage
    )


def test_the_estimate_against_the_one_real_pair() -> None:
    # Pricing the Opus run's tokens on Sonnet (the counterfactual's assumption) against what the
    # same work really cost on Sonnet: the estimate is close, and errs high here because Sonnet
    # wrote fewer output tokens than Opus did.
    run = Run(config(), FROZEN_PRICES)
    estimate = 0.0
    for name in ("subagent-explore-1", "subagent-explore-2", "subagent-explore-3"):
        plan, position = run(name)
        estimate += counterfactual_usd(plan, usage_of(load(name)), position, FROZEN_PRICES) or 0.0
    actual = sum(
        FROZEN_PRICES.cost("claude-sonnet-5-5", usage_of(load(f"subagent-explore-sonnet-{n}")))
        or 0.0
        for n in (1, 2, 3)
    )
    assert estimate == pytest.approx(0.0810, abs=1e-4)
    assert actual == pytest.approx(0.0788, abs=1e-4)
    assert abs(estimate - actual) / actual < 0.05


def test_decisions_are_never_applied_in_observer_mode() -> None:
    decision = to_decision(Plan.route(SONNET, "subagent_at_spawn"), 0.05)
    assert decision.applied is False
    assert decision.counterfactual_usd == 0.05


# ── observer mode never changes a request ────────────────────────────────────


@pytest.fixture
def everything_routes() -> GatewayConfig:
    title = signals_of(load("auxiliary-session-title")).body
    assert title is not None
    return config(
        main="claude-sonnet-5-5",
        local="aux-local",
        rules={title.fingerprint: "allow_local"},
    )


def test_requests_are_forwarded_unchanged_whatever_the_decision(
    tmp_path: Path, everything_routes: GatewayConfig
) -> None:
    seen: list[Observation] = []
    replayed = [e for e in all_exchanges() if e.abort_after_events is None]
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw", state_dir=tmp_path
        )
        with serve(create_app(settings, observe=seen.append, config=everything_routes)) as url:
            for count, exchange in enumerate(replayed, 1):
                httpx.request(
                    exchange.method,
                    f"{url}{exchange.target}",
                    content=exchange.request_bytes(),
                    headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
                    timeout=10,
                )
                deadline = time.monotonic() + 10
                while len(seen) < count:
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
            received = {r.header("x-fake-fixture")[0]: r.body for r in upstream.requests}
    routed = [o for o in seen if o.decision is not None and o.decision.action == "route"]
    assert routed, "the config routes main, subagent and title traffic"
    for exchange in replayed:
        sent = hashlib.sha256(exchange.request_bytes()).hexdigest()
        assert hashlib.sha256(received[exchange.name]).hexdigest() == sent
    assert all(o.decision is not None and o.decision.applied is False for o in seen)
    assert {o.decision.target.model for o in routed if o.decision and o.decision.target} == {
        "claude-sonnet-5-5",
        "aux-local",
    }
