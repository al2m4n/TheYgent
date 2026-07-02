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
from fastapi.responses import JSONResponse, Response, StreamingResponse
from theygent_inference_plane.launcher import EngineHandle
from theygent_ir import Capabilities, ManagedBinding

# Deterministic completion; streamed as the two chunks below.
FULL_MESSAGE = "hello world"
_CHUNKS = ["hello", " world"]

# Deterministic non-chat outputs (M18 fast suite): the embeddings vector, the STT transcript, and
# the TTS audio body the fake upstream returns so the audio/embeddings endpoints prove end-to-end.
FAKE_EMBEDDING = [0.1, 0.2, 0.3]
FAKE_TRANSCRIPT = "the quick brown fox"
FAKE_AUDIO = b"ID3fake-audio-bytes"


def _build_fake_app() -> tuple[FastAPI, dict[str, object]]:
    app = FastAPI()
    # Records what the upstream actually received, so a test can prove the credential
    # resolved locally and reached only this user-configured endpoint.
    captured: dict[str, object] = {"authorization": None}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/props")
    async def props() -> dict[str, object]:
        return {"default_generation_settings": {"n_ctx": 4096}}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        captured["authorization"] = request.headers.get("authorization")
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

    @app.post("/v1/embeddings")
    async def embeddings(request: Request):
        captured["authorization"] = request.headers.get("authorization")
        body = await request.json()
        model = body.get("model", "fake")
        inputs = body.get("input")
        if inputs == "__force_upstream_404__":
            return JSONResponse({"error": {"message": "Not Found"}}, status_code=404)
        items = inputs if isinstance(inputs, list) else [inputs]
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {"object": "embedding", "index": i, "embedding": list(FAKE_EMBEDDING)}
                    for i, _ in enumerate(items)
                ],
                "model": model,
                "usage": {"prompt_tokens": len(items), "total_tokens": len(items)},
            }
        )

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        captured["authorization"] = request.headers.get("authorization")
        # Consume the multipart body so the request completes; the transcript is deterministic.
        await request.form()
        return JSONResponse({"text": FAKE_TRANSCRIPT})

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        captured["authorization"] = request.headers.get("authorization")
        await request.json()
        return Response(content=FAKE_AUDIO, media_type="audio/mpeg")

    return app, captured


def _dumps(obj: object) -> str:
    import json

    return json.dumps(obj)


class _ThreadedServer:
    def __init__(self) -> None:
        app, self.captured = _build_fake_app()
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
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

    @property
    def last_authorization(self) -> object:
        """The Authorization header this upstream last received (None if none)."""
        return self._server.captured["authorization"]

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
