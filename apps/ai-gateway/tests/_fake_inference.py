"""A fake inference plane on real uvicorn: an OpenAI-compatible chat completions endpoint that
records what it was asked and answers as told — content, status, delay, finish reason."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from _fake_anthropic import serve
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

ANSWER = '{"title": "Fix the parser"}'
USAGE = {"prompt_tokens": 812, "completion_tokens": 9, "total_tokens": 821}


@dataclass
class FakeInference:
    """An OpenAI-compatible chat completions endpoint that records what it was asked."""

    content: str | None = ANSWER
    status: int = 200
    delay: float = 0.0
    finish_reason: str = "stop"
    #: The streamed answer in pieces (``None``: ``content`` whole), each after ``piece_delay``.
    pieces: list[str] | None = None
    piece_delay: float = 0.0
    #: Tool calls streamed after the text: (name, the arguments in pieces) each.
    tool_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    #: Answer this many requests as told, then fail every later one (503).
    fail_after: int | None = None
    #: The usage reported: the prompt's tokens, and how many of them the engine had cached.
    prompt_tokens: int = USAGE["prompt_tokens"]
    cached_tokens: int | None = None
    sent_pieces: int = 0
    abandoned: threading.Event = field(default_factory=threading.Event)
    requests: list[dict[str, Any]] = field(default_factory=list)
    asked: threading.Event = field(default_factory=threading.Event)
    base_url: str = ""

    def usage(self) -> dict[str, Any]:
        usage: dict[str, Any] = {
            **USAGE,
            "prompt_tokens": self.prompt_tokens,
            "total_tokens": self.prompt_tokens + USAGE["completion_tokens"],
        }
        if self.cached_tokens is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": self.cached_tokens}
        return usage

    async def completions(self, request: Request) -> Response:
        body = await request.json()
        self.requests.append(body)
        self.asked.set()
        await asyncio.sleep(self.delay)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            self.status = 503
        if self.status != 200:
            return JSONResponse(
                {"error": {"message": "engine unavailable", "code": "engine_unavailable"}},
                status_code=self.status,
            )
        if body.get("stream"):
            return StreamingResponse(self._chunks(body), media_type="text/event-stream")
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
                "usage": self.usage(),
            }
        )

    async def _chunks(self, body: dict[str, Any]) -> AsyncIterator[bytes]:
        """The answer as an OpenAI stream: a role chunk, the text in ``pieces``, the finish reason,
        and usage when asked for — each piece after ``piece_delay``."""

        def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> bytes:
            data = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": body["model"],
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                **extra,
            }
            return f"data: {json.dumps(data)}\n\n".encode()

        try:
            yield chunk({"role": "assistant"})
            pieces = self.pieces if self.pieces is not None else [self.content or ""]
            for piece in pieces:
                await asyncio.sleep(self.piece_delay)
                self.sent_pieces += 1
                yield chunk({"content": piece})
            for index, (name, arguments) in enumerate(self.tool_calls):
                # A call's id and name first, then its arguments in pieces, as servers stream them.
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": f"call_{index}",
                                "type": "function",
                                "function": {"name": name, "arguments": ""},
                            }
                        ]
                    }
                )
                for part in arguments:
                    await asyncio.sleep(self.piece_delay)
                    yield chunk({"tool_calls": [{"index": index, "function": {"arguments": part}}]})
            yield chunk({}, "tool_calls" if self.tool_calls else self.finish_reason)
            if (body.get("stream_options") or {}).get("include_usage"):
                usage = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": body["model"],
                    "choices": [],
                    "usage": self.usage(),
                }
                yield f"data: {json.dumps(usage)}\n\n".encode()
            yield b"data: [DONE]\n\n"
        except asyncio.CancelledError:
            self.abandoned.set()
            raise


@contextmanager
def fake_inference() -> Iterator[FakeInference]:
    state = FakeInference()
    app = Starlette(routes=[Route("/v1/chat/completions", state.completions, methods=["POST"])])
    with serve(app) as url:
        state.base_url = f"{url}/v1"
        yield state
