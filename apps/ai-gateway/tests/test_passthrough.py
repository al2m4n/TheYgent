"""Exact pass-through: what Claude Code sends reaches the upstream unchanged, and what the upstream
sends comes back unchanged, in real time.

Both the gateway and the fake upstream run on real uvicorn: only real sockets show streaming
timing, a client hanging up mid-request, and an upstream breaking mid-response.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from _fake_anthropic import STREAM_BYTES, FakeAnthropic, fake_anthropic, serve
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.proxy import GATEWAY_ERROR_HEADER, classify
from theygent_ai_gateway.settings import GatewaySettings

WAIT_SEC = 10.0

#: A request body as Claude Code sends it, plus the things a re-serialization would change:
#: non-ASCII text, unusual spacing, keys not in sorted order, a trailing newline.
RAW_BODY = (
    '{"model":"claude-opus-5-5", "max_tokens":128000,"stream":true,\n'
    '  "system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.285.9"},'
    '{"type":"text","text":"Ünïcödé — and  two spaces","cache_control":{"type":"ephemeral"}}],'
    '"messages":[{"role":"user","content":"fix calc.py ✓"}],"thinking":{"type":"adaptive"}}\n'
).encode()


@pytest.fixture
def upstream() -> Iterator[FakeAnthropic]:
    with fake_anthropic() as state:
        yield state


def _gateway_url(upstream_url: str, tmp_path: Path) -> Iterator[str]:
    settings = GatewaySettings(
        upstream_url=upstream_url, gateway_id="gw-test", state_dir=tmp_path / "state"
    )
    with serve(create_app(settings)) as url:
        yield url


@pytest.fixture
def gateway(upstream: FakeAnthropic, tmp_path: Path) -> Iterator[str]:
    yield from _gateway_url(upstream.base_url, tmp_path)


@pytest.fixture
def http(gateway: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=gateway, timeout=WAIT_SEC) as client:
        yield client


def _post_messages(http: httpx.Client, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return http.post(
        "/v1/messages?beta=true",
        content=RAW_BODY,
        headers={"content-type": "application/json", **(headers or {})},
    )


# ── the request reaches the upstream unchanged ───────────────────────────────


def test_request_body_reaches_upstream_byte_for_byte(
    http: httpx.Client, upstream: FakeAnthropic
) -> None:
    _post_messages(http)
    received = upstream.last().body
    assert hashlib.sha256(received).hexdigest() == hashlib.sha256(RAW_BODY).hexdigest()


def test_path_and_query_are_kept(http: httpx.Client, upstream: FakeAnthropic) -> None:
    _post_messages(http)
    assert upstream.last().raw_path == "/v1/messages"
    assert upstream.last().query == "beta=true"


def test_headers_are_forwarded_as_an_open_list(http: httpx.Client, upstream: FakeAnthropic) -> None:
    http.post(
        "/v1/messages?beta=true",
        content=RAW_BODY,
        headers=[
            ("content-type", "application/json"),
            ("anthropic-version", "2023-06-01"),
            ("anthropic-beta", "claude-code-20250219,a-capability-from-the-future-2027-01-01"),
            ("anthropic-some-new-header", "kept"),
            ("x-claude-code-request-class", "main"),
            ("x-claude-code-session-id", "9c92c12e-b929-40ab-94b5-55e4ff08ba54"),
            ("x-stainless-retry-count", "0"),
            ("x-dup", "first"),
            ("x-dup", "second"),
        ],
    )
    seen = upstream.last()
    assert seen.header("anthropic-version") == ["2023-06-01"]
    assert seen.header("anthropic-beta") == [
        "claude-code-20250219,a-capability-from-the-future-2027-01-01"
    ]
    assert seen.header("anthropic-some-new-header") == ["kept"]
    assert seen.header("x-claude-code-request-class") == ["main"]
    assert seen.header("x-stainless-retry-count") == ["0"]
    assert seen.header("x-dup") == ["first", "second"]  # duplicates and their order kept


def test_credentials_for_the_upstream_are_forwarded(
    http: httpx.Client, upstream: FakeAnthropic
) -> None:
    # Personal mode and subscription seats pass the caller's own credential through.
    _post_messages(
        http,
        headers={"authorization": "Bearer sk-ant-oat01-example", "x-api-key": "sk-ant-api03-ex"},
    )
    assert upstream.last().header("authorization") == ["Bearer sk-ant-oat01-example"]
    assert upstream.last().header("x-api-key") == ["sk-ant-api03-ex"]


def test_the_gateway_key_is_never_forwarded(http: httpx.Client, upstream: FakeAnthropic) -> None:
    _post_messages(http, headers={"X-TheYgent-Gateway-Key": "tyg_example_key"})
    assert upstream.last().header("x-theygent-gateway-key") == []


def test_upstream_compression_is_turned_off(http: httpx.Client, upstream: FakeAnthropic) -> None:
    _post_messages(http, headers={"accept-encoding": "gzip, deflate, br, zstd"})
    assert upstream.last().header("accept-encoding") == ["identity"]


def test_connection_level_headers_are_not_forwarded(
    http: httpx.Client, upstream: FakeAnthropic
) -> None:
    _post_messages(http, headers={"proxy-authorization": "Basic secret", "te": "trailers"})
    seen = upstream.last()
    assert seen.header("proxy-authorization") == []
    assert seen.header("te") == []
    assert seen.header("host") == [urlsplit(upstream.base_url).netloc]


def test_an_upstream_base_path_is_kept(upstream: FakeAnthropic, tmp_path: Path) -> None:
    # e.g. an upstream gateway mounted under a path prefix.
    for url in _gateway_url(f"{upstream.base_url}/anthropic", tmp_path):
        httpx.post(f"{url}/v1/messages?beta=true", content=b"{}", timeout=WAIT_SEC)
    assert upstream.last().raw_path == "/anthropic/v1/messages"


# ── the response comes back unchanged, as it arrives ─────────────────────────


def test_streamed_response_bytes_are_identical(http: httpx.Client, upstream: FakeAnthropic) -> None:
    direct = httpx.post(
        f"{upstream.base_url}/v1/messages", content=RAW_BODY, timeout=WAIT_SEC
    ).content
    through_gateway = _post_messages(http).content
    assert direct == STREAM_BYTES
    assert through_gateway == direct


def test_streamed_response_headers_are_relayed(http: httpx.Client) -> None:
    response = _post_messages(http)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["anthropic-ratelimit-unified-status"] == "allowed"
    assert response.headers["anthropic-ratelimit-unified-5h-utilization"] == "0.41"
    assert response.headers["request-id"] == "req_fake"
    assert response.headers.get_list("x-multi") == ["one", "two"]


def test_pings_reach_the_client(http: httpx.Client) -> None:
    assert _post_messages(http).content.count(b"event: ping\n") == 2


def test_the_first_byte_is_not_held_back(http: httpx.Client) -> None:
    # The upstream sends message_start, then goes silent for a second (a thinking pause). A
    # buffering proxy would deliver nothing until the end.
    started = time.monotonic()
    with http.stream(
        "POST",
        "/v1/messages?beta=true",
        content=RAW_BODY,
        headers={"x-fake-scenario": "slow-start"},
    ) as response:
        chunks = response.iter_raw()
        first = next(chunks)
        first_byte_after = time.monotonic() - started
        rest = b"".join(chunks)
    total = time.monotonic() - started
    assert first.startswith(b"event: message_start")
    assert first_byte_after < 0.5
    assert total >= 1.0
    assert first + rest == STREAM_BYTES


def test_non_streaming_response_is_relayed(http: httpx.Client) -> None:
    body = json.loads(RAW_BODY)
    body["stream"] = False
    response = http.post("/v1/messages", json=body)
    assert response.status_code == 200
    assert response.json()["content"] == [{"type": "text", "text": "ok"}]
    assert response.headers.get_list("x-multi") == ["one", "two"]


@pytest.mark.parametrize(
    ("scenario", "status", "headers"),
    [
        ("error-400", 400, {"x-should-retry": "false", "request-id": "req_err"}),
        (
            "error-429",
            429,
            {
                "retry-after": "7",
                "x-should-retry": "true",
                "anthropic-ratelimit-unified-status": "rejected",
            },
        ),
    ],
)
def test_upstream_errors_are_relayed_verbatim(
    http: httpx.Client, upstream: FakeAnthropic, scenario: str, status: int, headers: dict
) -> None:
    direct = httpx.post(
        f"{upstream.base_url}/v1/messages",
        content=RAW_BODY,
        headers={"x-fake-scenario": scenario},
        timeout=WAIT_SEC,
    )
    response = _post_messages(http, headers={"x-fake-scenario": scenario})
    assert response.status_code == status
    assert response.content == direct.content  # Claude Code's retry logic reads this wording
    for name, value in headers.items():
        assert response.headers[name] == value
    assert GATEWAY_ERROR_HEADER not in response.headers


def test_count_tokens_models_and_hello_are_forwarded(
    http: httpx.Client, upstream: FakeAnthropic
) -> None:
    assert http.post("/v1/messages/count_tokens", json={}).json() == {"input_tokens": 42}
    assert http.get("/v1/models?limit=1000").json()["data"][0]["id"] == "claude-opus-5-5"
    assert upstream.last().query == "limit=1000"
    assert http.head("/api/hello").status_code == 200
    assert upstream.last().method == "HEAD"


def test_paths_the_gateway_has_never_seen_are_forwarded(
    http: httpx.Client, upstream: FakeAnthropic
) -> None:
    # Claude Code gains endpoints over releases; an allowlist would break the next one.
    response = http.post("/v1/some/endpoint/from/next/year", json={"a": 1})
    assert response.json() == {"echo": "/v1/some/endpoint/from/next/year"}


@pytest.mark.parametrize(
    ("path", "endpoint"),
    [
        ("/v1/messages", "messages"),
        ("/v1/messages/count_tokens", "count_tokens"),
        ("/v1/models", "models"),
        ("/v1/models/claude-opus-5-5", "models"),
        ("/api/hello", "hello"),
        ("/v1/files", "other"),
    ],
)
def test_paths_are_classified(path: str, endpoint: str) -> None:
    assert classify(path) == endpoint


# ── the gateway's own paths are never forwarded ──────────────────────────────


@pytest.mark.parametrize(("method", "path"), [("POST", "/healthz"), ("GET", "/admin/anything")])
def test_own_paths_are_not_forwarded(
    http: httpx.Client, upstream: FakeAnthropic, method: str, path: str
) -> None:
    response = http.request(method, path)
    assert response.status_code == 404
    assert response.headers[GATEWAY_ERROR_HEADER] == "not_forwarded"
    assert upstream.requests == []


# ── failures ─────────────────────────────────────────────────────────────────


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_an_unreachable_upstream_is_an_anthropic_shaped_error(tmp_path: Path) -> None:
    for url in _gateway_url(f"http://127.0.0.1:{_closed_port()}", tmp_path):
        response = httpx.post(f"{url}/v1/messages", content=RAW_BODY, timeout=WAIT_SEC)
    assert response.status_code == 502
    assert response.headers[GATEWAY_ERROR_HEADER] == "upstream_unreachable"
    assert response.headers["x-should-retry"] == "true"
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "api_error"
    assert body["error"]["message"].startswith("TheYgent AI gateway:")


def test_an_upstream_breaking_mid_stream_is_not_passed_off_as_complete(
    http: httpx.Client,
) -> None:
    # A clean end would let Claude Code treat a truncated response as finished.
    with pytest.raises(httpx.RemoteProtocolError):
        _post_messages(http, headers={"x-fake-scenario": "break"})


def _hang_up_once_held(gateway_url: str, upstream: FakeAnthropic, scenario: str) -> None:
    """Send a request over a raw socket and close it once the upstream is holding it — the way
    a client that gave up waiting does."""
    url = urlsplit(gateway_url)
    assert url.hostname is not None and url.port is not None
    sock = socket.create_connection((url.hostname, url.port), timeout=WAIT_SEC)
    try:
        sock.sendall(
            b"POST /v1/messages?beta=true HTTP/1.1\r\n"
            + f"Host: {url.netloc}\r\n".encode()
            + b"Content-Type: application/json\r\n"
            + f"x-fake-scenario: {scenario}\r\n".encode()
            + f"Content-Length: {len(RAW_BODY)}\r\n\r\n".encode()
            + RAW_BODY
        )
        assert upstream.hold_started.wait(WAIT_SEC), "the request never reached the upstream"
    finally:
        sock.close()


def test_a_client_hanging_up_mid_stream_cancels_the_upstream(
    gateway: str, upstream: FakeAnthropic
) -> None:
    _hang_up_once_held(gateway, upstream, "hold-stream")
    assert upstream.abandoned.wait(WAIT_SEC), "the upstream kept generating for a gone client"


def test_a_client_hanging_up_before_the_upstream_answers_cancels_it(
    gateway: str, upstream: FakeAnthropic
) -> None:
    # The upstream has not sent headers yet, so no response has started: the server would never
    # cancel this handler on its own.
    _hang_up_once_held(gateway, upstream, "hold-headers")
    assert upstream.abandoned.wait(WAIT_SEC), "the upstream kept working for a gone client"


# ── secrets and concurrency ──────────────────────────────────────────────────


def test_no_credential_reaches_the_logs(
    http: httpx.Client, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    secrets = ["sk-ant-oat01-SECRET-OAUTH", "sk-ant-api03-SECRET-KEY", "tyg_SECRET_GATEWAY_KEY"]
    with caplog.at_level(logging.DEBUG):
        _post_messages(
            http,
            headers={
                "authorization": f"Bearer {secrets[0]}",
                "x-api-key": secrets[1],
                "x-theygent-gateway-key": secrets[2],
            },
        )
        for url in _gateway_url(f"http://127.0.0.1:{_closed_port()}", tmp_path):
            httpx.post(f"{url}/v1/messages", headers={"x-api-key": secrets[1]}, timeout=WAIT_SEC)
    assert caplog.records, "expected some log output to inspect"
    for secret in secrets:
        assert secret not in caplog.text


def test_concurrent_streams_stay_separate_and_intact(http: httpx.Client) -> None:
    with ThreadPoolExecutor(max_workers=20) as pool:
        bodies = list(pool.map(lambda _: _post_messages(http).content, range(40)))
    assert all(body == STREAM_BYTES for body in bodies)
