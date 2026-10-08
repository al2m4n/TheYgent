"""Response signals and the per-exchange observation.

The parser reads real recorded streams cut at arbitrary byte boundaries, and hostile input; it
must never raise. Through the gateway, every exchange yields exactly one observation with the
right outcome — and neither a failing parser nor a failing hook changes a byte of what is relayed.
"""

from __future__ import annotations

import random
import socket
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _fixtures import Exchange, all_exchanges, load
from theygent_ai_gateway import observe as observe_module
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.observe import (
    MAX_JSON_BODY,
    MAX_PENDING_EVENT,
    Observation,
    ResponseObserver,
    seat_allowance,
    to_usage,
)
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.signals import body_signals
from theygent_ir.gateway import Usage

EXCHANGES = all_exchanges()
WAIT_SEC = 10.0


def _summarise(exchange: Exchange, chunks: list[bytes] | None = None):
    observer = ResponseObserver(exchange.response_header("content-type"))
    for chunk in chunks if chunks is not None else [exchange.response_bytes()]:
        observer.feed(chunk)
    return observer.finish()


def _split(data: bytes, seed: int) -> list[bytes]:
    rng = random.Random(seed)
    cuts = sorted(rng.sample(range(1, len(data)), k=min(40, len(data) - 1)))
    return [data[a:b] for a, b in zip([0, *cuts], [*cuts, len(data)], strict=True)]


# ── the parser on real streams ───────────────────────────────────────────────


@pytest.mark.parametrize("exchange", EXCHANGES, ids=lambda e: e.name)
def test_usage_model_and_stop_reason_are_read(exchange: Exchange) -> None:
    summary = _summarise(exchange)
    assert summary.parse_error is None
    if not exchange.streaming:
        return
    events = exchange.sse()
    start = events[0][1]["message"]
    assert summary.model == start["model"]
    merged = dict(start["usage"])
    for name, data in events:
        if name == "message_delta":
            merged.update(data.get("usage") or {})
    assert summary.usage == to_usage(merged)
    if exchange.abort_after_events is None:
        assert summary.stop_reason in ("end_turn", "tool_use")
    else:
        assert summary.stop_reason is None


@pytest.mark.parametrize("exchange", [e for e in EXCHANGES if e.streaming], ids=lambda e: e.name)
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_chunk_boundaries_do_not_matter(exchange: Exchange, seed: int) -> None:
    whole = _summarise(exchange)
    assert _summarise(exchange, _split(exchange.response_bytes(), seed)) == whole


def test_single_byte_chunks_and_crlf_line_endings() -> None:
    exchange = load("main-bugfix-1")
    whole = _summarise(exchange)
    crlf = exchange.response_bytes().replace(b"\n", b"\r\n")
    assert _summarise(exchange, [crlf[i : i + 1] for i in range(len(crlf))]) == whole


def test_cache_writes_are_split_by_ttl_on_real_subscription_traffic() -> None:
    # Claude Code marks the main conversation for 1-hour caching and subagents for the 5-minute
    # default; the upstream bills each at its own rate.
    assert _summarise(load("subagent-explore-1")).usage == Usage(
        input_tokens=2, output_tokens=188, cache_read_tokens=0, cache_write_5m_tokens=22987
    )
    main = _summarise(load("main-bugfix-1")).usage
    assert main.cache_write_1h_tokens == 18618 and main.cache_write_5m_tokens == 0


@pytest.mark.parametrize("exchange", [e for e in EXCHANGES if e.streaming], ids=lambda e: e.name)
def test_billed_cache_ttl_follows_the_requests_markers(exchange: Exchange) -> None:
    usage = _summarise(exchange).usage
    signals = body_signals(exchange.request_bytes())
    assert signals is not None
    marked = set(signals.cache_ttls)
    if usage.cache_write_1h_tokens:
        assert "1h" in marked
    if usage.cache_write_5m_tokens:
        assert "5m" in marked


def test_the_opus_and_sonnet_runs_add_up_to_the_measured_totals() -> None:
    def totals(prefix: str) -> tuple[int, int, int]:
        usages = [_summarise(load(f"{prefix}-{n}")).usage for n in (1, 2, 3)]
        return (
            sum(u.cache_write_1h_tokens + u.cache_write_5m_tokens for u in usages),
            sum(u.cache_read_tokens for u in usages),
            sum(u.output_tokens for u in usages),
        )

    # The Phase 0 census figures behind "subagent on Sonnet costs 48% less".
    assert totals("subagent-explore") == (26038, 48606, 1106)
    assert totals("subagent-explore-sonnet") == (25910, 48621, 914)


def test_tool_calls_are_counted() -> None:
    summaries = [_summarise(load(f"main-bugfix-{n}")) for n in (1, 2, 3)]
    assert [s.stop_reason for s in summaries][-1] == "end_turn"
    assert all(s.tool_use_count >= 1 for s in summaries if s.stop_reason == "tool_use")


@pytest.mark.parametrize(
    ("name", "error_type"),
    [
        ("subagent-haiku-adaptive-400", "invalid_request_error"),
        ("main-expired-login-401", "authentication_error"),
        ("auxiliary-quota-check-429", "rate_limit_error"),
    ],
)
def test_error_types_are_read_and_messages_are_not_kept(name: str, error_type: str) -> None:
    summary = _summarise(load(name))
    assert summary.error_type == error_type
    assert "message" not in summary.__dataclass_fields__


def test_usage_without_a_ttl_breakdown_counts_as_five_minute_writes() -> None:
    assert to_usage({"cache_creation_input_tokens": 500, "output_tokens": 7}) == Usage(
        output_tokens=7, cache_write_5m_tokens=500
    )


def test_usage_ignores_negative_and_non_numeric_values() -> None:
    assert to_usage({"input_tokens": -4, "output_tokens": "lots", "cache_creation": "x"}) == Usage()


# ── seat allowance ───────────────────────────────────────────────────────────


def test_seat_allowance_from_a_subscription_response() -> None:
    allowance = seat_allowance(dict(load("main-bugfix-1").response_headers))
    assert allowance is not None
    assert allowance.five_hour_utilization == 0.31
    assert allowance.seven_day_utilization == 0.05
    assert allowance.five_hour_resets_at == datetime.fromtimestamp(1791076800, UTC)
    assert allowance.status == "allowed"
    assert allowance.representative_claim == "five_hour"
    assert allowance.overage_status == "rejected"


def test_no_seat_allowance_without_the_headers() -> None:
    assert seat_allowance({"content-type": "application/json"}) is None


def test_malformed_seat_headers_are_dropped_not_raised() -> None:
    allowance = seat_allowance(
        {
            "anthropic-ratelimit-unified-5h-utilization": "high",
            "anthropic-ratelimit-unified-7d-utilization": "-0.2",
            "anthropic-ratelimit-unified-5h-reset": "tomorrow",
            "anthropic-ratelimit-unified-7d-reset": "99999999999999999999",
            "anthropic-ratelimit-unified-status": "not a token",
        }
    )
    assert allowance is not None
    assert allowance.five_hour_utilization is None
    assert allowance.seven_day_utilization is None
    assert allowance.five_hour_resets_at is None
    assert allowance.seven_day_resets_at is None
    assert allowance.status is None


# ── hostile input never raises ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("chunks", "error"),
    [
        ([b"event: message_start\ndata: {not json}\n\n"], "parse:JSONDecodeError"),
        ([b"event: x\ndata: \xff\xfe\n\n"], "parse:UnicodeDecodeError"),
        ([b"data: " + b"a" * (MAX_PENDING_EVENT + 10)], "event_too_large"),
    ],
)
def test_bad_streams_mark_a_parse_error(chunks: list[bytes], error: str) -> None:
    observer = ResponseObserver("text/event-stream")
    for chunk in chunks:
        observer.feed(chunk)
    observer.feed(b"event: message_stop\ndata: {}\n\n")  # ignored once failed
    assert observer.finish().parse_error == error


def test_an_oversized_json_body_is_not_buffered() -> None:
    observer = ResponseObserver("application/json")
    observer.feed(b"{" + b" " * MAX_JSON_BODY)
    assert observer.finish().parse_error == "body_too_large"


def test_a_compressed_body_is_not_parsed() -> None:
    observer = ResponseObserver("text/event-stream", "gzip")
    observer.feed(b"\x1f\x8b binary")
    assert observer.finish().parse_error == "encoded_body"


def test_non_object_payloads_are_ignored() -> None:
    observer = ResponseObserver("text/event-stream")
    observer.feed(b"event: ping\ndata: [1, 2]\n\nevent: x\ndata: 3\n\n: comment line\n\n")
    summary = observer.finish()
    assert summary.parse_error is None
    assert summary.usage == Usage()


# ── observations through the gateway ─────────────────────────────────────────


class Collector:
    def __init__(self) -> None:
        self.observations: list[Observation] = []
        self._arrived = threading.Condition()

    def __call__(self, observation: Observation) -> None:
        with self._arrived:
            self.observations.append(observation)
            self._arrived.notify_all()

    def wait_for(self, count: int) -> list[Observation]:
        with self._arrived:
            assert self._arrived.wait_for(lambda: len(self.observations) >= count, WAIT_SEC)
            return list(self.observations)


@pytest.fixture
def upstream() -> Iterator[FakeAnthropic]:
    with fake_anthropic() as state:
        yield state


@pytest.fixture
def collector() -> Collector:
    return Collector()


def _gateway(upstream_url: str, tmp_path: Path, hook) -> Iterator[str]:
    settings = GatewaySettings(upstream_url=upstream_url, gateway_id="gw-test", state_dir=tmp_path)
    with serve(create_app(settings, observe=hook)) as url:
        yield url


@pytest.fixture
def gateway(upstream: FakeAnthropic, tmp_path: Path, collector: Collector) -> Iterator[str]:
    yield from _gateway(upstream.base_url, tmp_path, collector)


def _replay(gateway: str, exchange: Exchange) -> httpx.Response:
    return httpx.request(
        exchange.method,
        f"{gateway}{exchange.target}",
        content=exchange.request_bytes(),
        headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
        timeout=WAIT_SEC,
    )


def test_a_completed_exchange_is_observed_once(gateway: str, collector: Collector) -> None:
    exchange = load("subagent-explore-1")
    assert _replay(gateway, exchange).content == exchange.response_bytes()
    [observation] = collector.wait_for(1)
    assert observation.outcome == "complete"
    assert observation.endpoint == "messages"
    assert observation.status == 200
    assert observation.request.hints.request_class == "subagent"
    assert observation.request.hints.agent_type == "Explore"
    assert observation.request.client.client == "sdk-cli"
    assert observation.request.credential == "oauth"
    assert observation.request.body is not None
    assert observation.response.usage.cache_write_5m_tokens == 22987
    assert observation.seat_allowance is not None
    timing = observation.timing
    assert timing.headers_ms is not None and timing.first_byte_ms is not None
    assert timing.headers_ms <= timing.first_byte_ms <= (timing.total_ms or 0)
    assert observation.started_at.tzinfo is not None


def test_an_error_response_is_observed(gateway: str, collector: Collector) -> None:
    _replay(gateway, load("subagent-haiku-max-tokens-400"))
    [observation] = collector.wait_for(1)
    assert observation.status == 400
    assert observation.outcome == "complete"
    assert observation.response.error_type == "invalid_request_error"


def test_an_upstream_drop_is_observed(gateway: str, collector: Collector) -> None:
    with pytest.raises(httpx.RemoteProtocolError):
        _replay(gateway, load("main-dropped-mid-stream"))
    [observation] = collector.wait_for(1)
    assert observation.outcome == "upstream_broke"
    assert observation.response.events == 3


def _hang_up(gateway: str, upstream: FakeAnthropic, scenario: str) -> None:
    url = urlsplit(gateway)
    assert url.hostname is not None and url.port is not None
    body = b'{"model":"claude-opus-5-5","stream":true,"messages":[]}'
    sock = socket.create_connection((url.hostname, url.port), timeout=WAIT_SEC)
    try:
        sock.sendall(
            b"POST /v1/messages HTTP/1.1\r\n"
            + f"Host: {url.netloc}\r\nx-fake-scenario: {scenario}\r\n".encode()
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )
        assert upstream.hold_started.wait(WAIT_SEC)
    finally:
        sock.close()


@pytest.mark.parametrize(("scenario", "status"), [("hold-stream", 200), ("hold-headers", None)])
def test_a_client_hang_up_is_observed(
    gateway: str, upstream: FakeAnthropic, collector: Collector, scenario: str, status: int | None
) -> None:
    _hang_up(gateway, upstream, scenario)
    [observation] = collector.wait_for(1)
    assert observation.outcome == "client_disconnected"
    assert observation.status == status


def test_an_unreachable_upstream_is_observed(tmp_path: Path, collector: Collector) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    for url in _gateway(f"http://127.0.0.1:{closed}", tmp_path, collector):
        httpx.post(f"{url}/v1/messages", content=b"{}", timeout=WAIT_SEC)
    [observation] = collector.wait_for(1)
    assert observation.outcome == "transport_error"
    assert observation.status is None


def test_endpoints_without_a_messages_body_are_observed(gateway: str, collector: Collector) -> None:
    httpx.get(f"{gateway}/v1/models", timeout=WAIT_SEC)
    [observation] = collector.wait_for(1)
    assert observation.endpoint == "models"
    assert observation.request.body is None
    assert observation.request.parse_errors == ()


def test_a_failing_hook_never_changes_the_response(
    upstream: FakeAnthropic, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    def broken(_: Observation) -> None:
        raise RuntimeError("the log is full")

    exchange = load("main-bugfix-2")
    for url in _gateway(upstream.base_url, tmp_path, broken):
        response = _replay(url, exchange)
    assert response.content == exchange.response_bytes()
    assert "the observe hook failed: RuntimeError" in caplog.text


def test_a_failing_parser_never_changes_the_response(
    gateway: str, collector: Collector, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(self: ResponseObserver, chunk: bytes) -> None:
        raise MemoryError("parser trouble")

    monkeypatch.setattr(observe_module.ResponseObserver, "_feed_sse", explode)
    exchange = load("main-bugfix-3")
    assert _replay(gateway, exchange).content == exchange.response_bytes()
    [observation] = collector.wait_for(1)
    assert observation.outcome == "complete"
    assert observation.response.parse_error == "parse:MemoryError"
