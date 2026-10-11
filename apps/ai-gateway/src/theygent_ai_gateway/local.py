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

import json
import logging
import re
import secrets
from dataclasses import dataclass
from typing import Any

from theygent_gateway_client import GatewayClient
from theygent_ir.gateway import Usage

log = logging.getLogger(__name__)

#: Side requests are small; a local model that runs on is not answering one.
LOCAL_TIMEOUT_SECONDS = 30.0
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


class LocalRoute:
    """Answers translatable side requests with a model on the inference plane."""

    def __init__(self, base_url: str, *, timeout: float | None = None) -> None:
        # No retries: the fallback for a failure is the Anthropic API, not a second local attempt.
        self._client = GatewayClient(
            base_url,
            timeout=timeout if timeout is not None else LOCAL_TIMEOUT_SECONDS,
            max_retries=0,
        )

    async def answer(self, model: str, body: bytes) -> LocalAnswer | None:
        translated = translate(body)
        if translated is None:
            return None
        try:
            completion = await self._client.complete(
                model=model, messages=list(translated.messages), params=translated.params
            )
        except Exception as exc:
            log.warning("the local model did not answer: %s", type(exc).__name__)
            return None
        choice = completion.choices[0] if completion.choices else None
        text = answer_text(choice.message.content) if choice is not None else ""
        if not text:
            log.warning("the local model's answer is empty")
            return None
        if translated.schema is not None:
            try:
                parsed = json.loads(text)
            except ValueError:
                log.warning("the local model's answer is not JSON")
                return None
            if not fits(parsed, translated.schema):
                log.warning("the local model's answer does not fit the schema")
                return None
        usage = Usage(
            input_tokens=completion.usage.prompt_tokens if completion.usage else 0,
            output_tokens=completion.usage.completion_tokens if completion.usage else 0,
        )
        cut_short = choice is not None and choice.finish_reason == "length"
        message = _message(text, model, usage, "max_tokens" if cut_short else "end_turn")
        if translated.stream:
            return LocalAnswer(as_stream(message), "text/event-stream; charset=utf-8")
        return LocalAnswer(json.dumps(message, ensure_ascii=False).encode(), "application/json")

    async def aclose(self) -> None:
        await self._client.aclose()
