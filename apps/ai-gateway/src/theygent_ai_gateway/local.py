"""The local route — router mode only: a request answered by a model on the inference plane instead
of the Anthropic API.

The inference plane speaks OpenAI's chat completions, so the request is translated there
(``translation``) and the answer back into Anthropic's shape: server-sent events when Claude Code
asked for a stream, a message otherwise. Text and tool calls both come back; a model that follows
tool calls is given Claude Code's tools.

The gateway always streams from the engine: a client that hangs up closes the stream, which stops
the model — a non-streaming request would hold the engine until it finished. A prompt larger than
the model takes is never sent.

Nothing reaches Claude Code until the local answer is worth sending:

* an answer that must be checked first — structured output, or a client that did not stream — is
  read whole, then checked: text, JSON that fits the schema where one was asked for, tool calls
  that name an offered tool with arguments that are a JSON object;
* any other answer is relayed as it is written, once it has started — visible text, or a first
  tool call that checks out — within the first-output time. A tool call is relayed whole, once
  its arguments are.

Any failure before then — the request does not translate, the inference plane is down, slow or
refuses, the answer does not check out — returns ``None``, and the caller sends the original
request to the model Claude Code asked for. After it, bytes are out: a model that breaks off ends
the response, as an upstream that breaks would.
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
from theygent_gateway_client import GatewayClient
from theygent_ir.gateway import ShadowOutcome, Usage

from theygent_ai_gateway.translation import (
    LOCAL_AGENT_MAX_TOKENS,
    LOCAL_MAX_TOKENS,
    Translated,
    answer_text,
    fits,
    translate,
)

__all__ = [
    "LOCAL_AGENT_MAX_TOKENS",
    "LOCAL_MAX_TOKENS",
    "LocalAnswer",
    "LocalAttempt",
    "LocalRoute",
    "LocalStream",
    "Translated",
    "answer_text",
    "as_stream",
    "fits",
    "translate",
]

log = logging.getLogger(__name__)

#: An answer checked before Claude Code sees it is read whole within this; a relayed answer must
#: have started (its first visible text, or its first tool call whole) within it.
LOCAL_TIMEOUT_SECONDS = 30.0
#: A relayed answer's first output — text, or a tool call's name — must come within this, or the
#: request goes to Anthropic (a model still loading takes about a minute; the cloud answers
#: meanwhile).
FIRST_TOKEN_SECONDS = 15.0
#: A model reads the whole prompt before its first token, so the first-output deadline grows with
#: the prompt: at least this reading speed is allowed for (tokens a second). A Claude Code agent
#: turn is 10K to 20K tokens; a 4B model on a laptop reads about 300 a second.
MIN_PROMPT_TOKENS_PER_SECOND = 200
#: The HTTP client's own limit on a quiet engine: only a backstop, the deadlines above govern.
CLIENT_TIMEOUT_SECONDS = 600.0
#: A keep-alive ping while the local model is quiet, as the Anthropic API sends them.
PING_SECONDS = 5.0
#: A prompt larger than this, at about four bytes a token, is never sent to a local model.
LOCAL_MAX_PROMPT_TOKENS = 32_768
SSE_CONTENT_TYPE = "text/event-stream; charset=utf-8"


@dataclass(frozen=True)
class LocalAnswer:
    """The local model's answer, as Claude Code receives it."""

    body: bytes
    content_type: str


@dataclass
class LocalStream:
    """A local answer relayed as the model writes it: Anthropic server-sent events. ``close``
    ends the engine's stream (a client that hung up stops the model)."""

    events: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
    content_type: str = SSE_CONTENT_TYPE


@dataclass(frozen=True)
class LocalAttempt:
    """How asking the local model went: the answer Claude Code would get (``None`` unless
    ``answered``), what the model used, the tools it called and when it started — a shadow run
    records them."""

    outcome: ShadowOutcome
    answer: LocalAnswer | None = None
    usage: Usage = field(default_factory=Usage)
    tool_calls: tuple[str, ...] = ()
    first_output_ms: int | None = None


class _Unusable(Exception):
    """The answer would not be used: a tool call that names no offered tool, or whose arguments
    are not a JSON object, before anything was sent."""


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def _message(
    content: list[dict[str, Any]], model: str, usage: Usage, stop_reason: str
) -> dict[str, Any]:
    return {
        "id": f"msg_local_{secrets.token_hex(12)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.input_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": usage.output_tokens,
        },
    }


def as_stream(message: dict[str, Any]) -> bytes:
    """An Anthropic message as the server-sent events Claude Code reads from a stream."""
    usage = message["usage"]
    start = {**message, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 0}}
    out = [_event("message_start", {"type": "message_start", "message": start})]
    for index, block in enumerate(message["content"]):
        out += _block_events(index, block)
    out += [
        _event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                "usage": {"output_tokens": usage["output_tokens"]},
            },
        ),
        _event("message_stop", {"type": "message_stop"}),
    ]
    return b"".join(out)


def _block_events(index: int, block: dict[str, Any]) -> list[bytes]:
    """One whole content block as its start, delta and stop events."""
    if block["type"] == "text":
        start = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": block["text"]}
    else:
        start = {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}
        delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
    return [
        _event(
            "content_block_start",
            {"type": "content_block_start", "index": index, "content_block": start},
        ),
        _event(
            "content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta}
        ),
        _event("content_block_stop", {"type": "content_block_stop", "index": index}),
    ]


class _ThinkFilter:
    """Drops a leading ``<think>…</think>`` block, and the blank space after it, from text that
    arrives in pieces."""

    def __init__(self) -> None:
        self._buffer = ""
        self._open = True  # still deciding whether the answer starts with a reasoning block
        self._trim = False  # just past a reasoning block: drop leading blank space

    def feed(self, piece: str) -> str:
        if not self._open:
            return self._trimmed(piece)
        self._buffer += piece
        stripped = self._buffer.lstrip()
        if not stripped or ("<think>".startswith(stripped) and len(stripped) < 7):
            return ""  # nothing yet, or what could still become "<think>"
        if not stripped.startswith("<think>"):
            self._open = False
            return self._buffer
        end = stripped.find("</think>")
        if end < 0:
            return ""
        self._open, self._trim = False, True
        return self._trimmed(stripped[end + len("</think>") :])

    def _trimmed(self, piece: str) -> str:
        if self._trim:
            piece = piece.lstrip()
            self._trim = not piece
        return piece


@dataclass
class _Call:
    """A tool call being written: its arguments arrive in pieces."""

    id: str
    name: str
    arguments: str = ""


class _Writer:
    """OpenAI stream chunks in, Anthropic server-sent events out (after ``message_start``, which
    the relay sends once committed). Text streams as deltas; a tool call is emitted whole once the
    next one starts or the stream ends. ``committed``: something was emitted — from then on
    nothing can be taken back."""

    def __init__(self, tool_names: frozenset[str]) -> None:
        self._tool_names = tool_names
        self._think = _ThinkFilter()
        self._index = 0
        self._text_open = False
        self._calls: dict[int, _Call] = {}
        self._current: int | None = None
        self.content: list[dict[str, Any]] = []
        self.usage = Usage()
        self.finish_reason: str | None = None
        self.committed = False
        self.started = False  # any output at all: text, or a tool call's name
        self.called: list[str] = []
        self.stop_reason = "end_turn"

    def feed(self, chunk: Any) -> list[bytes]:
        usage = getattr(chunk, "usage", None)
        if usage is not None:
            self.usage = Usage(
                input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            )
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            return []
        choice = choices[0]
        self.finish_reason = getattr(choice, "finish_reason", None) or self.finish_reason
        delta = getattr(choice, "delta", None)
        out: list[bytes] = []
        if text := self._think.feed(getattr(delta, "content", None) or ""):
            out += self._text(text)
        for call in getattr(delta, "tool_calls", None) or []:
            out += self._tool_piece(call)
        return out

    def _text(self, text: str) -> list[bytes]:
        self.started = self.committed = True
        out: list[bytes] = []
        if not self._text_open:
            self._text_open = True
            self.content.append({"type": "text", "text": ""})
            out.append(
                _event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": self._index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            )
        self.content[-1]["text"] += text
        out.append(
            _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": self._index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
        return out

    def _close_text(self) -> list[bytes]:
        if not self._text_open:
            return []
        self._text_open = False
        stop = _event("content_block_stop", {"type": "content_block_stop", "index": self._index})
        self._index += 1
        return [stop]

    def _tool_piece(self, piece: Any) -> list[bytes]:
        index = getattr(piece, "index", 0) or 0
        function = getattr(piece, "function", None)
        out: list[bytes] = []
        if index not in self._calls:
            out += self._flush()  # a new call starts: the one before it is whole
            self._calls[index] = _Call(
                id=f"toolu_local_{secrets.token_hex(10)}",
                name=getattr(function, "name", None) or "",
            )
            self._current = index
            self.started = True
        call = self._calls[index]
        name = getattr(function, "name", None)
        if not call.name and name:
            call.name = name
        call.arguments += getattr(function, "arguments", None) or ""
        return out

    def _flush(self) -> list[bytes]:
        """Emit the call being written, whole."""
        if self._current is None:
            return []
        call = self._calls[self._current]
        self._current = None
        try:
            arguments = json.loads(call.arguments or "{}")
        except ValueError:
            arguments = None
        valid = call.name in self._tool_names and isinstance(arguments, dict)
        if not valid and not self.committed:
            raise _Unusable
        block = {
            "type": "tool_use",
            "id": call.id,
            "name": call.name,
            # Past the point of no return a bad call still goes out: Claude Code answers it with
            # a tool error, as it would any model's mistake.
            "input": arguments if isinstance(arguments, dict) else {},
        }
        out = self._close_text()
        out += _block_events(self._index, block)
        self._index += 1
        self.content.append(block)
        self.called.append(call.name)
        self.committed = True
        return out

    def finish(self) -> list[bytes]:
        """The closing events once the stream has ended."""
        out = self._flush() + self._close_text()
        if self.called:
            stop = "tool_use"
        elif self.finish_reason == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        self.stop_reason = stop
        return [
            *out,
            _event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {
                        "input_tokens": self.usage.input_tokens,
                        "output_tokens": self.usage.output_tokens,
                    },
                },
            ),
            _event("message_stop", {"type": "message_stop"}),
        ]


def _reading_seconds(body: bytes) -> float:
    """How long reading this prompt may take, at the slowest speed allowed for."""
    return (len(body) / 4) / MIN_PROMPT_TOKENS_PER_SECOND


def _too_large(body: bytes, context_tokens: int | None) -> bool:
    limit = LOCAL_MAX_PROMPT_TOKENS if context_tokens is None else context_tokens
    return len(body) // 4 > limit


class LocalRoute:
    """Answers translatable requests with a model on the inference plane, streaming from it."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float | None = None,
        first_token_seconds: float | None = None,
    ) -> None:
        # No retries: the fallback for a failure is the Anthropic API, not a second local attempt.
        self._timeout = timeout if timeout is not None else LOCAL_TIMEOUT_SECONDS
        self._first_token = first_token_seconds
        self._client = GatewayClient(base_url, timeout=CLIENT_TIMEOUT_SECONDS, max_retries=0)

    async def _open(self, model: str, translated: Translated) -> Any:
        return await self._client.open_stream(
            model=model,
            messages=list(translated.messages),
            params=translated.params,
            tools=translated.tools,
            tool_choice=translated.tool_choice,
            include_usage=True,
        )

    async def answer(
        self,
        model: str,
        body: bytes,
        *,
        tools: bool = False,
        images: bool = False,
        context_tokens: int | None = None,
    ) -> LocalAnswer | LocalStream | None:
        """What Claude Code gets, or ``None`` when the request goes upstream instead. ``tools``,
        ``images`` and ``context_tokens``: what the model can take."""
        translated = translate(body, tools=tools, images=images)
        if translated is None or _too_large(body, context_tokens):
            return None
        reading = _reading_seconds(body)
        if translated.stream and translated.schema is None:
            return await self._relay(model, translated, reading)
        return (await self._checked(model, translated, reading)).answer

    async def attempt(
        self,
        model: str,
        body: bytes,
        *,
        tools: bool = False,
        images: bool = False,
        context_tokens: int | None = None,
    ) -> LocalAttempt:
        """How the local model does on this request, read whole and checked (a shadow run)."""
        translated = translate(body, tools=tools, images=images)
        if translated is None or _too_large(body, context_tokens):
            return LocalAttempt("untranslatable")
        return await self._checked(model, translated, _reading_seconds(body))

    async def _checked(self, model: str, translated: Translated, reading: float) -> LocalAttempt:
        started = time.perf_counter()
        first: list[int] = []
        opened: list[Any] = []
        writer = _Writer(translated.tool_names)

        async def read() -> None:
            # Opening the stream shares the deadline: a server slow to answer at all is as late
            # as a model slow to finish.
            stream = await self._open(model, translated)
            opened.append(stream)
            async for chunk in stream:
                writer.feed(chunk)
                if writer.started and not first:
                    first.append(int((time.perf_counter() - started) * 1000))
            writer.finish()

        try:
            await asyncio.wait_for(read(), self._timeout + reading)
        except _Unusable:
            log.warning("the local model called a tool it was not offered, or mangled one")
            return LocalAttempt("unusable", usage=writer.usage)
        except Exception as exc:
            what = "finish" if opened else "answer"
            log.warning("the local model did not %s: %s", what, type(exc).__name__)
            return LocalAttempt("failed")
        finally:
            with anyio.CancelScope(shield=True):
                for stream in opened:
                    await _close(stream)
        timing = first[0] if first else None
        usage = writer.usage
        calls = tuple(writer.called)
        texts = [b for b in writer.content if b["type"] == "text"]
        if not calls:
            text = answer_text("".join(b["text"] for b in texts))
            if not text:
                log.warning("the local model's answer is empty")
                return LocalAttempt("unusable", usage=usage, first_output_ms=timing)
            if translated.schema is not None:
                try:
                    parsed = json.loads(text)
                except ValueError:
                    log.warning("the local model's answer is not JSON")
                    return LocalAttempt("unusable", usage=usage, first_output_ms=timing)
                if not fits(parsed, translated.schema):
                    log.warning("the local model's answer does not fit the schema")
                    return LocalAttempt("unusable", usage=usage, first_output_ms=timing)
            content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        else:
            content = [
                {**b, "text": b["text"].strip()} if b["type"] == "text" else b
                for b in writer.content
                if b["type"] != "text" or b["text"].strip()
            ]
        message = _message(content, model, usage, writer.stop_reason)
        if translated.stream:
            answer = LocalAnswer(as_stream(message), SSE_CONTENT_TYPE)
        else:
            answer = LocalAnswer(
                json.dumps(message, ensure_ascii=False).encode(), "application/json"
            )
        return LocalAttempt("answered", answer, usage, calls, timing)

    async def _relay(
        self, model: str, translated: Translated, reading: float
    ) -> LocalStream | None:
        opened: list[Any] = []
        writer = _Writer(translated.tool_names)
        held: list[bytes] = []  # events written before the answer was committed
        exhausted = False
        base = self._first_token if self._first_token is not None else FIRST_TOKEN_SECONDS
        first_deadline = base + reading

        async def start() -> AsyncIterator[Any]:
            # Opening the stream, the first output and the commit share the deadlines: a server
            # slow to answer at all is as late as a model slow to start.
            nonlocal exhausted
            began = time.monotonic()
            started_at: float | None = None
            stream = await asyncio.wait_for(self._open(model, translated), first_deadline)
            opened.append(stream)
            chunks = stream.__aiter__()
            while not writer.committed:
                if writer.started and started_at is None:
                    started_at = time.monotonic()
                # Before any output: the first-output deadline from the start. After it: the
                # commit (a first tool call whole) gets its own time from when output began.
                deadline = (
                    began + first_deadline if started_at is None else started_at + self._timeout
                )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                try:
                    chunk = await asyncio.wait_for(chunks.__anext__(), remaining)
                except StopAsyncIteration:
                    exhausted = True
                    held.extend(writer.finish())
                    if not writer.committed:
                        raise _Unusable from None  # it ended without saying anything
                    break
                held.extend(writer.feed(chunk))
            return chunks

        try:
            chunks = await start()
        except (Exception, StopAsyncIteration) as exc:
            log.warning("the local model did not start a usable answer: %s", type(exc).__name__)
            with anyio.CancelScope(shield=True):
                for stream in opened:
                    await _close(stream)
            return None
        stream = opened[0]

        async def events() -> AsyncIterator[bytes]:
            start = {
                "id": f"msg_local_{secrets.token_hex(12)}",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
            yield _event("message_start", {"type": "message_start", "message": start})
            for event in held:
                yield event
            if exhausted:
                return
            pending: asyncio.Future[Any] | None = None
            try:
                while True:
                    pending = pending or asyncio.ensure_future(chunks.__anext__())
                    done, _ = await asyncio.wait({pending}, timeout=PING_SECONDS)
                    if not done:
                        yield _event("ping", {"type": "ping"})
                        continue
                    try:
                        chunk = pending.result()
                    except StopAsyncIteration:
                        break
                    finally:
                        pending = None
                    for event in writer.feed(chunk):
                        yield event
            finally:
                if pending is not None:
                    pending.cancel()
                with anyio.CancelScope(shield=True):
                    await _close(stream)
            for event in writer.finish():
                yield event

        async def close() -> None:
            with anyio.CancelScope(shield=True):
                await _close(stream)

        return LocalStream(events(), close)

    async def aclose(self) -> None:
        await self._client.aclose()


async def _close(stream: Any) -> None:
    """Close the engine's stream; the inference plane then stops the model."""
    try:
        await stream.close()
    except Exception as exc:
        log.warning("could not close the local stream: %s", type(exc).__name__)
