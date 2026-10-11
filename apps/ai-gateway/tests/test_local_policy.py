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
  so; observer mode only records it.
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
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _fake_inference import FakeInference, fake_inference
from _fixtures import Exchange, load
from _prices import FROZEN_PRICES
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.conversations import ConversationTracker
from theygent_ai_gateway.observe import Observation, ResponseObserver
from theygent_ai_gateway.policy import Plan, Policy, in_share
from theygent_ai_gateway.routing import with_model
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.signals import read_request
from theygent_ir.gateway import GatewayConfig, GatewayMode, PolicyConfig, Target

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


def test_a_partial_share_decides_by_the_conversation() -> None:
    # Where this subagent's conversation falls in [0, 1): a share just above takes it local, one
    # just below leaves it with Anthropic.
    run = Run(config(subagents={"share": 1.0}))
    run(MAIN)
    _, (_, position) = run(SPAWN)
    low, high = 0.0, 1.0
    for _ in range(40):
        middle = (low + high) / 2
        low, high = (low, middle) if in_share(position.key, middle) else (middle, high)
    for share, local in ((min(high + 0.01, 1.0), True), (max(low - 0.01, 0.0), False)):
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
    assert len(gw.inference.requests) == 1 and after.decision.target.model == SONNET  # type: ignore[union-attr]


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
