"""A fake inference plane on real uvicorn: an OpenAI-compatible chat completions endpoint that
records what it was asked and answers as told — content, status, delay, finish reason."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from _fake_anthropic import serve
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

ANSWER = '{"title": "Fix the parser"}'


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
