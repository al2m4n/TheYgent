"""MLX tool-call normalization: the engine returns Llama's text tool call as
content; `tool_parse` rewrites it into the OpenAI `tool_calls` shape the control plane expects.

Covers the OBSERVED bench output verbatim, the false-positive gate (name must be offered), the
non-stream + streaming rewrites, pass-through of normal answers, and the manager wiring of the
`needs_tool_parse` flag (mlx-chat only)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from theygent_inference_plane import tool_parse
from theygent_inference_plane.manager import EngineManager, Upstream, _Resident
from theygent_ir.registration import ManagedBinding

# A literal string observed from a real mlx run: a Llama python_tag call emitted as content.
OBSERVED = '<|python_tag|>{"name": "n_tool_4", "parameters": {"all": false, "filters": {"label": "docker"}}}'  # noqa: E501
OFFERED = {"n_tool_4"}


def _params(*names: str) -> dict[str, Any]:
    return {"tools": [{"type": "function", "function": {"name": n}} for n in names]}


# ── extract_tool_calls: the core normalizer ──────────────────────────────────


def test_extracts_the_observed_python_tag_call() -> None:
    calls = tool_parse.extract_tool_calls(OBSERVED, OFFERED)
    assert calls is not None and len(calls) == 1
    fn = calls[0]["function"]
    assert fn["name"] == "n_tool_4"
    assert calls[0]["type"] == "function"
    # arguments MUST be a JSON STRING (control-plane _finalize_tool_calls does json.loads on it).
    assert isinstance(fn["arguments"], str)
    assert json.loads(fn["arguments"]) == {"all": False, "filters": {"label": "docker"}}


def test_extracts_bare_json_when_name_is_offered() -> None:
    bare = '{"name": "n_tool_4", "parameters": {"all": true}}'
    calls = tool_parse.extract_tool_calls(bare, OFFERED)
    assert calls is not None and calls[0]["function"]["name"] == "n_tool_4"


def test_accepts_openai_arguments_key_too() -> None:
    s = '<|python_tag|>{"name": "n_tool_4", "arguments": {"x": 1}}'
    calls = tool_parse.extract_tool_calls(s, OFFERED)
    assert json.loads(calls[0]["function"]["arguments"]) == {"x": 1}


def test_tool_call_tag_variant() -> None:
    s = '<tool_call>{"name": "n_tool_4", "parameters": {}}</tool_call>'
    calls = tool_parse.extract_tool_calls(s, OFFERED)
    assert calls is not None and calls[0]["function"]["name"] == "n_tool_4"


def test_ignores_trailing_junk_after_the_json() -> None:
    # mlx/llama sometimes append an end token or stray text; the brace-balanced parse stops at the
    # complete object.
    s = '<|python_tag|>{"name": "n_tool_4", "parameters": {"a": 1}}<|eot_id|> some trailing noise'
    calls = tool_parse.extract_tool_calls(s, OFFERED)
    assert calls is not None and json.loads(calls[0]["function"]["arguments"]) == {"a": 1}


def test_multiple_python_tag_calls() -> None:
    s = '<|python_tag|>{"name": "a", "parameters": {}}<|python_tag|>{"name": "b", "parameters": {}}'
    calls = tool_parse.extract_tool_calls(s, {"a", "b"})
    assert calls is not None and [c["function"]["name"] for c in calls] == ["a", "b"]


# ── the false-positive gate: only an OFFERED name is a tool call ──────────────


def test_name_not_offered_is_not_a_tool_call() -> None:
    # a model returning JSON that happens to have a `name` we never offered → passed through.
    assert tool_parse.extract_tool_calls(OBSERVED, {"some_other_tool"}) is None


def test_prose_answer_is_not_a_tool_call() -> None:
    assert (
        tool_parse.extract_tool_calls("You have 3 containers running: web, db, cache.", OFFERED)
        is None
    )


def test_prose_mentioning_the_token_is_not_a_tool_call() -> None:
    # discussing the token in prose must NOT be mis-parsed (no leading marker / not whole-content).
    s = 'Llama emits a <|python_tag|> token before a call like {"name": ...}.'
    assert tool_parse.extract_tool_calls(s, OFFERED) is None


def test_malformed_json_after_tag_is_not_a_tool_call() -> None:
    assert (
        tool_parse.extract_tool_calls('<|python_tag|>{"name": "n_tool_4", "para', OFFERED) is None
    )


def test_no_offered_names_short_circuits() -> None:
    assert tool_parse.extract_tool_calls(OBSERVED, set()) is None


def test_empty_content() -> None:
    assert tool_parse.extract_tool_calls("", OFFERED) is None
    assert tool_parse.extract_tool_calls(None, OFFERED) is None


# ── non-stream completion rewrite ─────────────────────────────────────────────


def _completion(content: str | None, *, tool_calls: list | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}


def test_normalize_completion_rewrites_text_call() -> None:
    out = tool_parse.normalize_completion_dict(_completion(OBSERVED), OFFERED)
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "n_tool_4"


def test_normalize_completion_noop_for_plain_answer() -> None:
    resp = _completion("here are your containers")
    assert tool_parse.normalize_completion_dict(resp, OFFERED) == resp


def test_normalize_completion_leaves_structured_calls_alone() -> None:
    structured = [
        {"id": "call_x", "type": "function", "function": {"name": "n_tool_4", "arguments": "{}"}}
    ]
    resp = _completion(None, tool_calls=structured)
    out = tool_parse.normalize_completion_dict(resp, OFFERED)
    assert out["choices"][0]["message"]["tool_calls"] == structured


# ── streaming rewrite ─────────────────────────────────────────────────────────


def _chunk(
    content: str | None = None, *, finish: str | None = None, tool_calls: list | None = None
) -> dict:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {
        "id": "c1",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


async def _aiter(items: list) -> Any:
    for it in items:
        yield it


def _identity(d: dict) -> dict:
    return d


async def _collect(gen: Any) -> list[dict]:
    return [c async for c in gen]


async def test_stream_synthesizes_tool_calls_from_split_python_tag() -> None:
    # mlx_lm.server streams the tag+JSON as ordinary content deltas (split arbitrarily).
    pieces = ["<|python", "_tag|>", '{"name": "n_tool_4", ', '"parameters": {"all": false}}']
    chunks = [_chunk(p) for p in pieces] + [_chunk(finish="stop")]
    out = await _collect(tool_parse.rewrite_mlx_tool_stream(_aiter(chunks), OFFERED, _identity))
    # one fragment chunk + one finish chunk; no raw content leaked.
    tc_chunks = [c for c in out if c["choices"][0]["delta"].get("tool_calls")]
    assert len(tc_chunks) == 1
    frag = tc_chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    assert frag["function"]["name"] == "n_tool_4"
    assert json.loads(frag["function"]["arguments"]) == {"all": False}
    assert out[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert all(c["choices"][0]["delta"].get("content") is None for c in out)


async def test_stream_passes_through_a_normal_answer() -> None:
    pieces = ["Here ", "are your ", "containers."]
    chunks = [_chunk(p) for p in pieces] + [_chunk(finish="stop")]
    out = await _collect(tool_parse.rewrite_mlx_tool_stream(_aiter(chunks), OFFERED, _identity))
    streamed = "".join(c["choices"][0]["delta"].get("content") or "" for c in out)
    assert streamed == "Here are your containers."
    assert not any(c["choices"][0]["delta"].get("tool_calls") for c in out)


async def test_stream_bare_json_that_is_not_offered_passes_through() -> None:
    # leading "{" buffers, but the name isn't offered → flushed as content, not a fabricated call.
    body = '{"name": "not_offered", "parameters": {}}'
    chunks = [_chunk(body), _chunk(finish="stop")]
    out = await _collect(tool_parse.rewrite_mlx_tool_stream(_aiter(chunks), OFFERED, _identity))
    streamed = "".join(c["choices"][0]["delta"].get("content") or "" for c in out)
    assert streamed == body
    assert not any(c["choices"][0]["delta"].get("tool_calls") for c in out)


# ── manager wiring: the gate flag is set ONLY for mlx-chat ────────────────────


class _FakeHandle:
    base_url = "http://127.0.0.1:9"

    async def terminate(self) -> None:  # pragma: no cover - never called here
        ...

    async def capabilities(self) -> Any:  # pragma: no cover
        ...


def _resident(binding: str, modality: str) -> _Resident:
    mb = ManagedBinding(
        binding=binding,  # ty: ignore[invalid-argument-type]
        source="hf",
        model="m",
        modality=modality,  # ty: ignore[invalid-argument-type]
    )
    return _Resident(
        logical_id="x",
        handle=_FakeHandle(),
        binding=mb,
        last_used=0.0,
        priority=0,
        keep_warm=False,
        idle_timeout=0.0,
    )


@pytest.mark.parametrize(
    ("engine", "modality", "expected"),
    [("mlx", "chat", True), ("mlx", "vision", False), ("llamacpp", "chat", False)],
)
def test_upstream_needs_tool_parse_only_for_mlx_chat(
    engine: str, modality: str, expected: bool
) -> None:
    mgr = object.__new__(EngineManager)  # no deps needed for the pure _upstream_for mapping
    up: Upstream = EngineManager._upstream_for(mgr, _resident(engine, modality))
    assert up.needs_tool_parse is expected
