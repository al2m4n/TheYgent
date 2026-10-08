"""The conversation tracker on real Claude Code traffic, with a manual clock.

Where a request sits decides whether a model switch is free (the cache is cold anyway) or would
throw a warm cache away; these tests pin each boundary, the cache lifetimes Claude Code asks for,
and that the tracker's "tokens left cached" estimate is what the next request actually reads.
"""

from __future__ import annotations

import copy
import json
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _fixtures import Exchange, load
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.conversations import ConversationTracker, Position
from theygent_ai_gateway.identity import Identity
from theygent_ai_gateway.observe import Observation, ResponseObserver, ResponseSummary
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.signals import RequestSignals, read_request

WAIT_SEC = 10.0


class ManualClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def signals_of(exchange: Exchange, **header_overrides: str | None) -> RequestSignals:
    headers = dict(exchange.request_headers)
    for name, value in header_overrides.items():
        name = name.replace("_", "-")
        if value is None:
            headers.pop(name, None)
        else:
            headers[name] = value
    return read_request(headers, exchange.request_bytes(), has_messages_body=True)


def summary_of(exchange: Exchange) -> ResponseSummary:
    observer = ResponseObserver(exchange.response_header("content-type"))
    observer.feed(exchange.response_bytes())
    return observer.finish()


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def tracker(clock: ManualClock) -> ConversationTracker:
    return ConversationTracker(clock=clock)


def run(tracker: ConversationTracker, exchange: Exchange, **overrides: str | None) -> Position:
    position = tracker.begin(signals_of(exchange, **overrides), None)
    tracker.complete(position, exchange.status, summary_of(exchange))
    return position


# ── the main conversation ────────────────────────────────────────────────────


def test_a_conversation_starts_cold_and_continues_warm(
    tracker: ConversationTracker, clock: ManualClock
) -> None:
    first = run(tracker, load("main-bugfix-1"))
    clock.advance(3)
    second = run(tracker, load("main-bugfix-2"))
    clock.advance(3)
    third = run(tracker, load("main-bugfix-3"))
    assert (first.boundary, first.kind, first.cold) == ("conversation_start", "main", True)
    assert (second.boundary, second.cache_warm, second.cold) == ("continuation", True, False)
    assert third.boundary == "continuation"
    assert first.segment == second.segment == third.segment == 0
    assert second.ttl_seconds == 3600  # Claude Code asks for one hour on the main loop


def test_the_cached_prefix_estimate_is_what_the_next_request_reads(
    tracker: ConversationTracker, clock: ManualClock
) -> None:
    run(tracker, load("main-bugfix-1"))
    clock.advance(3)
    second = run(tracker, load("main-bugfix-2"))
    assert second.cached_prefix_tokens == summary_of(load("main-bugfix-2")).usage.cache_read_tokens
    assert second.cached_prefix_tokens == 28514


def test_a_new_prompt_with_a_warm_cache_is_a_costly_boundary(
    tracker: ConversationTracker, clock: ManualClock
) -> None:
    run(tracker, load("main-bugfix-1"))
    clock.advance(120)
    position = run(tracker, load("main-bugfix-2"), x_claude_code_prompt_id="another-prompt")
    assert (position.boundary, position.cache_warm, position.cold) == ("new_prompt", True, False)
    assert position.cached_prefix_tokens > 0
    assert position.segment == 1


def test_the_main_cache_outlives_ten_minutes_but_not_an_hour(
    tracker: ConversationTracker, clock: ManualClock
) -> None:
    run(tracker, load("main-bugfix-1"))
    clock.advance(600)
    assert run(tracker, load("main-bugfix-2")).boundary == "continuation"
    clock.advance(3601)
    expired = run(tracker, load("main-bugfix-3"))
    assert (expired.boundary, expired.cache_warm, expired.cold) == ("cache_expired", False, True)
    assert expired.cached_prefix_tokens == 0


def test_compaction_restarts_the_conversation_cold(
    tracker: ConversationTracker, clock: ManualClock
) -> None:
    run(tracker, load("main-bugfix-1"))
    clock.advance(5)
    compaction = run(tracker, load("compaction-manual"))
    assert compaction.kind == "main"  # compaction reads the main conversation's cache
    clock.advance(5)
    after = run(tracker, load("main-bugfix-2"), x_claude_code_context_compacted="manual")
    assert (after.boundary, after.cache_warm, after.cold) == ("after_compaction", False, True)


# ── subagents ────────────────────────────────────────────────────────────────


def test_a_subagent_is_its_own_conversation_starting_cold(
    tracker: ConversationTracker, clock: ManualClock
) -> None:
    run(tracker, load("main-explore-1"))
    clock.advance(2)
    spawn = run(tracker, load("subagent-explore-1"))
    clock.advance(2)
    second = run(tracker, load("subagent-explore-2"))
    assert (spawn.boundary, spawn.kind) == ("conversation_start", "subagent")
    assert spawn.key is not None and spawn.key[2] != "main"
    assert second.boundary == "continuation"
    assert second.ttl_seconds == 300  # the five-minute default on subagents
    assert (
        second.cached_prefix_tokens
        == summary_of(load("subagent-explore-2")).usage.cache_read_tokens
    )
    assert len(tracker) == 2


def test_a_subagent_cache_is_gone_after_ten_minutes(
    tracker: ConversationTracker, clock: ManualClock
) -> None:
    run(tracker, load("subagent-explore-1"))
    clock.advance(600)
    assert run(tracker, load("subagent-explore-2")).boundary == "cache_expired"


# ── side requests ────────────────────────────────────────────────────────────


def _side_request_sharing(main: Exchange) -> RequestSignals:
    """A side request that starts with the main conversation's first message (the shape of
    Claude Code's cache-riding side requests)."""
    side = load("auxiliary-with-safeguards")
    body = copy.deepcopy(side.body)
    body["messages"][0] = main.body["messages"][0]
    headers = dict(side.request_headers)
    headers["x-claude-code-session-id"] = main.request_header("x-claude-code-session-id") or ""
    return read_request(headers, json.dumps(body).encode(), has_messages_body=True)


def test_a_side_request_riding_the_main_cache_is_recognised(tracker: ConversationTracker) -> None:
    main = load("main-bugfix-1")
    run(tracker, main)
    position = tracker.begin(_side_request_sharing(main), None)
    assert (position.boundary, position.shares_main_prefix) == ("side_request", True)


def test_a_standalone_side_request_does_not_ride_the_main_cache(
    tracker: ConversationTracker,
) -> None:
    main = load("main-bugfix-1")
    run(tracker, main)
    title = signals_of(
        load("auxiliary-session-title"),
        x_claude_code_session_id=main.request_header("x-claude-code-session-id"),
    )
    assert tracker.begin(title, None).shares_main_prefix is False


def test_without_a_main_conversation_sharing_is_unknown(tracker: ConversationTracker) -> None:
    assert (
        tracker.begin(signals_of(load("auxiliary-session-title")), None).shares_main_prefix is None
    )
    assert len(tracker) == 0  # side requests are not conversations


# ── identity, untracked, and bounds ──────────────────────────────────────────


def test_requests_without_a_session_are_untracked(tracker: ConversationTracker) -> None:
    position = tracker.begin(signals_of(load("main-bugfix-1"), x_claude_code_session_id=None), None)
    assert position.boundary == "untracked"
    assert len(tracker) == 0


def test_developers_do_not_share_conversations(tracker: ConversationTracker) -> None:
    signals = signals_of(load("main-bugfix-1"))
    alice = Identity("usr_alice", "key_a", "passthrough")
    bob = Identity("usr_bob", "key_b", "passthrough")
    assert tracker.begin(signals, alice).boundary == "conversation_start"
    assert tracker.begin(signals, bob).boundary == "conversation_start"
    assert len(tracker) == 2


def test_failed_requests_leave_the_cache_estimate_alone(
    tracker: ConversationTracker, clock: ManualClock
) -> None:
    first = tracker.begin(signals_of(load("main-bugfix-1")), None)
    tracker.complete(first, 529, summary_of(load("main-bugfix-1")))
    clock.advance(3)
    assert tracker.begin(signals_of(load("main-bugfix-2")), None).cached_prefix_tokens == 0


def test_memory_is_bounded(clock: ManualClock) -> None:
    tracker = ConversationTracker(clock=clock, max_conversations=3)
    base = load("main-bugfix-1")
    for n in range(5):
        tracker.begin(signals_of(base, x_claude_code_session_id=f"session-{n}"), None)
    assert len(tracker) == 3
    # The oldest were forgotten: session-0 starts over.
    position = tracker.begin(signals_of(base, x_claude_code_session_id="session-0"), None)
    assert position.boundary == "conversation_start"


def test_idle_conversations_are_forgotten(clock: ManualClock) -> None:
    tracker = ConversationTracker(clock=clock, idle_seconds=7200)
    tracker.begin(signals_of(load("main-bugfix-1")), None)
    clock.advance(7201)
    tracker.begin(signals_of(load("subagent-explore-1")), None)
    assert len(tracker) == 1


# ── through the gateway ──────────────────────────────────────────────────────


@pytest.fixture
def upstream() -> Iterator[FakeAnthropic]:
    with fake_anthropic() as state:
        yield state


def _wait_for(seen: list[Observation], count: int) -> None:
    deadline = time.monotonic() + WAIT_SEC
    while len(seen) < count:
        assert time.monotonic() < deadline, f"expected {count} observations, got {len(seen)}"
        time.sleep(0.01)


def test_positions_reach_the_observation(upstream: FakeAnthropic, tmp_path: Path) -> None:
    seen: list[Observation] = []
    settings = GatewaySettings(
        upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
    )
    with serve(create_app(settings, observe=seen.append)) as url:
        for count, name in enumerate(("main-bugfix-1", "main-bugfix-2", "main-bugfix-3"), 1):
            exchange = load(name)
            httpx.request(
                exchange.method,
                f"{url}{exchange.target}",
                content=exchange.request_bytes(),
                headers=[*exchange.request_headers, ("x-fake-fixture", name)],
                timeout=WAIT_SEC,
            )
            _wait_for(seen, count)  # one at a time, as Claude Code sends them
        httpx.get(f"{url}/v1/models", timeout=WAIT_SEC)
        _wait_for(seen, 4)
    boundaries = [o.position.boundary if o.position else None for o in seen]
    assert boundaries == ["conversation_start", "continuation", "continuation", None]
    assert seen[1].position is not None and seen[1].position.cached_prefix_tokens == 28514
