"""A real, threaded, OpenAI-compatible *fake inference plane* for the fast suite.

Everything real except the model: a genuine uvicorn server on an ephemeral port so the
control-plane's gateway-client really talks HTTP across the seam (the in-process
shortcut defeats the purpose of testing the real HTTP boundary). Mirrors the inference plane's own
``tests/_fake_upstream._ThreadedServer`` mechanics.

Selectable ``mode`` exercises the control-plane's error paths:
  * ``normal``         — deterministic SSE + non-stream completion
  * ``error_503``      — 503 engine_unavailable (engine not ready)
  * ``error_404``      — 404 model_not_found (unknown logical id)
  * ``drop_midstream`` — stream a chunk then drop the connection mid-stream
  * ``reasoning``      — a reasoning model: emits ``reasoning_content`` deltas then ``content``
  * ``empty_length``   — emits only ``reasoning_content`` then ``finish_reason: length`` (the
                         budget-exhausted-no-answer case surfaced honestly)
  * ``inline_think``   — a raw-template server: the thinking travels INLINE in ``content`` as
                         ``<think>…</think>``, with BOTH tags split across chunk boundaries
                         (the shape the splitter must never leak)
  * ``inline_think_both``     — ``inline_think`` plus a separate ``reasoning_content`` delta
                                (both forms in one stream)
  * ``inline_think_unclosed`` — the block never closes and no answer follows;
                                ``finish_reason: length`` (all-thinking, inline form)

An optional ``response`` overrides the streamed/returned content (default ``"hello world"``).
The optional ``response`` override makes an ``llm`` node emit a deterministic routing decision
(e.g. a JSON ``{"handle": "yes", ...}``) so the combined ``llm → router`` graph is fully
deterministic.
"""

from __future__ import annotations

import json
import threading
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

FULL_MESSAGE = "hello world"
_CHUNKS = ["hello", " world"]
# The inline-think modes' thinking text — importable so tests assert the reasoning stream verbatim.
INLINE_THINKING = "deep thoughts"


def _build_app(
    mode: str,
    captured: dict[str, object],
    response: str | None,
    tool_name: str = "search",
    tool_args: dict[str, object] | None = None,
) -> FastAPI:
    app = FastAPI()
    content = response if response is not None else FULL_MESSAGE
    tool_args = {"query": "cats"} if tool_args is None else tool_args

    @app.get("/readyz")
    async def readyz() -> dict[str, str]:
        return {"status": "ready"}

    @app.get("/v1/models")
    async def models() -> dict[str, object]:
        return {
            "object": "list",
            "data": [{"id": "triage-fast", "object": "model", "owned_by": "theygent"}],
        }

    # The audio data-plane endpoints (the control-plane's transcribe/speak nodes call
    # THESE — proving the bytes go to the inference base URL, never a control-plane route).
    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        form = await request.form()
        captured["audio_hit"] = True
        captured["audio_model"] = form.get("model")
        captured["audio_bytes_in"] = len(await form["file"].read()) if "file" in form else 0
        return JSONResponse({"text": response if response is not None else "transcribed text"})

    @app.post("/v1/audio/speech")
    async def speech(request: Request):
        body = await request.json()
        captured["audio_hit"] = True
        captured["audio_model"] = body.get("model")
        return Response(content=b"FAKE_AUDIO_BYTES", media_type="audio/mpeg")

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        # Prove the control-plane forwards the run-id header and the logical id verbatim.
        captured["run_id_header"] = request.headers.get("x-theygent-run-id")
        captured["model"] = body.get("model")
        # Capture the full messages array so session-replay tests can assert the prior
        # turns (in order) were prepended to the new input.
        captured["messages"] = body.get("messages")

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
        # Streamed token usage mirrors the real engines: the final chunk (choices: [] + usage)
        # is emitted ONLY when the request asked via stream_options.include_usage — so a test
        # asserting usage landed proves the control-plane actually requested it.
        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        captured["stream_options"] = body.get("stream_options")
        # Tool-calling: the scripted loop emits a tool_call on the FIRST turn, then — once a
        # ``{role: tool}`` result is in the transcript — the final answer. Stateless (driven by the
        # request) so the control-plane's loop terminates naturally.
        messages_in = body.get("messages") or []
        has_tool_result = any(isinstance(m, dict) and m.get("role") == "tool" for m in messages_in)
        # ``tool_call`` answers once a tool result is in the transcript; ``tool_loop`` NEVER answers
        # (always re-requests the tool) — the cap test exercises maxToolIterations.
        wants_answer = has_tool_result and mode != "tool_loop"
        if body.get("stream"):
            return StreamingResponse(
                _stream(
                    model,
                    mode="tool_call" if mode in ("tool_call", "tool_loop") else mode,
                    content=content,
                    tool_spec=(tool_name, tool_args, wants_answer),
                    include_usage=include_usage,
                ),
                media_type="text/event-stream",
            )
        # Non-stream: a reasoning model carries its thinking in `reasoning_content`; the
        # budget-exhausted case returns empty content + finish_reason=length.
        msg: dict[str, object] = {"role": "assistant", "content": content}
        finish = "stop"
        if mode == "reasoning":
            msg["reasoning_content"] = "thinking step..."
        elif mode == "empty_length":
            msg = {"role": "assistant", "content": "", "reasoning_content": "thinking step..."}
            finish = "length"
        elif mode in ("inline_think", "inline_think_both"):
            # Raw-template shape: the whole message carries the thinking inline.
            msg["content"] = f"<think>{INLINE_THINKING}</think>{content}"
            if mode == "inline_think_both":
                msg["reasoning_content"] = "field thinking..."
        elif mode == "inline_think_unclosed":
            msg = {"role": "assistant", "content": f"<think>{INLINE_THINKING}"}
            finish = "length"
        elif mode in ("tool_call", "tool_loop") and not wants_answer:
            msg = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": tool_name, "arguments": json.dumps(tool_args)},
                    }
                ],
            }
            finish = "tool_calls"
        return JSONResponse(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
                "usage": USAGE,
            }
        )

    return app


def _chunk(model: str, delta: dict, finish: str | None = None) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
        )
        + "\n\n"
    )


# One model turn's deterministic usage — matches the non-stream response body so both paths meter
# identically (a tool loop's N turns accumulate N of these on the llm node span).
USAGE = {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


def _usage_chunk(model: str) -> str:
    # The real-engine terminal usage chunk shape: empty choices + a top-level usage object,
    # emitted after the finish_reason chunk, before [DONE].
    return (
        "data: "
        + json.dumps(
            {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": 0,
                "model": model,
                "choices": [],
                "usage": USAGE,
            }
        )
        + "\n\n"
    )


async def _stream(
    model: str, *, mode: str, content: str = FULL_MESSAGE, tool_spec=None, include_usage=False
):
    def _tail():
        # Every completed turn reports usage when asked (real engines meter tool-call turns too).
        if include_usage:
            yield _usage_chunk(model)
        yield "data: [DONE]\n\n"

    # Tool-calling: stream a tool_call (fragmented arguments — proves the accumulator) on the
    # first turn, then the final answer once the tool result is in the transcript.
    if mode == "tool_call":
        name, args, has_result = tool_spec
        if has_result:
            yield _chunk(model, {"role": "assistant", "content": content})
            yield _chunk(model, {}, finish="stop")
            for piece in _tail():
                yield piece
            return
        yield _chunk(
            model,
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": name, "arguments": ""},
                    }
                ],
            },
        )
        argstr = json.dumps(args)
        mid = max(len(argstr) // 2, 1)
        yield _chunk(model, {"tool_calls": [{"index": 0, "function": {"arguments": argstr[:mid]}}]})
        yield _chunk(model, {"tool_calls": [{"index": 0, "function": {"arguments": argstr[mid:]}}]})
        yield _chunk(model, {}, finish="tool_calls")
        for piece in _tail():
            yield piece
        return

    # A raw-template server: thinking INLINE in `content` as <think> tags, with BOTH the
    # opening and the closing tag split across chunk boundaries — the worst case the
    # control-plane's splitter must handle without leaking half a tag.
    if mode in ("inline_think", "inline_think_both", "inline_think_unclosed"):
        if mode == "inline_think_both":
            # Both forms at once: a separate reasoning_content delta AND inline tags.
            yield _chunk(model, {"role": "assistant", "reasoning_content": "field thinking..."})
        yield _chunk(model, {"role": "assistant", "content": "<th"})
        if mode == "inline_think_unclosed":
            # Budget exhausted mid-think: the block never closes, no answer follows.
            yield _chunk(model, {"content": f"ink>{INLINE_THINKING}"})
            yield _chunk(model, {}, finish="length")
            for piece in _tail():
                yield piece
            return
        think_mid = len(INLINE_THINKING) // 2
        yield _chunk(model, {"content": f"ink>{INLINE_THINKING[:think_mid]}"})
        yield _chunk(model, {"content": f"{INLINE_THINKING[think_mid:]}</thi"})
        answer_chunks = _CHUNKS if content == FULL_MESSAGE else [content]
        yield _chunk(model, {"content": "nk>" + answer_chunks[0]})
        for piece in answer_chunks[1:]:
            yield _chunk(model, {"content": piece})
        yield _chunk(model, {}, finish="stop")
        for piece in _tail():
            yield piece
        return

    # A reasoning model emits `reasoning_content` deltas BEFORE its `content` answer.
    if mode in ("reasoning", "empty_length"):
        yield _chunk(model, {"role": "assistant", "reasoning_content": "thinking "})
        yield _chunk(model, {"reasoning_content": "step..."})
        if mode == "empty_length":
            # Budget exhausted during reasoning: no content, finish_reason=length (the no-answer).
            yield _chunk(model, {}, finish="length")
            for piece in _tail():
                yield piece
            return
        yield _chunk(model, {"content": content})
        yield _chunk(model, {}, finish="stop")
        for piece in _tail():
            yield piece
        return

    # Default content streams in two chunks (proves reassembly); a custom response streams whole.
    chunks = _CHUNKS if content == FULL_MESSAGE else [content]
    for i, piece in enumerate(chunks):
        delta = {"content": piece}
        if i == 0:
            delta["role"] = "assistant"
        yield _chunk(model, delta)
        if mode == "drop_midstream":
            # Simulate inference dying mid-stream: stop emitting without [DONE] and
            # raise so the connection drops, exactly the mid-stream-failure case.
            raise RuntimeError("inference dropped mid-stream")
    yield _chunk(model, {}, finish="stop")
    for piece in _tail():
        yield piece


class FakeInference:
    """A real OpenAI-compatible HTTP server on an ephemeral port (context manager)."""

    def __init__(
        self,
        mode: str = "normal",
        response: str | None = None,
        tool_name: str = "search",
        tool_args: dict[str, object] | None = None,
    ) -> None:
        self.captured: dict[str, object] = {
            "run_id_header": None,
            "model": None,
            "messages": None,
            "stream_options": None,
            "audio_hit": False,
            "audio_model": None,
            "audio_bytes_in": 0,
        }
        app = _build_app(mode, self.captured, response, tool_name, tool_args)
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
