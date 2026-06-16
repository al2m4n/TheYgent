"""FakeUpstreamLauncher — everything real except the model weights.

It boots a *real* in-process OpenAI-compatible server (uvicorn on an ephemeral
port) returning deterministic completions and genuine SSE. So the manager really
spawns something, really tracks a port, the LiteLLM gateway really proxies over
HTTP, and SSE really streams — only the thing answering on the port is fake.
This is the injected ``EngineLauncher`` for the fast suite (plan §"Test strategy").
"""

from __future__ import annotations

import threading
import time

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from theygent_inference.launcher import EngineHandle
from theygent_ir import Capabilities, ManagedBinding

# Deterministic completion; streamed as the two chunks below.
FULL_MESSAGE = "hello world"
_CHUNKS = ["hello", " world"]


def _build_fake_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/props")
    async def props() -> dict[str, object]:
        return {"default_generation_settings": {"n_ctx": 4096}}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        model = body.get("model", "fake")
        if body.get("stream"):

            async def gen():
                for i, piece in enumerate(_CHUNKS):
                    delta = {"content": piece}
                    if i == 0:
                        delta["role"] = "assistant"
                    chunk = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    yield f"data: {_dumps(chunk)}\n\n"
                final = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {_dumps(final)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(gen(), media_type="text/event-stream")

        return JSONResponse(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": FULL_MESSAGE},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
            }
        )

    return app


def _dumps(obj: object) -> str:
    import json

    return json.dumps(obj)


class _ThreadedServer:
    def __init__(self) -> None:
        config = uvicorn.Config(_build_fake_app(), host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> int:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake upstream did not start")
            time.sleep(0.01)
        return self._server.servers[0].sockets[0].getsockname()[1]

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


class FakeUpstreamHandle:
    def __init__(self, advertised: Capabilities) -> None:
        self._server = _ThreadedServer()
        self._port = self._server.start()
        self._advertised = advertised
        self.terminated = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    async def health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                return (await client.get(f"{self.base_url}/health")).status_code == 200
        except httpx.HTTPError:
            return False

    async def capabilities(self) -> Capabilities:
        return self._advertised

    async def terminate(self) -> None:
        self.terminated = True
        self._server.stop()


# Static type sanity: FakeUpstreamHandle satisfies EngineHandle.
_: type[EngineHandle] = FakeUpstreamHandle


class FakeUpstreamLauncher:
    """Injected EngineLauncher. Tracks handles so tests can assert spawn/terminate."""

    def __init__(self, advertised: Capabilities | None = None) -> None:
        self._advertised = advertised or Capabilities(
            tool_calling=True, structured_output=True, vision=False, max_context=4096
        )
        self.handles: list[FakeUpstreamHandle] = []

    @property
    def ready(self) -> bool:
        return True

    @property
    def not_ready_reason(self) -> str | None:
        return None

    @property
    def launch_count(self) -> int:
        return len(self.handles)

    async def launch(self, binding: ManagedBinding) -> EngineHandle:
        handle = FakeUpstreamHandle(self._advertised)
        self.handles.append(handle)
        return handle
