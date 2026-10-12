"""The recorded Claude Code exchanges: they cover the traffic the gateway must handle, they are
internally sound, they carry no content, and each one passes through the gateway unchanged.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _fixtures import FIXTURE_DIR, Exchange, all_exchanges, load
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.settings import GatewaySettings

EXCHANGES = all_exchanges()
NAMES = [e.name for e in EXCHANGES]
WAIT_SEC = 10.0


# ── coverage ─────────────────────────────────────────────────────────────────


def test_every_request_class_is_covered() -> None:
    classes = {e.request_class for e in EXCHANGES}
    assert {"main", "subagent", "auxiliary", "compaction"} <= classes


def test_every_failure_the_gateway_must_relay_is_covered() -> None:
    statuses = {e.status for e in EXCHANGES}
    assert {200, 400, 401, 429} <= statuses
    assert any(e.abort_after_events for e in EXCHANGES), "an upstream dropping mid-stream"


def test_the_known_side_request_kinds_are_covered() -> None:
    assert load("auxiliary-session-title").body["output_config"]["format"]
    assert load("auxiliary-quota-check-429").body["max_tokens"] == 1
    assert "safeguards" in load("auxiliary-with-safeguards").body


def test_tool_search_off_and_on_are_both_covered() -> None:
    off, on = load("main-tool-search-off"), load("main-tool-search-on")
    assert len(off.body["tools"]) > 100
    assert not any(t.get("defer_loading") for t in off.body["tools"])
    assert any(t.get("defer_loading") for t in on.body["tools"])


def test_the_opus_and_sonnet_runs_of_the_same_subagent_work_are_covered() -> None:
    def served(prefix: str) -> set[str]:
        return {
            e.sse()[0][1]["message"]["model"]
            for e in EXCHANGES
            if e.name.startswith(prefix) and e.streaming
        }

    assert served("subagent-explore-sonnet") == {"claude-sonnet-5-5"}
    assert served("subagent-explore-") - {"claude-sonnet-5-5"} == {"claude-opus-5-5"}


# ── each recording is sound ──────────────────────────────────────────────────


@pytest.mark.parametrize("name", NAMES)
def test_streams_are_complete_unless_recorded_as_dropped(name: str) -> None:
    exchange = load(name)
    if not exchange.streaming:
        assert exchange.json_body is not None
        return
    kinds = [event for event, _ in exchange.sse()]
    assert kinds[0] == "message_start"
    if exchange.abort_after_events is None:
        assert kinds[-1] == "message_stop"
        assert "message_delta" in kinds
    else:
        assert len(kinds) == exchange.abort_after_events
        assert "message_stop" not in kinds


@pytest.mark.parametrize("name", NAMES)
def test_streamed_tool_inputs_are_valid_json(name: str) -> None:
    # Scrubbed tool inputs are sent whole in the first fragment of each block; the concatenation
    # of a block's fragments must still parse.
    fragments: dict[int, str] = {}
    for _, data in load(name).sse():
        delta = data.get("delta") or {}
        if delta.get("type") == "input_json_delta":
            fragments[data["index"]] = fragments.get(data["index"], "") + delta["partial_json"]
    for joined in fragments.values():
        json.loads(joined)


@pytest.mark.parametrize("name", NAMES)
def test_successful_streams_report_usage(name: str) -> None:
    exchange = load(name)
    if not exchange.streaming or exchange.status != 200:
        return
    usage = exchange.sse()[0][1]["message"]["usage"]
    assert {"input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"} <= set(usage)


def test_requests_from_one_session_share_its_id() -> None:
    by_conversation: dict[str, set[str | None]] = {}
    for exchange in EXCHANGES:
        if exchange.source == "derived":
            continue
        session = exchange.request_header("x-claude-code-session-id")
        by_conversation.setdefault(exchange.conversation, set()).add(session)
    for conversation, sessions in by_conversation.items():
        assert len(sessions) == 1, f"{conversation} spans sessions {sessions}"


def test_a_subagents_requests_share_its_agent_id() -> None:
    agents = {
        load(f"subagent-explore-{n}").request_header("x-claude-code-agent-id") for n in (1, 2, 3)
    }
    assert len(agents) == 1 and None not in agents


def test_hint_headers_are_kept() -> None:
    assert load("subagent-explore-1").request_header("x-claude-code-agent-type") == "Explore"
    assert load("compaction-manual").request_header("x-claude-code-compaction") == "manual"
    for exchange in EXCHANGES:
        assert exchange.request_header("x-claude-code-request-class") == exchange.request_class


def test_equal_content_keeps_equal_placeholders() -> None:
    # Turns of one conversation repeat its first message; the placeholder must repeat with it,
    # or "this request continues that conversation" could not be recognised from fixtures.
    first = [load(f"main-bugfix-{n}").body["messages"][0] for n in (1, 2, 3)]
    assert first[0] == first[1] == first[2]


# ── no content ───────────────────────────────────────────────────────────────

_FIXTURE_UUID = "00000000-0000-4000-8000-"
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_LEAKS = {
    "an absolute home or temp path": re.compile(
        r"(^|[\s\"'=(])(/Users/|/home/|/private/|/var/folders/|[A-Za-z]:\\)"
    ),
    "an email address": re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}"),
    "a real-looking API credential": re.compile(r"sk-ant-[a-z]+\d*-(?!fixture\b)[A-Za-z0-9_-]+"),
    "Claude Code's own prompt text": re.compile(r"You are |Claude Code"),
}


def _strings(node: Any) -> Iterator[str]:
    """Every string in a recorded exchange — keys too, and inside each SSE event's data."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, str):
        if node.startswith("event: ") and "\ndata: " in node:
            yield from _strings(json.loads(node.split("\ndata: ", 1)[1]))
        else:
            yield node


@pytest.mark.parametrize("path", sorted(FIXTURE_DIR.glob("*.json")), ids=lambda p: p.stem)
def test_fixtures_carry_no_user_content_or_proprietary_text(path: Path) -> None:
    raw = json.loads(path.read_text())
    found: list[str] = []
    for text in _strings({"request": raw["request"], "response": raw["response"]}):
        for what, pattern in _LEAKS.items():
            if pattern.search(text):
                found.append(f"{what} ({len(text)} chars)")
        found += [
            f"a non-fixture id {u[:8]}…"
            for u in _UUID.findall(text)
            if not u.startswith(_FIXTURE_UUID)
        ]
        if len(text) > 200 and not text.startswith("claude-code-20250219"):
            found.append(f"a {len(text)}-character string (prose?)")
    assert found == []


def test_the_leak_check_would_catch_a_leak() -> None:
    leaky = {"messages": [{"content": "see /Users/someone/project and mail me@example.com"}]}
    hits = [w for t in _strings(leaky) for w, p in _LEAKS.items() if p.search(t)]
    assert set(hits) == {"an absolute home or temp path", "an email address"}


# ── every recording passes through the gateway unchanged ─────────────────────


@pytest.fixture(scope="module")
def upstream() -> Iterator[FakeAnthropic]:
    with fake_anthropic() as state:
        yield state


@pytest.fixture(scope="module")
def gateway(upstream: FakeAnthropic, tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    settings = GatewaySettings(
        upstream_url=upstream.base_url,
        gateway_id="gw-test",
        state_dir=tmp_path_factory.mktemp("state"),
    )
    with serve(create_app(settings)) as url:
        yield url


def _send(gateway: str, exchange: Exchange) -> httpx.Response:
    return httpx.request(
        exchange.method,
        f"{gateway}{exchange.target}",
        content=exchange.request_bytes(),
        headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
        timeout=WAIT_SEC,
    )


@pytest.mark.parametrize("name", [n for n in NAMES if load(n).abort_after_events is None])
def test_each_recorded_exchange_passes_through_unchanged(
    gateway: str, upstream: FakeAnthropic, name: str
) -> None:
    exchange = load(name)
    response = _send(gateway, exchange)
    sent = hashlib.sha256(exchange.request_bytes()).hexdigest()
    received = next(r for r in reversed(upstream.requests) if r.header("x-fake-fixture") == [name])
    assert hashlib.sha256(received.body).hexdigest() == sent
    assert received.query == exchange.query
    assert response.status_code == exchange.status
    assert response.content == exchange.response_bytes()
    for header, value in exchange.response_headers:
        assert response.headers.get_list(header)[-1] == value


def test_a_recorded_mid_stream_drop_reaches_the_client_as_a_drop(gateway: str) -> None:
    with pytest.raises(httpx.RemoteProtocolError):
        _send(gateway, load("main-dropped-mid-stream"))
