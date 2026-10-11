"""Anthropic Messages ⇄ OpenAI chat completions, for the local route.

A request translates faithfully or not at all (``None``): the local model must see what the
Anthropic model would have seen, or the request stays with Anthropic.

What translates:

* **The system prompt.** Text blocks, less Claude Code's billing attribution. Mid-conversation
  ``system`` messages translate in place: their text, and the tools their ``tool_addition`` blocks
  add (how tool search loads tools into a conversation).
* **Messages.** Text; images, to a model that takes them. With a model that follows tool calls:
  ``tool_use`` blocks become an assistant's ``tool_calls``, and ``tool_result`` blocks become
  ``tool`` messages. Thinking blocks are dropped: a local model's turn carries none, and it is
  not shown the cloud model's.
* **Tools.** Every tool not deferred (a deferred tool is not in the model's prompt until tool
  search loads it, which arrives as a ``tool_addition``), as OpenAI functions; ``tool_choice``
  mapped.
* **Settings.** The output budget (capped), sampling, stop sequences, and an output schema as a
  ``json_schema`` response format. Thinking is off: a local model would spend the budget on it.

What does not translate: documents, server tools and their results, a tool result holding an
image, a block of any type this module does not know, and tools or images for a model without
them.

``fits`` and ``answer_text`` check an answer that must be checked before Claude Code sees it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

#: The output budget for an answer that is a few words (structured output, a title).
LOCAL_MAX_TOKENS = 4096
#: The output budget for an agent turn: a tool call can carry a whole file.
LOCAL_AGENT_MAX_TOKENS = 16_384
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
_SKIPPED_BLOCKS = frozenset({"thinking", "redacted_thinking"})


@dataclass(frozen=True)
class Translated:
    """An Anthropic request as a chat completion: messages, tools, params, and what the answer
    must be."""

    messages: list[dict[str, Any]]
    params: dict[str, Any]
    schema: dict[str, Any] | None
    stream: bool
    tools: list[dict[str, Any]] | None = None
    tool_choice: Any = None

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(t["function"]["name"] for t in self.tools or [])


class _Untranslatable(Exception):
    """The request holds something a local model cannot be given faithfully."""


def _function(definition: Any) -> dict[str, Any]:
    if not isinstance(definition, dict) or not isinstance(definition.get("name"), str):
        raise _Untranslatable
    if definition.get("type") not in (None, "custom"):
        raise _Untranslatable  # a server tool (web search, code execution, …)
    schema = definition.get("input_schema")
    if not isinstance(schema, dict):
        raise _Untranslatable
    parameters = {k: v for k, v in schema.items() if k != "$schema"}
    function: dict[str, Any] = {"name": definition["name"], "parameters": parameters}
    if isinstance(definition.get("description"), str):
        function["description"] = definition["description"]
    return {"type": "function", "function": function}


def _image(block: dict[str, Any]) -> dict[str, Any]:
    source = block.get("source")
    if not isinstance(source, dict):
        raise _Untranslatable
    if source.get("type") == "base64" and isinstance(source.get("data"), str):
        url = f"data:{source.get('media_type', 'image/png')};base64,{source['data']}"
    elif source.get("type") == "url" and isinstance(source.get("url"), str):
        url = source["url"]
    else:
        raise _Untranslatable
    return {"type": "image_url", "image_url": {"url": url}}


def _text_of(content: Any) -> str:
    """A tool result's text (a result holding anything else does not translate)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise _Untranslatable
    parts = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            raise _Untranslatable
        parts.append(str(block.get("text", "")))
    return "\n\n".join(parts)


class _Builder:
    """Accumulates the chat messages and the tools added along the way."""

    def __init__(self, *, tools: bool, images: bool) -> None:
        self.allow_tools, self.allow_images = tools, images
        self.messages: list[dict[str, Any]] = []
        self.added_tools: list[dict[str, Any]] = []

    def system(self, content: Any) -> None:
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        texts = []
        for block in blocks:
            if isinstance(block, str):
                texts.append(block)
                continue
            if not isinstance(block, dict):
                raise _Untranslatable
            kind = block.get("type")
            if kind == "text":
                text = block.get("text")
                if not isinstance(text, str):
                    raise _Untranslatable
                if not text.startswith(_BILLING_PREFIX):
                    texts.append(text)
            elif kind == "tool_addition":
                if not self.allow_tools:
                    raise _Untranslatable
                tool = block.get("tool")
                definition = tool.get("definition") if isinstance(tool, dict) else None
                self.added_tools.append(_function(definition))
            else:
                raise _Untranslatable
        text = "\n\n".join(t for t in texts if t)
        if text:
            self.messages.append({"role": "system", "content": text})

    def user(self, content: Any) -> None:
        if isinstance(content, str):
            self.messages.append({"role": "user", "content": content})
            return
        if not isinstance(content, list):
            raise _Untranslatable
        parts: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                raise _Untranslatable
            kind = block.get("type")
            if kind == "text":
                parts.append({"type": "text", "text": str(block.get("text", ""))})
            elif kind == "image":
                if not self.allow_images:
                    raise _Untranslatable
                parts.append(_image(block))
            elif kind == "tool_result":
                if not self.allow_tools or not isinstance(block.get("tool_use_id"), str):
                    raise _Untranslatable
                text = _text_of(block.get("content", ""))
                if block.get("is_error"):
                    text = f"[error] {text}"
                # OpenAI wants each tool result right after the call it answers.
                self.messages.append(
                    {"role": "tool", "tool_call_id": block["tool_use_id"], "content": text}
                )
            else:
                raise _Untranslatable
        if not parts:
            return
        if all(p["type"] == "text" for p in parts):
            self.messages.append({"role": "user", "content": "\n\n".join(p["text"] for p in parts)})
        else:
            self.messages.append({"role": "user", "content": parts})

    def assistant(self, content: Any) -> None:
        if isinstance(content, str):
            self.messages.append({"role": "assistant", "content": content})
            return
        if not isinstance(content, list):
            raise _Untranslatable
        texts: list[str] = []
        calls: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                raise _Untranslatable
            kind = block.get("type")
            if kind == "text":
                texts.append(str(block.get("text", "")))
            elif kind == "tool_use":
                if not self.allow_tools:
                    raise _Untranslatable
                if not isinstance(block.get("id"), str) or not isinstance(block.get("name"), str):
                    raise _Untranslatable
                calls.append(
                    {
                        "id": block["id"],
                        "type": "function",
                        "function": {
                            "name": block["name"],
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    }
                )
            elif kind not in _SKIPPED_BLOCKS:
                raise _Untranslatable
        message: dict[str, Any] = {"role": "assistant", "content": "\n\n".join(texts) or None}
        if calls:
            message["tool_calls"] = calls
        if message["content"] is not None or calls:
            self.messages.append(message)


def _tool_choice(choice: Any) -> tuple[Any, bool | None]:
    """OpenAI's tool choice, and whether parallel calls are off."""
    if choice is None:
        return None, None
    if not isinstance(choice, dict):
        raise _Untranslatable
    serial = True if choice.get("disable_parallel_tool_use") else None
    kind = choice.get("type")
    if kind == "auto":
        return "auto", serial
    if kind == "any":
        return "required", serial
    if kind == "none":
        return "none", serial
    if kind == "tool" and isinstance(choice.get("name"), str):
        return {"type": "function", "function": {"name": choice["name"]}}, serial
    raise _Untranslatable


def translate(body: bytes, *, tools: bool = False, images: bool = False) -> Translated | None:
    """The request as a chat completion, or ``None`` when it does not translate faithfully.
    ``tools`` and ``images``: what the local model can take."""
    try:
        request = json.loads(body)
    except ValueError:
        return None
    if not isinstance(request, dict):
        return None
    try:
        return _translate(request, tools=tools, images=images)
    except _Untranslatable:
        return None


def _translate(request: dict[str, Any], *, tools: bool, images: bool) -> Translated:
    builder = _Builder(tools=tools, images=images)
    declared = request.get("tools") or []
    if not isinstance(declared, list) or (declared and not tools):
        raise _Untranslatable
    functions = [
        _function(t) for t in declared if not (isinstance(t, dict) and t.get("defer_loading"))
    ]
    if request.get("tool_choice") is not None and not tools:
        raise _Untranslatable
    choice, serial = _tool_choice(request.get("tool_choice"))
    if request.get("system") is not None:
        builder.system(request["system"])
    turns = request.get("messages")
    if not isinstance(turns, list) or not turns:
        raise _Untranslatable
    for turn in turns:
        if not isinstance(turn, dict):
            raise _Untranslatable
        role = turn.get("role")
        if role == "system":
            builder.system(turn.get("content"))
        elif role == "user":
            builder.user(turn.get("content"))
        elif role == "assistant":
            builder.assistant(turn.get("content"))
        else:
            raise _Untranslatable
    all_tools = [*functions, *builder.added_tools]
    budget = LOCAL_AGENT_MAX_TOKENS if all_tools else LOCAL_MAX_TOKENS
    asked = request.get("max_tokens")
    params: dict[str, Any] = {
        "max_tokens": min(asked, budget) if isinstance(asked, int) else budget,
        # A reasoning model would spend the answer's budget thinking.
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
    }
    for name in ("temperature", "top_p"):
        if isinstance(request.get(name), int | float):
            params[name] = request[name]
    stop = request.get("stop_sequences")
    if isinstance(stop, list) and stop and all(isinstance(item, str) for item in stop):
        params["stop"] = stop
    if serial:
        params["parallel_tool_calls"] = False
    schema: dict[str, Any] | None = None
    output = request.get("output_config")
    if isinstance(output, dict) and output.get("format") is not None:
        fmt = output["format"]
        if not (isinstance(fmt, dict) and fmt.get("type") == "json_schema"):
            raise _Untranslatable
        schema = fmt.get("schema")
        if not isinstance(schema, dict):
            raise _Untranslatable
        params["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": schema},
        }
    return Translated(
        builder.messages,
        params,
        schema,
        bool(request.get("stream")),
        tools=all_tools or None,
        tool_choice=choice if all_tools else None,
    )


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
