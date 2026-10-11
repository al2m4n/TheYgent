"""The local route — router mode only: a side request the Catalog allows to run locally (a session
title, typically), answered by a model on the inference plane instead of the Anthropic API.

The inference plane speaks OpenAI's chat completions, so the request is translated there and the
answer back into an Anthropic message — streamed as server-sent events when Claude Code asked for a
stream. Only what translates faithfully goes: text system blocks and text messages, and a JSON
schema for structured output. Anything else (images, documents, tools, tool results) stays with
the Anthropic API.

The local answer is checked before Claude Code sees it: it must be text, and where the request
asked for structured output, JSON that fits the schema. Any failure — the request does not
translate, the inference plane is down, slow or refuses, the answer does not fit — returns
``None``, and the caller sends the original request to the model Claude Code asked for. A local
model is a candidate for a few cheap side requests; it is never a reason for Claude Code to see an
error.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
from theygent_gateway_client import GatewayClient
from theygent_ir.gateway import ShadowOutcome, Usage

log = logging.getLogger(__name__)

#: An answer checked before Claude Code sees it (structured output) is read whole within this.
LOCAL_TIMEOUT_SECONDS = 30.0
#: A relayed answer must start within this, or the request goes to Anthropic (a model still loading
#: takes about a minute; the cloud answers meanwhile).
FIRST_TOKEN_SECONDS = 15.0
#: A keep-alive ping while the local model is quiet, as the Anthropic API sends them.
PING_SECONDS = 5.0
#: A prompt larger than this, at about four bytes a token, is never sent to a local model.
LOCAL_MAX_PROMPT_TOKENS = 32_768
SSE_CONTENT_TYPE = "text/event-stream; charset=utf-8"
#: Claude Code asks for 128K output on every request; a side request's answer is a few tokens.
LOCAL_MAX_TOKENS = 4096
#: Claude Code's first system block is billing attribution for Anthropic, nothing a model reads.
_BILLING_PREFIX = "x-anthropic-billing-header:"
#: What a local model wraps an answer in that Claude Code would not: a reasoning block it was asked
#: not to produce, and a Markdown code fence around JSON.
_THINKING = re.compile(r"^\s*<think>.*?</think>\s*", re.DOTALL)
_FENCE = re.compile(r"^```[A-Za-z]*\s*\n(.*?)\n?```$", re.DOTALL)
_JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "number": (int, float),
    "integer": (int,),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


@dataclass(frozen=True)
class Translated:
    """An Anthropic request as a chat completion: messages, params, and what the answer must be."""

    messages: list[dict[str, str]]
    params: dict[str, Any]
    schema: dict[str, Any] | None
    stream: bool


@dataclass(frozen=True)
class LocalAnswer:
    """The local model's answer, as Claude Code receives it."""

    body: bytes
    content_type: str


@dataclass(frozen=True)
class LocalAttempt:
    """How asking the local model went: the answer Claude Code would get (``None`` unless
    ``answered``), and what the model used — a shadow run records both."""

    outcome: ShadowOutcome
    answer: LocalAnswer | None = None
    usage: Usage = field(default_factory=Usage)


def _texts(content: Any) -> str | None:
    """The text of a message or system value, or ``None`` if it holds anything but text."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            return None
        text = block.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    return "\n\n".join(parts)


def translate(body: bytes) -> Translated | None:
    """The request as a chat completion, or ``None`` when it does not translate faithfully."""
    try:
        request = json.loads(body)
    except ValueError:
        return None
    if not isinstance(request, dict) or request.get("tools") or request.get("tool_choice"):
        return None
    messages: list[dict[str, str]] = []
    system = request.get("system")
    if system is not None:
        blocks = system if isinstance(system, list) else [{"type": "text", "text": system}]
        kept = [
            b
            for b in blocks
            if not (isinstance(b, dict) and str(b.get("text", "")).startswith(_BILLING_PREFIX))
        ]
        text = _texts(kept)
        if text is None:
            return None
        if text:
            messages.append({"role": "system", "content": text})
    turns = request.get("messages")
    if not isinstance(turns, list) or not turns:
        return None
    for turn in turns:
        if not isinstance(turn, dict) or turn.get("role") not in ("user", "assistant"):
            return None
        text = _texts(turn.get("content"))
        if text is None:
            return None
        messages.append({"role": turn["role"], "content": text})
    asked = request.get("max_tokens")
    params: dict[str, Any] = {
        "max_tokens": min(asked, LOCAL_MAX_TOKENS) if isinstance(asked, int) else LOCAL_MAX_TOKENS,
        # A reasoning model would spend the answer's budget thinking about a title.
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    for name in ("temperature", "top_p"):
        if isinstance(request.get(name), int | float):
            params[name] = request[name]
    stop = request.get("stop_sequences")
    if isinstance(stop, list) and stop and all(isinstance(item, str) for item in stop):
        params["stop"] = stop
    schema: dict[str, Any] | None = None
    output = request.get("output_config")
    if isinstance(output, dict) and output.get("format") is not None:
        fmt = output["format"]
        if not (isinstance(fmt, dict) and fmt.get("type") == "json_schema"):
            return None
        schema = fmt.get("schema")
        if not isinstance(schema, dict):
            return None
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": schema},
        }
    return Translated(messages, params, schema, bool(request.get("stream")))


def fits(value: Any, schema: dict[str, Any]) -> bool:
    """Whether ``value`` fits the schema, for the subset structured output uses: types, required
    properties, no additional ones when the schema says so, and nested properties and items."""
    kind = schema.get("type")
    if isinstance(kind, str):
        allowed = _JSON_TYPES.get(kind)
        if allowed is None or not isinstance(value, allowed):
            return False
        if kind in ("number", "integer") and isinstance(value, bool):
            return False
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(name not in value for name in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(
            fits(item, properties[name])
            for name, item in value.items()
            if isinstance(properties.get(name), dict)
        )
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        return all(fits(item, schema["items"]) for item in value)
    return True


def answer_text(content: str | None) -> str:
    """The answer as Claude Code should read it: no reasoning block, no code fence."""
    text = _THINKING.sub("", content or "").strip()
    fenced = _FENCE.match(text)
    return fenced.group(1).strip() if fenced else text


def _message(text: str, model: str, usage: Usage, stop_reason: str) -> dict[str, Any]:
    return {
        "id": f"msg_local_{secrets.token_hex(12)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.input_tokens,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            "output_tokens": usage.output_tokens,
        },
    }


def _event(name: str, data: dict[str, Any]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def as_stream(message: dict[str, Any]) -> bytes:
    """An Anthropic message as the server-sent events Claude Code reads from a stream."""
    usage = message["usage"]
    start = {**message, "content": [], "stop_reason": None, "usage": {**usage, "output_tokens": 0}}
    text = message["content"][0]["text"]
    return b"".join(
        [
            _event("message_start", {"type": "message_start", "message": start}),
            _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _event(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            _event("content_block_stop", {"type": "content_block_stop", "index": 0}),
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
    )


@dataclass
class LocalStream:
    """A local answer relayed as the model writes it: Anthropic server-sent events. ``close``
    ends the engine's stream (a client that hung up stops the model)."""

    events: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
    content_type: str = SSE_CONTENT_TYPE


@dataclass
class _Written:
    """What a local model wrote, read whole: its text, usage and why it stopped."""

    text: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None


def _too_large(body: bytes) -> bool:
    return len(body) // 4 > LOCAL_MAX_PROMPT_TOKENS


def _usage_of(chunk: Any) -> Usage | None:
    usage = getattr(chunk, "usage", None)
    if usage is None:
        return None
    return Usage(
        input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )


def _delta_text(chunk: Any) -> tuple[str, str | None]:
    """A chunk's visible text and finish reason (reasoning is not part of the answer)."""
    choices = getattr(chunk, "choices", None) or []
    if not choices:
        return "", None
    choice = choices[0]
    delta = getattr(choice, "delta", None)
    return (getattr(delta, "content", None) or ""), getattr(choice, "finish_reason", None)


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
        self._first_token = (
            first_token_seconds if first_token_seconds is not None else FIRST_TOKEN_SECONDS
        )
        self._client = GatewayClient(base_url, timeout=self._timeout, max_retries=0)

    async def _open(self, model: str, translated: Translated) -> Any | None:
        try:
            return await self._client.open_stream(
                model=model,
                messages=list(translated.messages),
                params=translated.params,
                include_usage=True,
            )
        except Exception as exc:
            log.warning("the local model did not answer: %s", type(exc).__name__)
            return None

    async def answer(self, model: str, body: bytes) -> LocalAnswer | LocalStream | None:
        """What Claude Code gets, or ``None`` when the request goes upstream instead. An answer that
        must be checked first (structured output, or a client that did not stream) is read whole;
        any other is relayed as it is written, once the model has started within the first-token
        time."""
        translated = translate(body)
        if translated is None or _too_large(body):
            return None
        if translated.stream and translated.schema is None:
            return await self._relay(model, translated)
        return (await self._checked(model, translated)).answer

    async def attempt(self, model: str, body: bytes) -> LocalAttempt:
        """How the local model does on this request, read whole and checked (a shadow run)."""
        translated = translate(body)
        if translated is None or _too_large(body):
            return LocalAttempt("untranslatable")
        return await self._checked(model, translated)

    async def _read_whole(self, stream: Any) -> _Written:
        written = _Written()
        async for chunk in stream:
            text, finish = _delta_text(chunk)
            written.text += text
            written.finish_reason = finish or written.finish_reason
            written.usage = _usage_of(chunk) or written.usage
        return written

    async def _checked(self, model: str, translated: Translated) -> LocalAttempt:
        stream = await self._open(model, translated)
        if stream is None:
            return LocalAttempt("failed")
        try:
            written = await asyncio.wait_for(self._read_whole(stream), self._timeout)
        except Exception as exc:
            log.warning("the local model did not finish: %s", type(exc).__name__)
            return LocalAttempt("failed")
        finally:
            with anyio.CancelScope(shield=True):
                await _close(stream)
        usage = written.usage
        text = answer_text(written.text)
        if not text:
            log.warning("the local model's answer is empty")
            return LocalAttempt("unusable", usage=usage)
        if translated.schema is not None:
            try:
                parsed = json.loads(text)
            except ValueError:
                log.warning("the local model's answer is not JSON")
                return LocalAttempt("unusable", usage=usage)
            if not fits(parsed, translated.schema):
                log.warning("the local model's answer does not fit the schema")
                return LocalAttempt("unusable", usage=usage)
        cut_short = written.finish_reason == "length"
        message = _message(text, model, usage, "max_tokens" if cut_short else "end_turn")
        if translated.stream:
            answer = LocalAnswer(as_stream(message), SSE_CONTENT_TYPE)
        else:
            answer = LocalAnswer(
                json.dumps(message, ensure_ascii=False).encode(), "application/json"
            )
        return LocalAttempt("answered", answer, usage)

    async def _relay(self, model: str, translated: Translated) -> LocalStream | None:
        opened: list[Any] = []
        thinking = _ThinkFilter()
        state = _Written()

        async def start() -> tuple[Any, AsyncIterator[Any], str]:
            # Opening the stream and the first visible text share one deadline: a server slow to
            # answer at all is as late as a model slow to start.
            stream = await self._client.open_stream(
                model=model,
                messages=list(translated.messages),
                params=translated.params,
                include_usage=True,
            )
            opened.append(stream)
            chunks = stream.__aiter__()
            while True:
                chunk = await chunks.__anext__()
                text, finish = _delta_text(chunk)
                state.usage = _usage_of(chunk) or state.usage
                state.finish_reason = finish or state.finish_reason
                if visible := thinking.feed(text):
                    return stream, chunks, visible
                if finish is not None:
                    raise StopAsyncIteration

        try:
            stream, chunks, first = await asyncio.wait_for(start(), self._first_token)
        except (Exception, StopAsyncIteration) as exc:
            log.warning("the local model did not start answering: %s", type(exc).__name__)
            with anyio.CancelScope(shield=True):
                for stream in opened:
                    await _close(stream)
            return None

        async def events() -> AsyncIterator[bytes]:
            message_id = f"msg_local_{secrets.token_hex(12)}"
            start = {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            }
            yield _event("message_start", {"type": "message_start", "message": start})
            yield _event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            yield _text_delta(first)
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
                    text, finish = _delta_text(chunk)
                    state.usage = _usage_of(chunk) or state.usage
                    state.finish_reason = finish or state.finish_reason
                    if visible := thinking.feed(text):
                        yield _text_delta(visible)
            finally:
                if pending is not None:
                    pending.cancel()
                with anyio.CancelScope(shield=True):
                    await _close(stream)
            yield _event("content_block_stop", {"type": "content_block_stop", "index": 0})
            stop = "max_tokens" if state.finish_reason == "length" else "end_turn"
            yield _event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": stop, "stop_sequence": None},
                    "usage": {
                        "input_tokens": state.usage.input_tokens,
                        "output_tokens": state.usage.output_tokens,
                    },
                },
            )
            yield _event("message_stop", {"type": "message_stop"})

        async def close() -> None:
            with anyio.CancelScope(shield=True):
                await _close(stream)

        return LocalStream(events(), close)

    async def aclose(self) -> None:
        await self._client.aclose()


def _text_delta(text: str) -> bytes:
    return _event(
        "content_block_delta",
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
    )


async def _close(stream: Any) -> None:
    """Close the engine's stream; the inference plane then stops the model."""
    try:
        await stream.close()
    except Exception as exc:
        log.warning("could not close the local stream: %s", type(exc).__name__)
