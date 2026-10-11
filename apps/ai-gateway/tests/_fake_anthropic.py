"""A fake Anthropic-format upstream on real uvicorn, for the fast suite.

It records every request exactly as received (method, raw path, query, raw headers, body bytes)
and answers with deterministic bytes, so a test can compare what went through the gateway against
what the upstream saw and sent. Two test-only request headers steer it (the gateway forwards them
like any other — headers are an open list):

* ``x-fake-fixture: <name>`` replays a recorded exchange (``_fixtures.py``): the recorded status,
  headers and bytes, dropping the connection where the fixture says the upstream did;
* ``x-fake-scenario`` picks a misbehaviour: a slow first byte, holding until the caller hangs up,
  breaking mid-stream, or an error response;
* ``x-fake-refuse-model: <model>`` refuses requests for that model (``x-fake-refuse-status``,
  400 by default), as a router's target sometimes does. A replayed exchange reports the model the
  request asked for, so a routed request is answered "by" its target;
* ``x-fake-slow-model: <model>`` holds requests for that model ``x-fake-slow-seconds`` (2 by
  default) before answering; ``x-fake-pressure-model: <model>`` answers them with rate-limit
  headers showing little left; ``x-fake-output-model: <model>=<n>`` reports ``n`` output tokens
  for them — so a shadow run, a copy sent to another model, can be told apart from the original.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import uvicorn
from _fixtures import Exchange, load
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route
from starlette.types import ASGIApp


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


#: A Messages stream shaped like Claude Code's real traffic: thinking, text, a tool call, pings.
STREAM_EVENTS: list[bytes] = [
    sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": "msg_fake01",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-5-5",
                "content": [],
                "stop_reason": None,
                "usage": {
                    "input_tokens": 2,
                    "cache_read_input_tokens": 28514,
                    "cache_creation_input_tokens": 323,
                    "output_tokens": 1,
                },
            },
        },
    ),
    sse("ping", {"type": "ping"}),
    sse(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ),
    sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Fixing `add` — ünïcödé ✓"},
        },
    ),
    sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
    sse("ping", {"type": "ping"}),
    sse(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Edit", "input": {}},
        },
    ),
    sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"file_path": "calc.py"}'},
        },
    ),
    sse("content_block_stop", {"type": "content_block_stop", "index": 1}),
    sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 42},
        },
    ),
    sse("message_stop", {"type": "message_stop"}),
]
STREAM_BYTES = b"".join(STREAM_EVENTS)

MESSAGE_JSON = {
    "id": "msg_fake02",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus-5-5",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 1},
}
RATE_LIMIT_HEADERS = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.41",
    "request-id": "req_fake",
}


@dataclass
class Captured:
    method: str
    raw_path: str
    query: str
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> list[str]:
        return [value for key, value in self.headers if key == name]


@dataclass
class FakeAnthropic:
    """The fake upstream's state, shared with the test thread."""

    requests: list[Captured] = field(default_factory=list)
    hold_started: threading.Event = field(default_factory=threading.Event)
    slow_started: threading.Event = field(default_factory=threading.Event)
    abandoned: threading.Event = field(default_factory=threading.Event)
    base_url: str = ""

    def last(self) -> Captured:
        assert self.requests, "the upstream received no request"
        return self.requests[-1]


#: What the upstream reports when a rate limit is nearly used up.
PRESSURE_HEADERS = [
    (b"anthropic-ratelimit-output-tokens-limit", b"80000"),
    (b"anthropic-ratelimit-output-tokens-remaining", b"4000"),
]


def replay(
    exchange: Exchange,
    served: str | None = None,
    *,
    output_tokens: int | None = None,
    extra_headers: list[tuple[bytes, bytes]] | None = None,
) -> Response:
    """Answer exactly as the recorded upstream did — reporting ``served`` as the model, when the
    request asked for another than the recording did (a routed request), and ``output_tokens``
    where given."""
    headers = [(k.encode("latin-1"), v.encode("latin-1")) for k, v in exchange.response_headers]
    headers += extra_headers or []
    recorded = exchange.body.get("model") if isinstance(exchange.body, dict) else None

    def as_served(text: str) -> str:
        if output_tokens is not None:
            text = re.sub(r'"output_tokens":\d+', f'"output_tokens":{output_tokens}', text)
        if served is None or recorded is None or served == recorded:
            return text
        return text.replace(f'"{recorded}"', f'"{served}"')

    if exchange.events is None:
        body = as_served(exchange.response_bytes().decode()).encode()
        response = Response(body, status_code=exchange.status)
        response.raw_headers = headers
        return response
    events = [as_served(event) for event in exchange.events]

    async def stream() -> AsyncIterator[bytes]:
        for index, event in enumerate(events):
            yield event.encode()
            if exchange.abort_after_events is not None and index + 1 >= exchange.abort_after_events:
                raise ConnectionResetError("the recorded upstream dropped the connection here")

    response = StreamingResponse(stream(), status_code=exchange.status)
    response.raw_headers = headers
    return response


def build_app(state: FakeAnthropic) -> Starlette:
    async def capture(request: Request) -> Captured:
        body = await request.body()
        captured = Captured(
            method=request.method,
            raw_path=request.scope["raw_path"].decode("latin-1"),
            query=request.scope["query_string"].decode("latin-1"),
            headers=[(k.decode("latin-1"), v.decode("latin-1")) for k, v in request.headers.raw],
            body=body,
        )
        state.requests.append(captured)
        return captured

    async def messages(request: Request) -> Response:
        captured = await capture(request)
        asked = json.loads(captured.body or b"{}").get("model")
        refused = request.headers.get("x-fake-refuse-model")
        if refused and asked == refused:
            # A model that will not take this request, as a router's target sometimes will not.
            status = int(request.headers.get("x-fake-refuse-status", "400"))
            return JSONResponse(
                {
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": f"{asked} refuses"},
                },
                status_code=status,
            )
        if asked and asked == request.headers.get("x-fake-slow-model"):
            state.slow_started.set()
            await asyncio.sleep(float(request.headers.get("x-fake-slow-seconds", "2")))
        fixture = request.headers.get("x-fake-fixture")
        if fixture:
            output = request.headers.get("x-fake-output-model", "")
            model, _, count = output.partition("=")
            pressured = asked and asked == request.headers.get("x-fake-pressure-model")
            return replay(
                load(fixture),
                served=asked,
                output_tokens=int(count) if asked and model == asked else None,
                extra_headers=PRESSURE_HEADERS if pressured else None,
            )
        scenario = request.headers.get("x-fake-scenario", "")
        if scenario == "error-400":
            return JSONResponse(
                {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": "thinking: bound to a different conversation",
                    },
                },
                status_code=400,
                headers={"x-should-retry": "false", "request-id": "req_err"},
            )
        if scenario == "error-429":
            return JSONResponse(
                {"type": "error", "error": {"type": "rate_limit_error", "message": "Error"}},
                status_code=429,
                headers={
                    "retry-after": "7",
                    "x-should-retry": "true",
                    "anthropic-ratelimit-unified-status": "rejected",
                },
            )
        if scenario == "hold-headers":
            state.hold_started.set()
            # The body is already read, so the next message is the caller's disconnect.
            while (await request.receive())["type"] != "http.disconnect":
                pass
            state.abandoned.set()
            return Response(status_code=200)

        streaming = json.loads(captured.body or b"{}").get("stream", False)
        if not streaming:
            response = JSONResponse(MESSAGE_JSON, headers=RATE_LIMIT_HEADERS)
            response.raw_headers.extend([(b"x-multi", b"one"), (b"x-multi", b"two")])
            return response

        async def stream() -> AsyncIterator[bytes]:
            try:
                for index, event in enumerate(STREAM_EVENTS):
                    yield event
                    if scenario == "slow-start" and index == 0:
                        await asyncio.sleep(1.0)
                    if scenario == "hold-stream" and index == 0:
                        state.hold_started.set()
                        await asyncio.sleep(30)
                    if scenario == "break" and index == 2:
                        raise ConnectionResetError("upstream went away mid-stream")
            except asyncio.CancelledError:
                state.abandoned.set()
                raise

        response = StreamingResponse(
            stream(), media_type="text/event-stream", headers=RATE_LIMIT_HEADERS
        )
        response.raw_headers.extend([(b"x-multi", b"one"), (b"x-multi", b"two")])
        return response

    async def count_tokens(request: Request) -> Response:
        await capture(request)
        return JSONResponse({"input_tokens": 42})

    async def models(request: Request) -> Response:
        await capture(request)
        return JSONResponse({"data": [{"id": "claude-opus-5-5"}, {"id": "claude-sonnet-5-5"}]})

    async def hello(request: Request) -> Response:
        await capture(request)
        return Response(status_code=200)

    async def anything(request: Request) -> Response:
        captured = await capture(request)
        return JSONResponse({"echo": captured.raw_path})

    return Starlette(
        routes=[
            Route("/v1/messages", messages, methods=["POST"]),
            Route("/v1/messages/count_tokens", count_tokens, methods=["POST"]),
            Route("/v1/models", models, methods=["GET"]),
            Route("/api/hello", hello, methods=["HEAD", "GET"]),
            Route("/{path:path}", anything, methods=["GET", "POST", "PUT", "PATCH", "DELETE"]),
        ]
    )


class ThreadedServer:
    """An ASGI app on real uvicorn (ephemeral port) in a daemon thread."""

    def __init__(self, app: ASGIApp) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> int:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.01)
        return self._server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


@contextmanager
def serve(app: ASGIApp) -> Iterator[str]:
    """Serve ``app`` on real uvicorn for the block's duration; yields its base URL."""
    server = ThreadedServer(app)
    port = server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


@contextmanager
def fake_anthropic() -> Iterator[FakeAnthropic]:
    state = FakeAnthropic()
    with serve(build_app(state)) as url:
        state.base_url = url
        yield state
