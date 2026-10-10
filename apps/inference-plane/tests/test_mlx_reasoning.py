"""Reasoning a thinking model's engine separated, on the gated MLX chat path.

``mlx_lm.server`` splits a think-token model's thinking (Qwen3's ``<think>``) off itself and
sends it as ``reasoning``. The dispatch layer renames that to ``reasoning_content``, the field the
control plane reads; these tests pin the rename end to end, and that the MLX rewriters — which
hold the opening of a reply while they decide on its ``content`` — stream reasoning as it arrives
and never discard it when the reply becomes a tool call."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from _fake_upstream import (
    HARMONY_ANSWER_REPLY,
    MLX_REASONING,
    MLX_REASONING_ANSWER,
    MLX_REASONING_TOOL_TURN,
    MLX_REASONING_TURN,
    FakeUpstreamLauncher,
)
from _payloads import managed_payload
from fastapi.testclient import TestClient
from theygent_inference_plane import tool_parse

OFFERED = {"rfc3261_search"}
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "rfc3261_search",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        },
    }
]


def _chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "c",
        "created": 0,
        "model": "m",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _thinking(*pieces: str) -> list[dict[str, Any]]:
    return [_chunk({"role": "assistant", "reasoning_content": p}) for p in pieces]


class _Engine:
    """An engine stream that records how many chunks the rewriter has pulled from it."""

    def __init__(self, chunks: list[dict[str, Any]]) -> None:
        self.chunks = chunks
        self.pulled = 0

    async def __aiter__(self) -> AsyncIterator[dict[str, Any]]:
        for c in self.chunks:
            self.pulled += 1
            yield c


def _delta(c: dict[str, Any]) -> dict[str, Any]:
    return c["choices"][0]["delta"]


def _joined(out: list[dict[str, Any]], key: str) -> str:
    return "".join(_delta(c).get(key) or "" for c in out)


async def _collect(chunks: list[dict[str, Any]], offered: set[str]) -> list[dict[str, Any]]:
    stream = tool_parse.normalize_mlx_stream(_Engine(chunks).__aiter__(), offered, lambda c: c)
    return [c async for c in stream]


# ── the stream rewriters ─────────────────────────────────────────────────────


async def test_reasoning_streams_before_the_reply_format_is_known() -> None:
    for offered in (set(), OFFERED):
        engine = _Engine([*_thinking("Timer B ", "is 64*T1."), _chunk({"content": "32 s"})])
        stream = tool_parse.normalize_mlx_stream(engine.__aiter__(), offered, lambda c: c)
        first = await anext(stream)
        assert _delta(first)["reasoning_content"] == "Timer B "
        assert engine.pulled == 1, "the first thought waited for later chunks"
        second = await anext(stream)
        assert _delta(second)["reasoning_content"] == "is 64*T1."
        assert engine.pulled == 2
        assert _joined([c async for c in stream], "content") == "32 s"


async def test_a_thinking_reply_passes_through_unchanged() -> None:
    chunks = [
        *_thinking("Timer B ", "is 64*T1."),
        _chunk({"content": "Timer B is "}),
        _chunk({"content": "32 seconds."}),
        _chunk({}, "stop"),
    ]
    assert await _collect(chunks, set()) == chunks
    assert await _collect(chunks, OFFERED) == chunks


async def test_a_text_tool_call_keeps_the_reasoning_before_it() -> None:
    call = '<tool_call>{"name": "rfc3261_search", "arguments": {"query": "Timer B"}}</tool_call>'
    chunks = [
        *_thinking("Look it ", "up."),
        _chunk({"content": call[:9]}),
        _chunk({"content": call[9:]}),
        _chunk({}, "stop"),
    ]
    out = await _collect(chunks, OFFERED)
    assert _joined(out, "reasoning_content") == "Look it up."
    calls = [tc for c in out for tc in _delta(c).get("tool_calls") or []]
    assert [tc["function"]["name"] for tc in calls] == ["rfc3261_search"]
    assert out[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert not _joined(out, "content")


async def test_reasoning_inside_a_held_tool_call_is_emitted_not_held() -> None:
    engine = _Engine(
        [
            _chunk({"content": '<tool_call>{"name": "rfc3261_search", '}),
            _chunk({"reasoning_content": "args next"}),
            _chunk({"content": '"arguments": {}}</tool_call>'}),
            _chunk({}, "stop"),
        ]
    )
    stream = tool_parse.rewrite_mlx_tool_stream(engine.__aiter__(), OFFERED, lambda c: c)
    first = await anext(stream)
    assert _delta(first) == {"reasoning_content": "args next"}
    assert engine.pulled == 2
    rest = [c async for c in stream]
    assert [tc["function"]["name"] for c in rest for tc in _delta(c).get("tool_calls") or []] == [
        "rfc3261_search"
    ]


async def test_a_chunk_mixing_reasoning_and_content_is_split() -> None:
    mixed = _chunk({"role": "assistant", "reasoning_content": "done.", "content": "<|python"})
    out = await _collect([mixed, _chunk({"content": '_tag|>{"name": "rfc3261_search"}'})], OFFERED)
    assert _delta(out[0]) == {"reasoning_content": "done.", "role": "assistant"}
    assert _joined(out, "reasoning_content") == "done."
    assert not _joined(out, "content"), "the content still decided the tool call"
    assert any(_delta(c).get("tool_calls") for c in out)


async def test_separated_reasoning_passes_through_a_harmony_reply() -> None:
    pieces = [HARMONY_ANSWER_REPLY[i : i + 9] for i in range(0, len(HARMONY_ANSWER_REPLY), 9)]
    chunks = [_chunk({"content": p}) for p in pieces]
    chunks.insert(3, _chunk({"reasoning_content": "separate "}))
    out = await _collect([*chunks, _chunk({}, "stop")], set())
    assert _joined(out, "reasoning_content") == "separate Timer B is 64*T1."
    assert _joined(out, "content") == "Timer B is 32 seconds by default."


# ── through the plane (litellm in the loop) ──────────────────────────────────


def _register_mlx(client: TestClient) -> None:
    payload = managed_payload(binding="mlx", model="mlx-community/Qwen3-0.6B-4bit", source="hf")
    assert client.put("/admin/models/qwen", json=payload).status_code == 200


def _stream(client: TestClient, body: dict[str, Any]) -> list[dict[str, Any]]:
    deltas = []
    with client.stream("POST", "/v1/chat/completions", json={**body, "stream": True}) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if line.startswith("data:") and not line.endswith("[DONE]"):
                chunk = json.loads(line[5:])
                deltas += [c["delta"] for c in chunk.get("choices") or []]
    return deltas


def test_plane_returns_mlx_reasoning_as_reasoning_content(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    _register_mlx(client)
    r = client.post(
        "/v1/chat/completions",
        json={"model": "qwen", "messages": [{"role": "user", "content": MLX_REASONING_TURN}]},
    )
    assert r.status_code == 200, r.text
    message = r.json()["choices"][0]["message"]
    assert message["reasoning_content"] == MLX_REASONING
    assert message["content"] == MLX_REASONING_ANSWER


def test_plane_streams_mlx_reasoning_as_reasoning_content(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    _register_mlx(client)
    messages = [{"role": "user", "content": MLX_REASONING_TURN}]
    for extra in ({}, {"tools": _TOOLS}):
        deltas = _stream(client, {"model": "qwen", "messages": messages, **extra})
        assert "".join(d.get("reasoning_content") or "" for d in deltas) == MLX_REASONING
        assert "".join(d.get("content") or "" for d in deltas) == MLX_REASONING_ANSWER
        assert not any(d.get("reasoning") for d in deltas)


def test_plane_keeps_mlx_reasoning_on_a_text_tool_call(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    _register_mlx(client)
    body = {
        "model": "qwen",
        "messages": [{"role": "user", "content": MLX_REASONING_TOOL_TURN}],
        "tools": _TOOLS,
    }
    deltas = _stream(client, body)
    assert "".join(d.get("reasoning_content") or "" for d in deltas) == MLX_REASONING
    calls = [tc for d in deltas for tc in d.get("tool_calls") or []]
    assert [tc["function"]["name"] for tc in calls] == ["rfc3261_search"]
    assert not "".join(d.get("content") or "" for d in deltas)

    r = client.post("/v1/chat/completions", json=body)
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["reasoning_content"] == MLX_REASONING
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "rfc3261_search"
