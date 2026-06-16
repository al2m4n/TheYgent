"""A real, threaded, OpenAI-compatible *fake inference plane* for the fast suite.

Everything real except the model: a genuine uvicorn server on an ephemeral port so the
control-plane's gateway-client really talks HTTP across the seam (M3 §6 — the in-process
shortcut is exactly the trap §3.1 forbids). Mirrors the inference plane's own
``tests/_fake_upstream._ThreadedServer`` mechanics.

Selectable ``mode`` exercises the control-plane's error paths:
  * ``normal``         — deterministic SSE + non-stream completion
  * ``error_503``      — 503 engine_unavailable (engine not ready)
  * ``error_404``      — 404 model_not_found (unknown logical id)
  * ``drop_midstream`` — stream a chunk then drop the connection mid-stream
"""

from __future__ import annotations

import json
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

FULL_MESSAGE = "hello world"
_CHUNKS = ["hello", " world"]


def _build_app(mode: str, captured: dict[str, object]) -> FastAPI:
    app = FastAPI()

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/v1/models")
    async def models() -> dict[str, object]:
        return {
            "object": "list",
            "data": [{"id": "triage-fast", "object": "model", "owned_by": "theygent"}],
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        # Prove the control-plane forwards the run-id header and the logical id verbatim.
        captured["run_id_header"] = request.headers.get("x-theygent-run-id")
        captured["model"] = body.get("model")

        if mode == "error_503":
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "message": "engine not installed on this host",
                        "type": "server_error",
                        "code": "engine_unavailable",
                    }
                },
            )
        if mode == "error_404":
            return JSONResponse(
                status_code=404,
                content={
                    "error": {
                        "message": "unknown logical id",
                        "type": "invalid_request_error",
                        "code": "model_not_found",
                    }
                },
            )

        model = body.get("model", "fake")
        if body.get("stream"):
            return StreamingResponse(
                _stream(model, drop=mode == "drop_midstream"),
                media_type="text/event-stream",
            )
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


async def _stream(model: str, *, drop: bool):
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
        yield f"data: {json.dumps(chunk)}\n\n"
        if drop:
            # Simulate inference dying mid-stream: stop emitting without [DONE] and
            # raise so the connection drops, exactly the §4 mid-stream-failure case.
            raise RuntimeError("inference dropped mid-stream")
    final = {
        "id": "chatcmpl-fake",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final)}\n\n"
    yield "data: [DONE]\n\n"


class FakeInference:
    """A real OpenAI-compatible HTTP server on an ephemeral port (context manager)."""

    def __init__(self, mode: str = "normal") -> None:
        self.captured: dict[str, object] = {"run_id_header": None, "model": None}
        app = _build_app(mode, self.captured)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def __enter__(self) -> FakeInference:
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake inference did not start")
            time.sleep(0.01)
        self.port = self._server.servers[0].sockets[0].getsockname()[1]
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)

    @property
    def v1_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"
