"""The local dials: which conversations a local model serves, which model, and what happens when
it cannot.

What must hold:

* a share is sampled per conversation, the same way every time, and at cold boundaries only — a
  subagent at its spawn, the main conversation at its start — then held across its tool loop;
* the model is the dial's, or the smallest listed that can take the request (tool calls for an
  agent turn, the prompt within its context); none fitting falls to Anthropic's side, saying so;
* an agent-type filter limits the subagents that go local;
* a local failure re-decides on Anthropic's side — a subagent may still start on the cheaper
  target — and holds that until the next cold boundary;
* through the gateway: an Explore subagent's turns are served locally, tools and all, and recorded
  so; observer mode only records it;
* a trial takes the conversations just above the share: Anthropic answers them, whatever the
  decision, and the local model is tried on each request afterwards — in router mode too.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _fake_inference import FakeInference, fake_inference
from _fixtures import Exchange, load
from _prices import FROZEN_PRICES
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.conversations import ConversationTracker
from theygent_ai_gateway.observe import Observation, ResponseObserver
from theygent_ai_gateway.policy import Plan, Policy, in_share, in_trial, local_model_for
from theygent_ai_gateway.requestlog import to_record
from theygent_ai_gateway.routing import with_model
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.signals import read_request
from theygent_ir.gateway import GatewayConfig, GatewayMode, LocalShare, PolicyConfig, Target

SONNET = "claude-sonnet-5-5"
SMALL = {"model": "aux-small", "context_tokens": 32_768}
MEDIUM = {"model": "coder-medium", "context_tokens": 131_072, "tools": True}
MAIN, SPAWN, LOOP = load("main-explore-1"), load("subagent-explore-1"), load("subagent-explore-2")


def config(
    mode: GatewayMode = "router",
    *,
    models: list[dict[str, Any]] | None = None,
    subagents: dict[str, Any] | None = None,
    main: dict[str, Any] | None = None,
    **policy: Any,
) -> GatewayConfig:
    return GatewayConfig(
        version=1,
        mode=mode,
        policy=PolicyConfig.model_validate(
            {
                "subagent_target": SONNET,
                "shadow_local": False,
                "local_models": models if models is not None else [SMALL, MEDIUM],
                "local_routing": {"subagents": subagents or {}, "main": main or {}},
                **policy,
            }
        ),
        updated_at=datetime(2026, 10, 11, tzinfo=UTC),
    )


class Run:
    """A tracker and policy fed requests in order, as the gateway does."""

    def __init__(self, cfg: GatewayConfig) -> None:
        self.tracker = ConversationTracker()
        self.policy = Policy()
        self.cfg = cfg

    def __call__(self, exchange: Exchange) -> tuple[Plan, Any]:
        headers = dict(exchange.request_headers)
        signals = read_request(headers, exchange.request_bytes(), has_messages_body=True)
        position = self.tracker.begin(signals, None)
        plan = self.policy.decide(
            signals, position, self.cfg, endpoint_is_messages=True, prices=FROZEN_PRICES
        )
        observer = ResponseObserver(exchange.response_header("content-type"))
        observer.feed(exchange.response_bytes())
        self.tracker.complete(position, exchange.status, observer.finish())
        return plan, (signals, position)


LOCAL_MEDIUM = Target(kind="local", model="coder-medium")


# ── the policy ───────────────────────────────────────────────────────────────


def test_a_share_of_zero_changes_nothing() -> None:
    run = Run(config())
    run(MAIN)
    plan, _ = run(SPAWN)
    assert plan.target == Target(kind="anthropic", model=SONNET)
    assert plan.reasons == ("subagent_at_spawn",)


def test_a_subagent_in_the_share_goes_local_at_spawn_and_stays_for_its_loop() -> None:
    run = Run(config(subagents={"share": 1.0}))
    main, _ = run(MAIN)
    spawn, _ = run(SPAWN)
    loop, _ = run(LOOP)
    assert main.action == "stay"  # the main dial is at zero
    # The smallest listed model that follows tool calls.
    assert (spawn.target, spawn.reasons) == (LOCAL_MEDIUM, ("subagent_at_spawn", "local_share"))
    assert loop.target == LOCAL_MEDIUM and loop.reasons[0] == "mid_conversation"


def test_the_dials_model_is_used_when_it_fits() -> None:
    big = {"model": "coder-large", "context_tokens": 262_144, "tools": True}
    run = Run(config(models=[SMALL, MEDIUM, big], subagents={"share": 1.0, "model": "coder-large"}))
    run(MAIN)
    plan, _ = run(SPAWN)
    assert plan.target == Target(kind="local", model="coder-large")


def test_no_model_that_fits_falls_to_anthropic_saying_so() -> None:
    tiny = {"model": "coder-tiny", "context_tokens": 1_024, "tools": True}
    run = Run(config(models=[tiny], subagents={"share": 1.0}))
    run(MAIN)
    plan, _ = run(SPAWN)  # an 8K-byte prompt does not fit 1K tokens
    assert plan.target == Target(kind="anthropic", model=SONNET)
    assert plan.reasons == ("subagent_at_spawn", "no_local_model_fits")


def test_an_agent_type_filter_keeps_other_subagents_off_the_local_model() -> None:
    run = Run(config(subagents={"share": 1.0, "agent_types": ["Plan"]}))
    run(MAIN)
    plan, _ = run(SPAWN)  # an Explore subagent
    assert plan.target == Target(kind="anthropic", model=SONNET)


def test_the_main_conversation_goes_local_at_its_start() -> None:
    run = Run(config(main={"share": 1.0}))
    plan, _ = run(MAIN)
    assert (plan.target, plan.reasons) == (LOCAL_MEDIUM, ("cold_cache_boundary", "local_share"))


def spawn_point() -> float:
    """Where the Explore subagent's conversation falls in [0, 1)."""
    run = Run(config(subagents={"share": 1.0}))
    run(MAIN)
    _, (_, position) = run(SPAWN)
    low, high = 0.0, 1.0
    for _ in range(40):
        middle = (low + high) / 2
        low, high = (low, middle) if in_share(position.key, middle) else (middle, high)
    return high


def test_a_partial_share_decides_by_the_conversation() -> None:
    # A share just above the conversation's point takes it local, one just below leaves it with
    # Anthropic.
    point = spawn_point()
    for share, local in ((min(point + 0.01, 1.0), True), (max(point - 0.01, 0.0), False)):
        run = Run(config(subagents={"share": share}))
        run(MAIN)
        plan, _ = run(SPAWN)
        assert (plan.target == LOCAL_MEDIUM) is local, share


def test_a_share_is_sampled_per_conversation_and_stable() -> None:
    keys = [("usr", f"session-{n}", "agent") for n in range(4_000)]
    inside = [key for key in keys if in_share(key, 0.3)]
    assert 0.27 < len(inside) / len(keys) < 0.33
    assert all(in_share(key, 0.3) for key in inside)  # the same answer every time
    assert not any(in_share(key, 0.0) for key in keys)
    assert all(in_share(key, 1.0) for key in keys)


def test_a_local_failure_holds_anthropics_side_until_the_next_cold_boundary() -> None:
    run = Run(config(subagents={"share": 1.0}))
    run(MAIN)
    spawn, (signals, position) = run(SPAWN)
    assert spawn.target == LOCAL_MEDIUM
    cloud = run.policy.after_local_failure(signals, position, run.cfg, FROZEN_PRICES, spawn)
    assert cloud.target == Target(kind="anthropic", model=SONNET)
    assert cloud.reasons == ("target_refused", "subagent_at_spawn")
    loop, _ = run(LOOP)
    assert loop.target == Target(kind="anthropic", model=SONNET)  # not back to the local model


def test_a_side_requests_dial_picks_the_smallest_model_that_fits() -> None:
    title = load("auxiliary-session-title")
    signals = read_request(
        dict(title.request_headers), title.request_bytes(), has_messages_body=True
    )
    assert signals.body is not None and signals.body.fingerprint is not None
    rules = [{"fingerprint": signals.body.fingerprint, "status": "allow_local"}]
    cfg = GatewayConfig.model_validate({**config().model_dump(), "fingerprints": rules})
    plan, _ = Run(cfg)(title)
    assert plan.target == Target(kind="local", model="aux-small")
    off = {**cfg.model_dump()}
    off["policy"]["local_routing"]["side_requests"]["share"] = 0.0
    plan, _ = Run(GatewayConfig.model_validate(off))(title)
    assert plan.action == "stay" and plan.reasons == ("local_candidate",)


def test_a_trial_takes_the_conversations_just_above_the_share() -> None:
    keys = [("usr", f"session-{n}", "agent") for n in range(4_000)]
    dial = LocalShare(share=0.2, trial=0.1)
    tried = {key for key in keys if in_trial(key, dial)}
    # Raising the share by the trial serves exactly the conversations tried.
    assert tried == {k for k in keys if in_share(k, 0.3)} - {k for k in keys if in_share(k, 0.2)}
    assert 0.08 < len(tried) / len(keys) < 0.12
    assert not any(in_trial(key, LocalShare(share=0.2)) for key in keys)  # no trial unless chosen


def test_a_subagent_in_the_trial_stays_with_anthropic_and_is_tried_for_its_loop() -> None:
    point = spawn_point()
    share = max(point - 0.01, 0.0)
    run = Run(config(subagents={"share": share, "trial": 0.02}))
    main, _ = run(MAIN)
    spawn, _ = run(SPAWN)
    loop, _ = run(LOOP)
    assert main.trial is None  # the main dial has no trial
    # Anthropic's side decides as without a trial: here the cheaper target.
    assert (spawn.target, spawn.reasons) == (
        Target(kind="anthropic", model=SONNET),
        ("subagent_at_spawn",),
    )
    assert spawn.trial == LOCAL_MEDIUM and loop.trial == LOCAL_MEDIUM
    # Below the trial, or once inside the share, there is nothing to try.
    for dial in ({"share": 0.0, "trial": share}, {"share": min(point + 0.01, 1.0)}):
        run = Run(config(subagents=dial))
        run(MAIN)
        plan, _ = run(SPAWN)
        assert plan.trial is None, dial


def test_a_trial_is_only_for_conversations_a_local_model_could_take() -> None:
    tiny = {"model": "coder-tiny", "context_tokens": 1_024, "tools": True}
    for cfg in (
        config(subagents={"trial": 1.0, "agent_types": ["Plan"]}),  # an Explore subagent
        config(models=[tiny], subagents={"trial": 1.0}),  # its prompt does not fit
    ):
        run = Run(cfg)
        run(MAIN)
        plan, _ = run(SPAWN)
        assert plan.trial is None
        assert plan.target == Target(kind="anthropic", model=SONNET)


def test_a_side_request_in_the_trial_is_tried_on_its_model() -> None:
    title = load("auxiliary-session-title")
    signals = read_request(
        dict(title.request_headers), title.request_bytes(), has_messages_body=True
    )
    assert signals.body is not None and signals.body.fingerprint is not None
    cfg = config().model_dump()
    cfg["fingerprints"] = [{"fingerprint": signals.body.fingerprint, "status": "allow_local"}]
    cfg["policy"]["local_routing"]["side_requests"] = {"share": 0.0, "trial": 1.0}
    plan, _ = Run(GatewayConfig.model_validate(cfg))(title)
    assert (plan.action, plan.reasons) == ("stay", ("local_candidate",))
    assert plan.trial == Target(kind="local", model="aux-small")


# ── through the gateway ──────────────────────────────────────────────────────


@dataclass
class Gateway:
    url: str
    upstream: FakeAnthropic
    inference: FakeInference
    seen: list[Observation]

    def send(self, exchange: Exchange) -> httpx.Response:
        count = len(self.seen)
        response = httpx.post(
            f"{self.url}{exchange.target}",
            content=exchange.request_bytes(),
            headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
            timeout=10,
        )
        deadline = time.monotonic() + 10
        while len(self.seen) <= count:
            assert time.monotonic() < deadline, "no observation"
            time.sleep(0.01)
        return response


@contextmanager
def gateway(tmp_path: Path, cfg: GatewayConfig) -> Iterator[Gateway]:
    seen: list[Observation] = []
    with fake_anthropic() as upstream, fake_inference() as inference:
        settings = GatewaySettings(
            upstream_url=upstream.base_url,
            inference_plane_url=inference.base_url,
            gateway_id="gw",
            state_dir=tmp_path,
        )
        app = create_app(settings, observe=seen.append, config=cfg, prices=FROZEN_PRICES)
        with serve(app) as url:
            yield Gateway(url, upstream, inference, seen)


def tool_uses(body: bytes) -> list[tuple[str, Any]]:
    blocks: dict[int, dict[str, Any]] = {}
    for chunk in body.decode().split("\n\n"):
        lines = chunk.strip().split("\n")
        if len(lines) != 2:
            continue
        data = json.loads(lines[1][6:])
        if data["type"] == "content_block_start" and data["content_block"]["type"] == "tool_use":
            blocks[data["index"]] = {"name": data["content_block"]["name"], "json": ""}
        elif data["type"] == "content_block_delta" and data["delta"]["type"] == "input_json_delta":
            blocks[data["index"]]["json"] += data["delta"]["partial_json"]
    return [(b["name"], json.loads(b["json"])) for _, b in sorted(blocks.items())]


def test_an_explore_subagent_is_served_locally_tools_and_all(tmp_path: Path) -> None:
    with gateway(tmp_path, config(subagents={"share": 1.0, "agent_types": ["Explore"]})) as gw:
        gw.inference.pieces = []
        gw.inference.tool_calls = [("Grep", ['{"pattern": "def add"}'])]
        gw.send(MAIN)
        answer = gw.send(SPAWN)
        spawned = gw.seen[-1]
        gw.send(LOOP)
        looped = gw.seen[-1]
    # The main turn went to Anthropic; both subagent turns to the local model, never upstream.
    assert [r.header("x-fake-fixture")[0] for r in gw.upstream.requests] == [MAIN.name]
    assert len(gw.inference.requests) == 2
    asked = gw.inference.requests[0]
    assert asked["model"] == "coder-medium" and {t["function"]["name"] for t in asked["tools"]} >= {
        "Grep",
        "Read",
    }
    assert tool_uses(answer.content) == [("Grep", {"pattern": "def add"})]
    for observed in (spawned, looped):
        assert observed.decision.applied and observed.decision.target == LOCAL_MEDIUM
        assert observed.response.model == "coder-medium" and observed.cost_usd == 0.0
        assert observed.response.stop_reason == "tool_use"
    assert spawned.decision.reasons == ["subagent_at_spawn", "local_share"]
    assert spawned.baseline_usd is not None and spawned.baseline_usd > 0


def test_a_subagent_the_local_model_cannot_take_starts_on_the_cheaper_target(
    tmp_path: Path,
) -> None:
    with gateway(tmp_path, config(subagents={"share": 1.0})) as gw:
        gw.inference.status = 503
        gw.send(MAIN)
        gw.send(SPAWN)
        failed = gw.seen[-1]
        gw.send(LOOP)
        after = gw.seen[-1]
    sent = [r.body for r in gw.upstream.requests]
    assert sent[1] == with_model(SPAWN.request_bytes(), SONNET)
    assert failed.decision.applied and failed.decision.target == Target(
        kind="anthropic", model=SONNET
    )
    assert failed.decision.reasons == ["target_refused", "subagent_at_spawn"]
    # The rest of its loop stays there: the local model is not asked again.
    assert sent[2] == with_model(LOOP.request_bytes(), SONNET)
    assert len(gw.inference.requests) == 1
    assert after.decision.target is not None and after.decision.target.model == SONNET


def test_observer_mode_only_records_what_local_routing_would_do(tmp_path: Path) -> None:
    with gateway(tmp_path, config("observer", subagents={"share": 1.0})) as gw:
        gw.send(MAIN)
        answer = gw.send(SPAWN)
    assert gw.inference.requests == []
    assert gw.upstream.requests[1].body == SPAWN.request_bytes()
    assert answer.content == SPAWN.response_bytes()
    decision = gw.seen[-1].decision
    assert (decision.action, decision.applied, decision.target) == ("route", False, LOCAL_MEDIUM)
    assert decision.counterfactual_usd == 0.0


# ── measuring a local model on agent turns ───────────────────────────────────


@pytest.mark.parametrize(("local_first", "agreed"), [("Bash", True), ("Grep", False)])
def test_a_local_shadow_says_whether_it_picked_the_same_next_action(
    tmp_path: Path, local_first: str, agreed: bool
) -> None:
    # Observer mode: the real answer to the Explore spawn called Bash first.
    cfg = config("observer", subagents={"share": 1.0}, shadow_local=True)
    with gateway(tmp_path, cfg) as gw:
        gw.inference.pieces = []
        gw.inference.tool_calls = [(local_first, ['{"command": "ls"}'])]
        gw.send(MAIN)
        gw.send(SPAWN)
    run = gw.seen[-1].shadow
    assert run is not None and run.target == LOCAL_MEDIUM and run.outcome == "answered"
    assert run.agreed is agreed
    assert run.first_output_ms is not None and run.cost_usd == 0.0


def test_a_local_answer_where_the_real_one_called_a_tool_disagrees(tmp_path: Path) -> None:
    cfg = config("observer", subagents={"share": 1.0}, shadow_local=True)
    with gateway(tmp_path, cfg) as gw:
        gw.inference.pieces = ["I would just answer."]
        gw.send(MAIN)
        gw.send(SPAWN)
    run = gw.seen[-1].shadow
    assert run is not None and run.agreed is False


# ── a local trial ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("local_first", "agreed"), [("Bash", True), ("Grep", False)])
def test_router_mode_tries_the_local_model_after_anthropic_answers(
    tmp_path: Path, local_first: str, agreed: bool
) -> None:
    # Every Explore subagent is in the trial: served by Anthropic (on the cheaper target, as
    # without a trial), then tried locally. Local shadows are off: a trial is its own opt-in.
    cfg = config(subagents={"trial": 1.0, "agent_types": ["Explore"]})
    assert not cfg.policy.shadow_local
    with gateway(tmp_path, cfg) as gw:
        gw.inference.pieces = []
        gw.inference.tool_calls = [(local_first, ['{"command": "ls"}'])]
        gw.send(MAIN)
        answer = gw.send(SPAWN)
        spawned = gw.seen[-1]
        gw.send(LOOP)
        looped = gw.seen[-1]
    sent = [r.body for r in gw.upstream.requests]
    assert sent[1:] == [
        with_model(SPAWN.request_bytes(), SONNET),
        with_model(LOOP.request_bytes(), SONNET),
    ]
    # Claude Code got Anthropic's answer, from the model it was routed to.
    assert answer.status_code == 200 and f'"model":"{SONNET}"'.encode() in answer.content
    assert len(gw.inference.requests) == 2  # each subagent request, tried once
    for observed in (spawned, looped):
        decision = observed.decision
        assert decision.applied and decision.target == Target(kind="anthropic", model=SONNET)
        run = observed.shadow
        assert run is not None and run.trial and run.target == LOCAL_MEDIUM
        assert run.outcome == "answered" and run.cost_usd == 0.0
        record = to_record(observed, record_id="greq_trial", gateway_id="gw")
        assert record.shadow == run  # a valid wire record
    assert spawned.shadow is not None and spawned.shadow.agreed is agreed
    assert gw.seen[0].shadow is None  # the main conversation has no trial


def test_a_trial_also_follows_a_stay(tmp_path: Path) -> None:
    with gateway(tmp_path, config(subagent_target=None, subagents={"trial": 1.0})) as gw:
        gw.send(MAIN)
        gw.send(SPAWN)
    observed = gw.seen[-1]
    assert observed.decision.action == "stay"
    assert gw.upstream.requests[1].body == SPAWN.request_bytes()
    assert observed.shadow is not None and observed.shadow.trial


def test_in_observer_mode_a_trial_runs_where_the_decision_has_no_shadow(tmp_path: Path) -> None:
    # The decision names the cheaper target, but cloud shadows are off: the trial runs instead.
    with gateway(tmp_path, config("observer", subagents={"trial": 1.0})) as gw:
        gw.send(MAIN)
        gw.send(SPAWN)
    observed = gw.seen[-1]
    assert (observed.decision.action, observed.decision.applied) == ("route", False)
    assert observed.shadow is not None and observed.shadow.trial
    assert observed.shadow.target == LOCAL_MEDIUM
    to_record(observed, record_id="greq_trial", gateway_id="gw")


def test_a_conversation_served_locally_is_never_tried(tmp_path: Path) -> None:
    # A served conversation is in the share, never the trial; and a local answer is not shadowed.
    with gateway(tmp_path, config(subagents={"share": 0.5, "trial": 0.5})) as gw:
        gw.inference.pieces = ["Done."]
        gw.send(MAIN)
        gw.send(SPAWN)
    observed = gw.seen[-1]
    assert observed.decision.applied and observed.decision.target == LOCAL_MEDIUM
    assert observed.shadow is None and len(gw.inference.requests) == 1


# ── compaction, output budgets and what local compute costs ──────────────────

BUGFIX, COMPACTION = load("main-bugfix-1"), load("compaction-manual")


def test_compaction_of_a_local_conversation_runs_on_its_local_model(tmp_path: Path) -> None:
    # Compaction reads the conversation's cache, so it goes where the conversation is: here, the
    # local model, rather than to Anthropic with nothing cached.
    with gateway(tmp_path, config(main={"share": 1.0})) as gw:
        gw.inference.pieces = ["The summary."]
        gw.send(BUGFIX)
        gw.send(COMPACTION)
        compacted = gw.seen[-1]
    assert gw.upstream.requests == []  # neither went to Anthropic
    assert compacted.decision.applied and compacted.decision.target == LOCAL_MEDIUM
    assert compacted.decision.reasons == ["compaction", "follows_warm_cache"]


def test_a_compaction_the_local_model_cannot_take_goes_to_the_requested_model(
    tmp_path: Path,
) -> None:
    with gateway(tmp_path, config(main={"share": 1.0})) as gw:
        gw.inference.pieces = ["Working on it."]
        gw.send(BUGFIX)
        gw.inference.status = 503
        gw.send(COMPACTION)
        failed = gw.seen[-1]
    assert [r.body for r in gw.upstream.requests] == [COMPACTION.request_bytes()]
    assert failed.decision.reasons[0] == "target_refused" and not failed.decision.applied


def test_an_agent_turn_may_write_up_to_the_models_own_budget(tmp_path: Path) -> None:
    big = {**MEDIUM, "max_output_tokens": 65_536}
    with gateway(tmp_path, config(models=[big], subagents={"share": 1.0})) as gw:
        gw.inference.pieces = ["Done."]
        gw.send(MAIN)
        gw.send(SPAWN)
    # Claude Code asks for 128K; the model's budget, not the default 16K, bounds it.
    assert [r["max_tokens"] for r in gw.inference.requests] == [65_536]


RATES = {"input": 0.5, "output": 2.0}


def test_a_local_answer_costs_what_running_the_model_costs(tmp_path: Path) -> None:
    priced = {**MEDIUM, "rates": RATES}
    with gateway(tmp_path, config(models=[priced], subagents={"share": 1.0})) as gw:
        gw.inference.pieces = ["Done."]
        gw.send(MAIN)
        gw.send(SPAWN)
    served = gw.seen[-1]
    usage = served.response.usage
    prompt = usage.input_tokens + usage.cache_read_tokens
    prompt += usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
    assert prompt and usage.output_tokens
    expected = (prompt * 0.5 + usage.output_tokens * 2.0) / 1_000_000
    assert served.decision.applied and served.cost_usd == pytest.approx(expected)
    # What it would have cost on the model Claude Code asked for is still the baseline.
    assert served.baseline_usd is not None and served.cost_usd is not None
    assert served.baseline_usd > served.cost_usd


def test_observer_mode_prices_a_local_route_and_its_shadow_at_the_models_rates(
    tmp_path: Path,
) -> None:
    priced = {**MEDIUM, "rates": RATES}
    cfg = config("observer", models=[priced], subagents={"share": 1.0}, shadow_local=True)
    with gateway(tmp_path, cfg) as gw:
        gw.inference.pieces = ["Done."]
        gw.send(MAIN)
        gw.send(SPAWN)
    observed = gw.seen[-1]
    usage = observed.response.usage
    prompt = usage.input_tokens + usage.cache_read_tokens
    prompt += usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
    expected = (prompt * 0.5 + usage.output_tokens * 2.0) / 1_000_000
    assert observed.decision.counterfactual_usd == pytest.approx(expected)
    run = observed.shadow
    assert run is not None and run.usage.output_tokens
    assert run.cost_usd == pytest.approx(
        (run.usage.input_tokens * 0.5 + run.usage.output_tokens * 2.0) / 1_000_000
    )


def test_a_model_without_rates_still_costs_nothing(tmp_path: Path) -> None:
    with gateway(tmp_path, config(subagents={"share": 1.0})) as gw:
        gw.inference.pieces = ["Done."]
        gw.send(MAIN)
        gw.send(SPAWN)
    assert gw.seen[-1].cost_usd == 0.0


# ── a conversation that outgrows its local model ─────────────────────────────

#: Takes the Explore subagent's first turn (about 2,700 tokens) but not its second (about 2,900).
SMALL_TOOLS = {"model": "coder-small", "context_tokens": 2_800, "tools": True}
LARGE = {"model": "coder-large", "context_tokens": 262_144, "tools": True}
LOCAL_SMALL = Target(kind="local", model="coder-small")
LOCAL_LARGE = Target(kind="local", model="coder-large")


def test_a_model_that_cannot_take_the_prompt_gives_way_to_a_larger_one() -> None:
    no_tools = {"model": "vision-large", "context_tokens": 262_144, "images": True}
    policy = PolicyConfig.model_validate({"local_models": [SMALL_TOOLS, no_tools, LARGE]})
    signals = read_request(dict(LOOP.request_headers), LOOP.request_bytes(), has_messages_body=True)
    spawn = read_request(dict(SPAWN.request_headers), SPAWN.request_bytes(), has_messages_body=True)

    def pick(chosen: str | None, request: Any) -> str | None:
        found = local_model_for(policy, chosen, request, tools=True)
        return found.model if found is not None else None

    assert pick("coder-small", spawn) == "coder-small"
    # Too large for the chosen model: the first larger one that follows tool calls.
    assert pick("coder-small", signals) == "coder-large"
    assert pick(None, signals) == "coder-large"
    # Never a smaller one than the chosen model, and nothing for a model not listed.
    assert pick("coder-large", spawn) == "coder-large"
    assert pick("unlisted", spawn) is None
    # A chosen model that can't take it (no tool calls) passes to a larger one, though a smaller
    # one could take it.
    assert pick("vision-large", spawn) == "coder-large"


def test_a_local_conversation_that_outgrows_its_model_moves_to_a_larger_one() -> None:
    run = Run(config(models=[SMALL_TOOLS, LARGE], subagents={"share": 1.0, "model": "coder-small"}))
    run(MAIN)
    spawn, _ = run(SPAWN)
    grown, _ = run(LOOP)
    after, _ = run(LOOP)
    assert spawn.target == LOCAL_SMALL
    assert (grown.target, grown.reasons) == (LOCAL_LARGE, ("outgrew_local_model",))
    # It stays on the larger model for the rest of its loop.
    assert (after.target, after.reasons) == (
        LOCAL_LARGE,
        ("mid_conversation", "outgrew_local_model"),
    )


def test_a_conversation_no_listed_model_can_hold_goes_to_anthropics_side() -> None:
    run = Run(config(models=[SMALL_TOOLS], subagents={"share": 1.0}))
    run(MAIN)
    spawn, _ = run(SPAWN)
    grown, _ = run(LOOP)
    after, _ = run(LOOP)
    assert spawn.target == LOCAL_SMALL
    # The cheaper Anthropic target, which holds nothing of the conversation yet.
    assert grown.target == Target(kind="anthropic", model=SONNET)
    assert grown.reasons[0] == "outgrew_local_model" and grown.cold
    assert after.target == Target(kind="anthropic", model=SONNET)  # and it stays there


def test_a_compaction_too_large_for_its_local_model_goes_to_a_larger_one_or_anthropic() -> None:
    small = {
        "model": "coder-small",
        "context_tokens": 3_000,
        "tools": True,
    }  # the turn, not its compaction
    for models, main_target, target, reasons in (
        ([small, LARGE], None, LOCAL_LARGE, ("compaction", "outgrew_local_model")),
        (
            [small],
            SONNET,
            Target(kind="anthropic", model=SONNET),
            ("compaction", "outgrew_local_model"),
        ),
        ([small], None, None, ("compaction", "outgrew_local_model", "no_target_configured")),
    ):
        run = Run(
            config(
                models=models, main={"share": 1.0, "model": "coder-small"}, main_target=main_target
            )
        )
        turn, _ = run(BUGFIX)
        assert turn.target == Target(kind="local", model="coder-small")
        plan, _ = run(COMPACTION)
        assert (plan.target, plan.reasons) == (target, reasons), models


def test_through_the_gateway_the_larger_model_answers(tmp_path: Path) -> None:
    cfg = config(models=[SMALL_TOOLS, LARGE], subagents={"share": 1.0, "model": "coder-small"})
    with gateway(tmp_path, cfg) as gw:
        gw.inference.pieces = ["Done."]
        gw.send(MAIN)
        gw.send(SPAWN)
        gw.send(LOOP)
    assert [r["model"] for r in gw.inference.requests] == ["coder-small", "coder-large"]
    grown = gw.seen[-1]
    assert grown.decision.applied and grown.decision.target == LOCAL_LARGE
    assert grown.decision.reasons == ["outgrew_local_model"]
