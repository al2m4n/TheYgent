"""FakeUpstreamLauncher — everything real except the model weights.

It boots a *real* in-process OpenAI-compatible server (uvicorn on an ephemeral
port) returning deterministic completions and genuine SSE. So the manager really
spawns something, really tracks a port, the LiteLLM gateway really proxies over
HTTP, and SSE really streams — only the thing answering on the port is fake.
This is the injected ``EngineLauncher`` for the fast suite.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from theygent_inference_plane.launcher import EngineHandle
from theygent_ir import Capabilities, ManagedBinding

# Deterministic completion; streamed as the two chunks below.
FULL_MESSAGE = "hello world"
_CHUNKS = ["hello", " world"]

# Deterministic non-chat outputs (fast suite): the embeddings vector, the STT transcript, and
# the TTS audio body the fake upstream returns so the audio/embeddings endpoints prove end-to-end.
FAKE_EMBEDDING = [0.1, 0.2, 0.3]
FAKE_TRANSCRIPT = "the quick brown fox"
FAKE_AUDIO = b"ID3fake-audio-bytes"
FAKE_IMAGE_B64 = "iVBORw0KGgo="  # the PNG signature, base64

# A gpt-oss reply in its harmony format, as mlx_lm.server returns it (verbatim, unparsed):
# reasoning on the analysis channel, then a call to the offered function. Sent back when the
# last message is HARMONY_TOOL_TURN; HARMONY_ANSWER_TURN gets reasoning and a final answer.
HARMONY_TOOL_TURN = "__harmony_tool_turn__"
HARMONY_ANSWER_TURN = "__harmony_answer_turn__"
HARMONY_TOOL_REPLY = (
    "<|channel|>analysis<|message|>We need to search RFC 3261 for Timer B. Use function.<|end|>"
    "<|start|>assistant<|channel|>commentary to=functions.rfc3261_search <|constrain|>json"
    '<|message|>{"query":"Timer B INVITE client transaction RFC 3261"}'
)
HARMONY_ANSWER_REPLY = (
    "<|channel|>analysis<|message|>Timer B is 64*T1.<|end|>"
    "<|start|>assistant<|channel|>final<|message|>Timer B is 32 seconds by default."
)

# A thinking model's reply as mlx_lm.server 0.31.3 sends it (recorded from
# mlx-community/Qwen3-0.6B-4bit): the engine splits the thinking off itself into a field named
# `reasoning` — not `reasoning_content` — and repeats `role` on every delta. MLX_REASONING_TURN
# then answers in content; MLX_REASONING_TOOL_TURN answers with Qwen3's text tool call, as a
# server with no tool parser for the model's template passes it through.
MLX_REASONING_TURN = "__mlx_reasoning_turn__"
MLX_REASONING_TOOL_TURN = "__mlx_reasoning_tool_turn__"
MLX_REASONING = "The user asks about Timer B. RFC 3261 sets it to 64*T1."
MLX_REASONING_ANSWER = "Timer B is 32 seconds by default."
MLX_REASONING_TOOL_CALL = (
    '<tool_call>\n{"name": "rfc3261_search", "arguments": {"query": "Timer B"}}\n</tool_call>'
)

# Sent as the chat message: the engine answers the way a llama-server with a failed GPU backend
# does (COMPUTE_ERROR), or with an ordinary per-request server error (SERVER_ERROR).
COMPUTE_ERROR = "__compute_error__"
COMPUTE_ERROR_MID_STREAM = "__compute_error_mid_stream__"
SERVER_ERROR = "__server_error__"

# A non-streaming call that never finishes on its own, like an engine still chewing on an
# oversized prompt or a minutes-long render: only the caller closing the connection ends it (or
# the safety cap below, which keeps a broken test from wedging the server). Sent as the chat
# message, the embeddings / speech input, the image prompt, or the transcribed file's bytes.
HOLD_UNTIL_DISCONNECT = "__hold_until_disconnect__"
_HOLD_CAP_SEC = 30.0


@dataclass
class _Captured:
    """What the upstream actually received. ``authorization`` lets a test prove the credential
    resolved locally and reached only this user-configured endpoint; the events are set from
    the server thread when a held call arrives / its caller hangs up on it."""

    authorization: str | None = None
    chat_body: dict | None = None
    hold_started: threading.Event = field(default_factory=threading.Event)
    hold_abandoned: threading.Event = field(default_factory=threading.Event)


def _build_fake_app() -> tuple[FastAPI, _Captured]:
    app = FastAPI()
    captured = _Captured()

    async def hold_until_caller_hangs_up(request: Request) -> Response | None:
        """Hold a call open until its caller disconnects; ``None`` if the cap passes first."""
        captured.hold_started.set()

        async def caller_hung_up() -> None:
            # The body is fully read, so the next ASGI message is the disconnect.
            while (await request.receive())["type"] != "http.disconnect":
                pass

        try:
            await asyncio.wait_for(caller_hung_up(), timeout=_HOLD_CAP_SEC)
        except TimeoutError:
            return None  # never cancelled: answer normally; the test asserting the hang-up fails
        captured.hold_abandoned.set()
        return Response(status_code=499)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/props")
    async def props(request: Request) -> dict[str, object]:
        captured.authorization = request.headers.get("authorization")
        return {
            "default_generation_settings": {"n_ctx": 4096},
            "chat_template": "{%- if tools %}{{ tools | tojson }}{%- endif %}<think>",
        }

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        captured.authorization = request.headers.get("authorization")
        body = await request.json()
        captured.chat_body = body
        model = body.get("model", "fake")
        last_content = (body.get("messages") or [{}])[-1].get("content")
        # A provider that rejects the request outright (e.g. an unsupported generation
        # param) — the error must reach the caller as a structured 4xx even on a stream.
        if last_content == "__force_upstream_400__":
            return JSONResponse(
                {
                    "error": {
                        "message": "`temperature` is deprecated for this model.",
                        "type": "invalid_request_error",
                    }
                },
                status_code=400,
            )
        # What llama-server answers once its GPU backend has failed (Metal out of memory): every
        # request, small or large, until the process is replaced.
        if last_content == COMPUTE_ERROR:
            return JSONResponse(
                {"error": {"code": 500, "message": "Compute error.", "type": "server_error"}},
                status_code=500,
            )
        if last_content == SERVER_ERROR:
            return JSONResponse(
                {
                    "error": {
                        "code": 500,
                        "message": "template rendering failed",
                        "type": "server_error",
                    }
                },
                status_code=500,
            )
        if last_content == HOLD_UNTIL_DISCONNECT and not body.get("stream"):
            if (abandoned := await hold_until_caller_hangs_up(request)) is not None:
                return abandoned
        thinking = {
            MLX_REASONING_TURN: MLX_REASONING_ANSWER,
            MLX_REASONING_TOOL_TURN: MLX_REASONING_TOOL_CALL,
        }
        answer = thinking.get(last_content) if isinstance(last_content, str) else None
        if answer is not None and body.get("stream"):

            async def thinking_stream():
                pieces: list[dict[str, str]] = [
                    {"reasoning": MLX_REASONING[i : i + 8]} for i in range(0, len(MLX_REASONING), 8)
                ]
                pieces.append({"content": answer})
                for delta in pieces:
                    chunk = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", **delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {_dumps(chunk)}\n\n"
                final = {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": "stop"}
                    ],
                }
                yield f"data: {_dumps(final)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(thinking_stream(), media_type="text/event-stream")
        if answer is not None:
            return JSONResponse(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": answer,
                                "reasoning": MLX_REASONING,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        harmony = {HARMONY_TOOL_TURN: HARMONY_TOOL_REPLY, HARMONY_ANSWER_TURN: HARMONY_ANSWER_REPLY}
        reply_text = harmony.get(last_content) if isinstance(last_content, str) else None
        if reply_text is not None and body.get("stream"):

            async def harmony_stream():
                # Seven-character pieces, so special tokens arrive split across chunks.
                for i in range(0, len(reply_text), 7):
                    delta = {"content": reply_text[i : i + 7]}
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

            return StreamingResponse(harmony_stream(), media_type="text/event-stream")
        if reply_text is not None:
            return JSONResponse(
                {
                    "id": "chatcmpl-fake",
                    "object": "chat.completion",
                    "created": 0,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": reply_text},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
        if body.get("stream"):
            # A stream that fails AFTER chunks started flowing — by then the 200 is
            # committed on every hop. Real engines report this as an in-band SSE error
            # frame (e.g. a context overflow mid-generation), which the dispatch layer
            # re-raises mid-iteration.
            if last_content in ("__abort_mid_stream__", COMPUTE_ERROR_MID_STREAM):
                reason = (
                    "Compute error."
                    if last_content == COMPUTE_ERROR_MID_STREAM
                    else "engine died mid-generation"
                )

                async def broken():
                    chunk = {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "hel"},
                                "finish_reason": None,
                            }
                        ],
                    }
                    yield f"data: {_dumps(chunk)}\n\n"
                    err = {"error": {"message": reason}}
                    yield f"data: {_dumps(err)}\n\n"

                return StreamingResponse(broken(), media_type="text/event-stream")

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
        captured.authorization = request.headers.get("authorization")
        body = await request.json()
        model = body.get("model", "fake")
        inputs = body.get("input")
        if inputs == "__force_upstream_404__":
            return JSONResponse({"error": {"message": "Not Found"}}, status_code=404)
        if inputs == HOLD_UNTIL_DISCONNECT:
            if (abandoned := await hold_until_caller_hangs_up(request)) is not None:
                return abandoned
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
        captured.authorization = request.headers.get("authorization")
        # Consume the multipart body so the request completes; the transcript is deterministic.
        upload = (await request.form()).get("file")
        audio = b"" if upload is None or isinstance(upload, str) else await upload.read()
        if audio == HOLD_UNTIL_DISCONNECT.encode():
            if (abandoned := await hold_until_caller_hangs_up(request)) is not None:
                return abandoned
        return JSONResponse({"text": FAKE_TRANSCRIPT})

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        captured.authorization = request.headers.get("authorization")
        body = await request.json()
        if body.get("input") == HOLD_UNTIL_DISCONNECT:
            if (abandoned := await hold_until_caller_hangs_up(request)) is not None:
                return abandoned
        return Response(content=FAKE_AUDIO, media_type="audio/mpeg")

    @app.post("/v1/images/generations")
    async def images(request: Request):
        captured.authorization = request.headers.get("authorization")
        body = await request.json()
        if body.get("prompt") == HOLD_UNTIL_DISCONNECT:
            if (abandoned := await hold_until_caller_hangs_up(request)) is not None:
                return abandoned
        return JSONResponse({"created": 0, "data": [{"b64_json": FAKE_IMAGE_B64}]})

    return app, captured


def _dumps(obj: object) -> str:
    import json

    return json.dumps(obj)


class _ThreadedServer:
    """An ASGI app on real uvicorn (ephemeral port) in a daemon thread."""

    def __init__(self, app: FastAPI) -> None:
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


@contextmanager
def serve_on_uvicorn(app: FastAPI) -> Iterator[str]:
    """Serve ``app`` on real uvicorn for the block's duration; yields its base URL. For tests
    that need a real socket to the app under test — a TestClient can never hang up mid-request."""
    server = _ThreadedServer(app)
    port = server.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.stop()


class FakeUpstreamHandle:
    def __init__(self, advertised: Capabilities) -> None:
        app, self._captured = _build_fake_app()
        self._server = _ThreadedServer(app)
        self._port = self._server.start()
        self._advertised = advertised
        self.terminated = False
        #: Set by a test to stand for an engine process that has exited with this code.
        self.exited_with: int | None = None

    @property
    def exit_code(self) -> int | None:
        return self.exited_with

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def port(self) -> int:
        return self._port

    @property
    def last_authorization(self) -> object:
        """The Authorization header this upstream last received (None if none)."""
        return self._captured.authorization

    @property
    def last_chat_body(self) -> dict | None:
        """The JSON body this upstream's chat endpoint last received (None if none)."""
        return self._captured.chat_body

    @property
    def hold_started(self) -> threading.Event:
        """Set once a HOLD_UNTIL_DISCONNECT call reaches this upstream."""
        return self._captured.hold_started

    @property
    def hold_abandoned(self) -> threading.Event:
        """Set once the caller closed the connection on a held call."""
        return self._captured.hold_abandoned

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
