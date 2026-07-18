"""MLX tool-call normalization — make ``mlx_lm.server`` responses conform to the OpenAI
``message.tool_calls`` shape (the inference-plane's job: absorb an engine quirk so the seam between
planes stays the OpenAI shape).

WHY THIS EXISTS. Llama 3.x emits a function call in its NATIVE text format —
``<|python_tag|>{"name": "fn", "parameters": {...}}`` — and ``mlx_lm.server`` returns that as plain
assistant ``content`` rather than a structured ``tool_calls`` array. llama.cpp's ``llama-server``
parses the same format into ``tool_calls`` internally; MLX does not. The control-plane tool loop
(``walker.execute_llm``) reads ONLY structured ``delta.tool_calls`` (engine-agnostic, by the plane
split), so an MLX-backed agent's wired tool never executes — the raw ``<|python_tag|>{...}`` text is
returned verbatim. This module is the gateway-level adapter that rewrites that text into structured
``tool_calls``, so the control plane stays untouched.

REMOVABLE BY DESIGN. This is a named adapter
gated to the MLX chat path (``Upstream.needs_tool_parse``); when a tool-aware MLX server ships
(emitting native ``tool_calls``), flip the gate off for it and delete this module. It NEVER runs for
engines that already emit structured calls (llama.cpp, hosted OpenAI) — double-parsing is the hazard
it guards against.

THE FALSE-POSITIVE GATE. A detected call's ``name`` must be one of the tool names the request
actually OFFERED (``params["tools"]``). A model answering with arbitrary JSON, or discussing the
``<|python_tag|>`` token in prose, never matches an offered tool name, so it is passed through as
content unchanged. Combined with "the whole content must parse as the call" this makes the rewrite
safe to apply broadly on the gated path.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

# Llama 3.x function-calling markers (the real shapes mlx_lm.server passes through as content).
_PYTHON_TAG = "<|python_tag|>"
_END_TOKENS = ("<|eom_id|>", "<|eot_id|>")
_TC_OPEN, _TC_CLOSE = "<tool_call>", "</tool_call>"


def offered_tool_names(params: dict[str, Any]) -> set[str]:
    """The function names a request offered (``params["tools"]`` = OpenAI tool specs). Empty when no
    tools were sent — the caller's gate (``has_tools``) then short-circuits, so the parser never
    runs for a tools-less call."""
    names: set[str] = set()
    for spec in params.get("tools") or []:
        fn = (spec or {}).get("function") if isinstance(spec, dict) else None
        name = (fn or {}).get("name") if isinstance(fn, dict) else None
        if isinstance(name, str) and name:
            names.add(name)
    return names


def has_tools(params: dict[str, Any]) -> bool:
    return bool(params.get("tools"))


def _first_json_object(text: str) -> Any | None:
    """Parse the FIRST complete top-level JSON object/array in ``text``, ignoring trailing junk
    (Llama sometimes appends an end token or stray text after the call). Returns the parsed value or
    None. Brace-balanced, string-aware (so a ``}`` inside a JSON string doesn't end it early)."""
    text = text.lstrip()
    if not text or text[0] not in "{[":
        return None
    open_ch = text[0]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[: i + 1])
                except ValueError:
                    return None
    return None


def _parse_call_objects(text: str) -> list[dict[str, Any]] | None:
    """Pull one-or-more call objects out of the (marker-stripped) payload. Handles a single object,
    a JSON array of objects, and multiple ``<|python_tag|>``-separated objects (Llama's multi-call
    shape). Returns None if nothing parses as an object."""
    objs: list[Any] = []
    for part in (p.strip() for p in text.split(_PYTHON_TAG)):
        if not part:
            continue
        parsed = _first_json_object(part)
        if parsed is None:
            return None
        if isinstance(parsed, list):
            objs.extend(parsed)
        else:
            objs.append(parsed)
    dicts = [o for o in objs if isinstance(o, dict)]
    return dicts or None


def _strip_markers(content: str) -> str | None:
    """Strip Llama tool-call markers; return the inner JSON payload, or None if ``content`` is not a
    tool-call SHAPE at all (so a plain prose answer is left alone). Requires the WHOLE content to be
    the call (after markers) — prose that merely mentions the token is not a candidate."""
    text = content.strip()
    if not text:
        return None
    for tok in _END_TOKENS:
        if text.endswith(tok):
            text = text[: -len(tok)].strip()
    if text.startswith(_PYTHON_TAG):
        return text[len(_PYTHON_TAG) :].strip()
    if text.startswith(_TC_OPEN):
        end = text.rfind(_TC_CLOSE)
        return text[len(_TC_OPEN) : end].strip() if end != -1 else text[len(_TC_OPEN) :].strip()
    if (
        text[0] in "{["
    ):  # bare JSON (no marker) — the false-positive gate (name ∈ offered) protects us
        return text
    return None


def _arguments_string(obj: dict[str, Any]) -> str:
    """The call's arguments as the OpenAI ``arguments`` JSON STRING (the control-plane
    ``_finalize_tool_calls`` does ``json.loads`` on it). Llama uses ``parameters``; OpenAI uses
    ``arguments`` — accept either. A value already a JSON string is kept; a dict is dumped."""
    args = obj.get("arguments")
    if args is None:
        args = obj.get("parameters", {})
    if isinstance(args, str):
        try:
            json.loads(args)
            return args
        except ValueError:
            return json.dumps({})
    return json.dumps(args if isinstance(args, dict) else {})


def extract_tool_calls(content: str | None, valid_names: set[str]) -> list[dict[str, Any]] | None:
    """Normalize a Llama-format text tool call in ``content`` into OpenAI ``tool_calls`` dicts, or
    None when ``content`` is not a tool call we offered. The gate: EVERY parsed object's ``name``
    must be in ``valid_names`` (a tool the request offered) — otherwise it is not a tool call
    (prose, arbitrary JSON, or a hallucinated name) and we pass the content through untouched."""
    if not content or not valid_names:
        return None
    payload = _strip_markers(content)
    if payload is None:
        return None
    objs = _parse_call_objects(payload)
    if not objs:
        return None
    calls: list[dict[str, Any]] = []
    for i, obj in enumerate(objs):
        name = obj.get("name")
        if not isinstance(name, str) or name not in valid_names:
            return None  # any non-offered name → not a tool call; fail safe to content
        calls.append(
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": name, "arguments": _arguments_string(obj)},
            }
        )
    return calls or None


def _has_structured_tool_calls(message: dict[str, Any]) -> bool:
    return bool(message.get("tool_calls"))


def normalize_completion_dict(resp: dict[str, Any], valid_names: set[str]) -> dict[str, Any]:
    """Non-streaming: rewrite ``choices[0].message`` IN PLACE if the model emitted a textual tool
    call and no structured ``tool_calls`` is present. Sets ``content`` to None, populates
    ``tool_calls``, and flips ``finish_reason`` to ``tool_calls`` — the OpenAI shape llama.cpp
    already produces. A no-match leaves ``resp`` byte-identical."""
    choices = resp.get("choices") or []
    if not choices:
        return resp
    choice = choices[0]
    message = choice.get("message") or {}
    if _has_structured_tool_calls(message):
        return resp  # already structured (defensive — should not happen on the MLX path)
    calls = extract_tool_calls(message.get("content"), valid_names)
    if not calls:
        return resp
    message["content"] = None
    message["tool_calls"] = calls
    choice["message"] = message
    choice["finish_reason"] = "tool_calls"
    return resp


def _could_be_tool_call_prefix(stripped: str) -> bool:
    """While streaming, keep buffering as long as the accumulated content could still BECOME a tool
    call (a marker arriving char-by-char, or a leading ``{``/``[``). The moment it can't, we flush
    the buffer and switch to pass-through — so a normal prose answer streams with at most a couple
    of held deltas of latency."""
    if not stripped:
        return True
    for marker in (_PYTHON_TAG, _TC_OPEN):
        if marker.startswith(stripped) or stripped.startswith(marker):
            return True
    return stripped[0] in "{["


def _synthetic_tool_call_chunks(
    template: dict[str, Any], calls: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Build the OpenAI streaming chunk(s) carrying the synthesized ``tool_calls`` — one whole-call
    fragment per call (the control-plane ``_accumulate_tool_calls`` tolerates a no-``index``/whole
    fragment), plus ``finish_reason: tool_calls``. ``template`` supplies id/created/model from a
    real upstream chunk so the envelope is well-formed for the OpenAI SDK the control plane uses."""
    base = {
        "id": template.get("id", "chatcmpl-mlx-tool"),
        "object": "chat.completion.chunk",
        "created": template.get("created", 0),
        "model": template.get("model", ""),
    }
    fragments = [
        {"index": i, "id": c["id"], "type": "function", "function": c["function"]}
        for i, c in enumerate(calls)
    ]
    return [
        {
            **base,
            "choices": [{"index": 0, "delta": {"tool_calls": fragments}, "finish_reason": None}],
        },
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]


async def rewrite_mlx_tool_stream(
    chunks: AsyncIterator[Any], valid_names: set[str], to_dict: Any
) -> AsyncIterator[dict[str, Any]]:
    """Stream rewriter for the gated MLX chat path. Buffers leading content while it could be a tool
    call; if the full buffered content normalizes to offered tool calls, emit synthetic
    ``tool_calls`` chunk(s); otherwise flush the buffered (and subsequent) chunks UNCHANGED so a
    normal answer still streams. ``to_dict`` turns an engine chunk into a plain dict.

    Yields plain chunk dicts (the caller SSE-encodes them)."""
    buffering = True
    held: list[dict[str, Any]] = []  # raw chunk dicts held while undecided
    content = ""
    template: dict[str, Any] | None = None

    async for chunk in chunks:
        d = to_dict(chunk)
        if template is None:
            template = d
        if not buffering:
            yield d
            continue
        choice = (d.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("tool_calls"):
            # the engine already produced structured calls — flush everything as-is, stop adapting.
            for h in held:
                yield h
            held = []
            buffering = False
            yield d
            continue
        piece = delta.get("content")
        if isinstance(piece, str):
            content += piece
        held.append(d)
        if not _could_be_tool_call_prefix(content.strip()):
            # definitely not a tool call → flush and pass through the rest of the stream live.
            for h in held:
                yield h
            held = []
            buffering = False

    if not buffering:
        return
    # stream ended while still undecided: decide on the full buffered content.
    calls = extract_tool_calls(content, valid_names)
    if calls and template is not None:
        for synth in _synthetic_tool_call_chunks(template, calls):
            yield synth
        # The buffered CONTENT became synthetic tool_calls, but a held terminal chunk carrying
        # token usage is accounting, not content — re-emit it so a usage-requesting caller
        # (stream_options.include_usage) still gets its usage on the tool-call turn.
        for h in held:
            if h.get("usage"):
                yield h
    else:
        for h in held:
            yield h
