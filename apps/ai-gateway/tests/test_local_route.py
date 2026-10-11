"""Router mode's local route: a reviewed side request answered by a model on the inference plane.

What must hold:

* the request translates to a chat completion faithfully or not at all: text system blocks (less
  the billing attribution) and text messages, the output schema as a JSON-schema response format,
  the output budget capped; tools, images, documents or another output format do not translate;
* the answer reaches Claude Code as the Anthropic message it would have read — streamed when it
  asked for a stream — and only once it is checked: text, and JSON that fits the schema;
* the record says a local model served it: applied, at no API spend, with the requested model's
  cost as its baseline;
* anything that goes wrong — no inference plane, an error, a slow answer, an answer that does not
  fit, a request that does not translate — sends the original bytes to the requested model, whose
  answer Claude Code gets, and nothing is tried locally outside router mode.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from _fake_anthropic import FakeAnthropic, fake_anthropic, serve
from _fixtures import Exchange, load
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from theygent_ai_gateway import local
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.local import answer_text, as_stream, fits, translate
from theygent_ai_gateway.observe import Observation, ResponseObserver
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ai_gateway.signals import read_request
from theygent_ir.gateway import FingerprintRule, GatewayConfig, GatewayMode, PolicyConfig, Usage

PRICES = PriceBook.packaged()
LOCAL = "aux-local"
TITLE = load("auxiliary-session-title")
TITLE_SCHEMA = TITLE.body["output_config"]["format"]["schema"]
ANSWER = '{"title": "Fix the parser"}'


def title_with(**changes: Any) -> Exchange:
    """The session-title request with top-level fields changed (``None`` removes one)."""
    body = {**TITLE.body, **changes}
    return replace(TITLE, body={k: v for k, v in body.items() if v is not None})


# ── translation ──────────────────────────────────────────────────────────────


def test_the_session_title_translates() -> None:
    translated = translate(TITLE.request_bytes())
    assert translated is not None
    system = TITLE.body["system"]
    # The billing attribution is for Anthropic; the rest of the system prompt goes, in order.
    assert translated.messages == [
        {"role": "system", "content": f"{system[1]['text']}\n\n{system[2]['text']}"},
        {"role": "user", "content": TITLE.body["messages"][0]["content"][0]["text"]},
    ]
    assert translated.params["max_tokens"] == local.LOCAL_MAX_TOKENS < TITLE.body["max_tokens"]
    assert translated.params["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "answer", "schema": TITLE_SCHEMA},
    }
    assert translated.params["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    assert translated.schema == TITLE_SCHEMA and translated.stream is True


def test_plain_strings_and_sampling_translate() -> None:
    body = {
        "model": "claude-haiku-4-5",
        "system": "Be brief.",
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": [{"type": "text", "text": "two"}]},
            {"role": "user", "content": "three"},
        ],
        "max_tokens": 64,
        "temperature": 0.2,
        "top_p": 0.9,
        "stop_sequences": ["END"],
    }
    translated = translate(json.dumps(body).encode())
    assert translated is not None
    assert [m["role"] for m in translated.messages] == ["system", "user", "assistant", "user"]
    assert translated.params["max_tokens"] == 64
    assert (translated.params["temperature"], translated.params["top_p"]) == (0.2, 0.9)
    assert translated.params["stop"] == ["END"]
    assert "response_format" not in translated.params and translated.schema is None
    assert translated.stream is False


IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}


@pytest.mark.parametrize(
    "changes",
    [
        {"tools": [{"name": "Read", "input_schema": {"type": "object"}}]},
        {"tool_choice": {"type": "auto"}},
        {"messages": [{"role": "user", "content": [IMAGE]}]},
        {"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]}]},
        {"messages": [{"role": "user", "content": [{"type": "document", "source": {}}]}]},
        # A block of a kind it does not know, even one carrying text, is not text.
        {"messages": [{"role": "user", "content": [{"type": "new_kind", "text": "x"}]}]},
        {"messages": [{"role": "system", "content": "x"}]},
        {"messages": []},
        {"messages": None},
        {"system": [{"type": "text", "text": "x"}, IMAGE]},
        {"output_config": {"format": {"type": "regex", "pattern": "x"}}},
        {"output_config": {"format": {"type": "json_schema"}}},
    ],
)
def test_what_does_not_translate_faithfully_does_not_translate(changes: dict[str, Any]) -> None:
    assert translate(title_with(**changes).request_bytes()) is None


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b'"x"'])
def test_a_body_that_is_not_a_request_does_not_translate(body: bytes) -> None:
    assert translate(body) is None


# ── checking the answer ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ({"title": "x"}, True),
        ({}, False),
        ({"title": 1}, False),
        ({"title": None}, False),
        ({"title": "x", "extra": 1}, False),
        (["title"], False),
        ("x", False),
    ],
)
def test_the_answer_must_fit_the_title_schema(value: Any, ok: bool) -> None:
    assert fits(value, TITLE_SCHEMA) is ok


def test_nested_schemas_are_checked() -> None:
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "inner": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
        },
    }
    assert fits({"n": 3, "tags": ["a"], "inner": {"ok": True}}, schema)
    assert not fits({"n": True}, schema)  # a boolean is not a number in JSON Schema
    assert not fits({"n": 1.5}, schema)
    assert not fits({"tags": ["a", 2]}, schema)
    assert not fits({"inner": {"ok": "yes"}}, schema)
    assert fits({"other": "anything"}, schema)  # additional properties allowed unless refused


@pytest.mark.parametrize(
    ("content", "text"),
    [
        (ANSWER, ANSWER),
        (f"  {ANSWER}\n", ANSWER),
        (f"<think>\nA title for it.\n</think>\n\n{ANSWER}", ANSWER),
        (f"```json\n{ANSWER}\n```", ANSWER),
        (f"<think></think>```\n{ANSWER}```", ANSWER),
        (None, ""),
    ],
)
def test_reasoning_and_fences_are_not_part_of_the_answer(content: str | None, text: str) -> None:
    assert answer_text(content) == text


def test_a_streamed_answer_reads_like_anthropics() -> None:
    message = local._message(ANSWER, LOCAL, Usage(input_tokens=812, output_tokens=9), "end_turn")
    stream = as_stream(message)
    events = [block.split("\n")[0] for block in stream.decode().split("\n\n") if block]
    assert events == [
        "event: message_start",
        "event: content_block_start",
        "event: content_block_delta",
        "event: content_block_stop",
        "event: message_delta",
        "event: message_stop",
    ]
    assert read_text(stream) == ANSWER
    observer = ResponseObserver("text/event-stream; charset=utf-8")
    observer.feed(stream)
    summary = observer.finish()
    assert (summary.model, summary.stop_reason, summary.parse_error) == (LOCAL, "end_turn", None)
    assert (summary.usage.input_tokens, summary.usage.output_tokens) == (812, 9)


def read_text(stream: bytes) -> str:
    """What Claude Code assembles from a stream: its text deltas, in order."""
    text = ""
    for block in stream.decode().split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if data["type"] == "content_block_delta":
                    text += data["delta"]["text"]
    return text


# ── a fake inference plane ───────────────────────────────────────────────────


@dataclass
class FakeInference:
    """An OpenAI-compatible chat completions endpoint that records what it was asked."""

    content: str | None = ANSWER
    status: int = 200
    delay: float = 0.0
    finish_reason: str = "stop"
    requests: list[dict[str, Any]] = field(default_factory=list)
    asked: threading.Event = field(default_factory=threading.Event)
    base_url: str = ""

    async def completions(self, request: Request) -> JSONResponse:
        body = await request.json()
        self.requests.append(body)
        self.asked.set()
        await asyncio.sleep(self.delay)
        if self.status != 200:
            return JSONResponse(
                {"error": {"message": "engine unavailable", "code": "engine_unavailable"}},
                status_code=self.status,
            )
        return JSONResponse(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": self.content},
                        "finish_reason": self.finish_reason,
                    }
                ],
                "usage": {"prompt_tokens": 812, "completion_tokens": 9, "total_tokens": 821},
            }
        )


@contextmanager
def fake_inference() -> Iterator[FakeInference]:
    state = FakeInference()
    app = Starlette(routes=[Route("/v1/chat/completions", state.completions, methods=["POST"])])
    with serve(app) as url:
        state.base_url = f"{url}/v1"
        yield state


def title_fingerprint() -> str:
    signals = read_request(
        dict(TITLE.request_headers), TITLE.request_bytes(), has_messages_body=True
    )
    assert signals.body is not None and signals.body.fingerprint is not None
    return signals.body.fingerprint


def config(mode: GatewayMode = "router") -> GatewayConfig:
    return GatewayConfig(
        version=1,
        mode=mode,
        policy=PolicyConfig(local_target=LOCAL),
        fingerprints=[FingerprintRule(fingerprint=title_fingerprint(), status="allow_local")],
        updated_at=datetime(2026, 10, 11, tzinfo=UTC),
    )


@dataclass
class Gateway:
    url: str
    upstream: FakeAnthropic
    inference: FakeInference
    seen: list[Observation]

    def send(self, exchange: Exchange = TITLE) -> httpx.Response:
        count = len(self.seen)
        response = httpx.post(
            f"{self.url}{exchange.target}",
            content=exchange.request_bytes(),
            headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
            timeout=10,
        )
        self.wait_for(count + 1)
        return response

    def wait_for(self, count: int) -> Observation:
        deadline = time.monotonic() + 10
        while len(self.seen) < count:
            assert time.monotonic() < deadline, "no observation"
            time.sleep(0.01)
        return self.seen[-1]


@contextmanager
def gateway(
    tmp_path: Path, *, mode: GatewayMode = "router", inference: bool = True
) -> Iterator[Gateway]:
    seen: list[Observation] = []
    with fake_anthropic() as upstream, fake_inference() as fake:
        settings = GatewaySettings(
            upstream_url=upstream.base_url,
            inference_plane_url=fake.base_url if inference else None,
            gateway_id="gw",
            state_dir=tmp_path,
        )
        app = create_app(settings, observe=seen.append, config=config(mode))
        with serve(app) as url:
            yield Gateway(url, upstream, fake, seen)


# ── answered locally ─────────────────────────────────────────────────────────


def test_a_session_title_is_answered_locally(tmp_path: Path) -> None:
    with gateway(tmp_path) as gw:
        answer = gw.send()
    observed = gw.seen[-1]
    assert gw.upstream.requests == []  # the Anthropic API never saw it
    [asked] = gw.inference.requests
    assert asked["model"] == LOCAL and asked["stream"] is False
    assert asked["response_format"]["json_schema"]["schema"] == TITLE_SCHEMA
    assert not any("x-anthropic-billing-header" in m["content"] for m in asked["messages"])
    # Claude Code reads its title from the stream, as it would from Anthropic's.
    assert answer.status_code == 200
    assert answer.headers["content-type"].startswith("text/event-stream")
    assert json.loads(read_text(answer.content)) == {"title": "Fix the parser"}
    decision = observed.decision
    assert (decision.action, decision.applied, decision.counterfactual_usd) == ("route", True, None)
    assert decision.target is not None and decision.target.kind == "local"
    assert decision.reasons == ["local_candidate"]
    assert (observed.status, observed.outcome, observed.response.model) == (200, "complete", LOCAL)
    assert observed.cost_usd == 0.0  # no API spend: not unpriced, free
    usage = observed.response.usage
    assert (usage.input_tokens, usage.output_tokens) == (812, 9)
    baseline = PRICES.cost("claude-opus-5-5", usage)
    assert baseline is not None and baseline > 0
    assert observed.baseline_usd == pytest.approx(baseline)


def test_a_request_without_a_stream_gets_a_message(tmp_path: Path) -> None:
    with gateway(tmp_path) as gw:
        answer = gw.send(title_with(stream=None))
    assert gw.upstream.requests == []
    message = answer.json()
    assert answer.headers["content-type"].startswith("application/json")
    assert (message["type"], message["role"], message["model"]) == ("message", "assistant", LOCAL)
    assert message["content"] == [{"type": "text", "text": ANSWER}]
    assert message["stop_reason"] == "end_turn"
    assert gw.seen[-1].decision.applied


def test_an_answer_cut_short_says_so(tmp_path: Path) -> None:
    with gateway(tmp_path) as gw:
        gw.inference.finish_reason = "length"
        gw.send(title_with(stream=None))
    assert gw.seen[-1].response.stop_reason == "max_tokens"


# ── falling back to the requested model ──────────────────────────────────────


def assert_fell_back(gw: Gateway, answer: httpx.Response, exchange: Exchange = TITLE) -> None:
    """The original bytes went to the requested model, and Claude Code got its answer."""
    [received] = gw.upstream.requests
    assert received.body == exchange.request_bytes()
    assert answer.status_code == 200 and answer.content == exchange.response_bytes()
    observed = gw.seen[-1]
    assert observed.decision.reasons == ["target_refused", "local_candidate"]
    assert (observed.decision.action, observed.decision.applied) == ("stay", False)
    assert observed.response.model == "claude-opus-5-5" and observed.baseline_usd is None


@pytest.mark.parametrize(
    "answer",
    [
        "not json",
        '{"name": "Fix the parser"}',  # misses the required key
        '{"title": 7}',
        '{"title": "x", "summary": "y"}',  # additional properties refused
        "",
        None,
    ],
)
def test_an_answer_that_does_not_fit_falls_back(tmp_path: Path, answer: str | None) -> None:
    with gateway(tmp_path) as gw:
        gw.inference.content = answer
        response = gw.send()
    assert len(gw.inference.requests) == 1
    assert_fell_back(gw, response)


@pytest.mark.parametrize("status", [400, 404, 500, 503])
def test_an_inference_error_falls_back(tmp_path: Path, status: int) -> None:
    with gateway(tmp_path) as gw:
        gw.inference.status = status
        response = gw.send()
    assert len(gw.inference.requests) == 1  # never retried locally
    assert_fell_back(gw, response)


def test_a_slow_local_model_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "LOCAL_TIMEOUT_SECONDS", 0.3)
    with gateway(tmp_path) as gw:
        gw.inference.delay = 3
        started = time.monotonic()
        response = gw.send()
        elapsed = time.monotonic() - started
    assert elapsed < 2.5
    assert_fell_back(gw, response)


def test_an_unreachable_inference_plane_falls_back(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]  # nothing listens once the probe closes
    seen: list[Observation] = []
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url,
            inference_plane_url=f"http://127.0.0.1:{closed}/v1",
            gateway_id="gw",
            state_dir=tmp_path,
        )
        with serve(create_app(settings, observe=seen.append, config=config())) as url:
            gw = Gateway(url, upstream, FakeInference(), seen)
            response = gw.send()
    assert_fell_back(gw, response)


def test_no_inference_plane_configured_falls_back(tmp_path: Path) -> None:
    with gateway(tmp_path, inference=False) as gw:
        response = gw.send()
    assert gw.inference.requests == []
    assert_fell_back(gw, response)


def test_a_request_that_does_not_translate_is_not_tried_locally(tmp_path: Path) -> None:
    # The reviewed fingerprint (system prompt and output format; never the messages), carrying a
    # document the inference plane could not be given.
    exchange = title_with(
        messages=[{"role": "user", "content": [{"type": "document", "source": {}}]}]
    )
    with gateway(tmp_path) as gw:
        response = gw.send(exchange)
    assert gw.inference.requests == []
    assert_fell_back(gw, response, exchange)


def test_observer_mode_never_asks_the_local_model(tmp_path: Path) -> None:
    with gateway(tmp_path, mode="observer") as gw:
        response = gw.send()
    assert gw.inference.requests == []
    [received] = gw.upstream.requests
    assert received.body == TITLE.request_bytes()
    assert response.content == TITLE.response_bytes()
    decision = gw.seen[-1].decision
    assert (decision.action, decision.applied, decision.counterfactual_usd) == ("route", False, 0.0)


def test_a_client_that_hangs_up_cancels_the_local_call(tmp_path: Path) -> None:
    with gateway(tmp_path) as gw:
        gw.inference.delay = 5
        url = urlsplit(gw.url)
        assert url.hostname is not None and url.port is not None
        body = TITLE.request_bytes()
        headers = "".join(f"{k}: {v}\r\n" for k, v in TITLE.request_headers if k != "host")
        sock = socket.create_connection((url.hostname, url.port), timeout=10)
        try:
            sock.sendall(
                f"POST /v1/messages HTTP/1.1\r\nHost: {url.netloc}\r\n{headers}".encode()
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
                + body
            )
            assert gw.inference.asked.wait(10)
        finally:
            sock.close()
        observed = gw.wait_for(1)
    assert observed.outcome == "client_disconnected"
    assert gw.upstream.requests == []  # nothing fell back for a client that left
