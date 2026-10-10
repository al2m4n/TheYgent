"""gpt-oss's harmony replies on the MLX path become reasoning, answer text and tool calls.

``mlx_lm.server`` has no harmony parser and returns the reply verbatim as content, so an agent's
tools never ran and the hidden reasoning landed in the answer. The fixtures are the reply shape
recorded from ``mlx-community/gpt-oss-20b-MXFP4-Q8`` on ``mlx_lm.server`` 0.31.3."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from _fake_upstream import (
    HARMONY_ANSWER_REPLY,
    HARMONY_ANSWER_TURN,
    HARMONY_TOOL_REPLY,
    HARMONY_TOOL_TURN,
    FakeUpstreamLauncher,
)
from _payloads import managed_payload
from fastapi.testclient import TestClient
from theygent_inference_plane import harmony, tool_parse

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


# ── the parser ───────────────────────────────────────────────────────────────


def test_parses_reasoning_and_a_tool_call() -> None:
    reasoning, content, calls = harmony.parse(HARMONY_TOOL_REPLY, OFFERED)
    assert reasoning == "We need to search RFC 3261 for Timer B. Use function."
    assert content == ""
    assert [(c.name, json.loads(c.arguments)) for c in calls] == [
        ("rfc3261_search", {"query": "Timer B INVITE client transaction RFC 3261"})
    ]


def test_parses_reasoning_and_a_final_answer() -> None:
    reasoning, content, calls = harmony.parse(HARMONY_ANSWER_REPLY, OFFERED)
    assert reasoning == "Timer B is 64*T1."
    assert content == "Timer B is 32 seconds by default."
    assert calls == []


def test_recipient_before_channel_and_explicit_stop_tokens() -> None:
    reply = (
        "<|channel|>analysis<|message|>Look it up.<|end|>"
        "<|start|>assistant to=functions.rfc3261_search<|channel|>commentary json"
        '<|message|>{"query":"x"}<|call|>'
    )
    _, content, calls = harmony.parse(reply, OFFERED)
    assert content == ""
    assert [c.name for c in calls] == ["rfc3261_search"]


def test_a_function_that_was_not_offered_is_not_a_tool_call() -> None:
    _, content, calls = harmony.parse(HARMONY_TOOL_REPLY, {"some_other_tool"})
    assert calls == []
    assert "Timer B INVITE" in content  # nothing hidden: the body is shown as text


def test_plain_text_is_not_harmony() -> None:
    assert not harmony.is_harmony("Timer B is 32 seconds.")
    assert not harmony.could_open_harmony("Timer B")
    assert harmony.could_open_harmony("<|chan")


@pytest.mark.parametrize("size", [1, 2, 5, 13])
def test_streaming_parse_matches_whole_parse_at_any_chunk_size(size: int) -> None:
    for reply in (HARMONY_TOOL_REPLY, HARMONY_ANSWER_REPLY):
        stream = harmony.HarmonyStream(offered=OFFERED)
        pieces: list[tuple[str, str]] = []
        for i in range(0, len(reply), size):
            pieces += stream.feed(reply[i : i + size])
        pieces += stream.finish()
        reasoning, content, calls = harmony.parse(reply, OFFERED)
        assert "".join(t for k, t in pieces if k == "reasoning") == reasoning
        assert "".join(t for k, t in pieces if k == "content") == content
        assert [(c.name, c.arguments) for c in stream.calls] == [
            (c.name, c.arguments) for c in calls
        ]
        assert not any("<|" in t for _, t in pieces)  # no special token ever leaks


# ── the MLX normalizers ──────────────────────────────────────────────────────


def _completion(content: str) -> dict[str, Any]:
    return {
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ]
    }


def test_completion_tool_turn() -> None:
    out = tool_parse.normalize_mlx_completion(_completion(HARMONY_TOOL_REPLY), OFFERED)
    choice = out["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] is None
    assert choice["message"]["reasoning_content"].startswith("We need to search")
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "rfc3261_search"


def test_completion_answer_turn_without_tools() -> None:
    out = tool_parse.normalize_mlx_completion(_completion(HARMONY_ANSWER_REPLY), set())
    message = out["choices"][0]["message"]
    assert message["content"] == "Timer B is 32 seconds by default."
    assert message["reasoning_content"] == "Timer B is 64*T1."
    assert out["choices"][0]["finish_reason"] == "stop"


def test_completion_plain_answer_is_unchanged() -> None:
    resp = _completion("Timer B is 32 seconds.")
    assert tool_parse.normalize_mlx_completion(json.loads(json.dumps(resp)), OFFERED) == resp
    assert tool_parse.normalize_mlx_completion(json.loads(json.dumps(resp)), set()) == resp


async def _aiter(items: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    for item in items:
        yield item


def _chunks(text: str, size: int) -> list[dict[str, Any]]:
    pieces = [
        {
            "id": "c",
            "created": 0,
            "model": "m",
            "choices": [
                {"index": 0, "delta": {"content": text[i : i + size]}, "finish_reason": None}
            ],
        }
        for i in range(0, len(text), size)
    ]
    pieces.append(
        {
            "id": "c",
            "created": 0,
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    return pieces


async def _collect(chunks: list[dict[str, Any]], offered: set[str]) -> list[dict[str, Any]]:
    return [c async for c in tool_parse.normalize_mlx_stream(_aiter(chunks), offered, lambda c: c)]


async def test_stream_plain_answer_passes_through_unchanged() -> None:
    chunks = _chunks("Timer B is 32 seconds.", 4)
    assert await _collect(chunks, set()) == chunks
    assert await _collect(chunks, OFFERED) == chunks


async def test_stream_answer_turn_splits_reasoning_from_content() -> None:
    out = await _collect(_chunks(HARMONY_ANSWER_REPLY, 3), set())
    deltas = [c["choices"][0]["delta"] for c in out]
    assert "".join(d.get("reasoning_content", "") for d in deltas) == "Timer B is 64*T1."
    assert "".join(d.get("content", "") for d in deltas) == "Timer B is 32 seconds by default."
    assert out[-1]["choices"][0]["finish_reason"] == "stop"


async def test_stream_tool_turn_emits_structured_calls() -> None:
    out = await _collect(_chunks(HARMONY_TOOL_REPLY, 3), OFFERED)
    calls = [tc for c in out for tc in c["choices"][0]["delta"].get("tool_calls", [])]
    assert [tc["function"]["name"] for tc in calls] == ["rfc3261_search"]
    assert out[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert not any(c["choices"][0]["delta"].get("content") for c in out)


# ── through the plane (litellm in the loop) ──────────────────────────────────


def _register_mlx(client: TestClient) -> None:
    payload = managed_payload(
        binding="mlx", model="mlx-community/gpt-oss-20b-MXFP4-Q8", source="hf"
    )
    assert client.put("/admin/models/oss", json=payload).status_code == 200


def test_plane_returns_a_structured_tool_call(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    _register_mlx(client)
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "oss",
            "messages": [{"role": "user", "content": HARMONY_TOOL_TURN}],
            "tools": _TOOLS,
        },
    )
    assert r.status_code == 200, r.text
    choice = r.json()["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["tool_calls"][0]["function"]["name"] == "rfc3261_search"
    assert "<|" not in (choice["message"].get("content") or "")


def test_plane_streams_reasoning_apart_from_the_answer(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    _register_mlx(client)
    reasoning = content = ""
    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "oss",
            "messages": [{"role": "user", "content": HARMONY_ANSWER_TURN}],
            "stream": True,
        },
    ) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if not line.startswith("data:") or line.endswith("[DONE]"):
                continue
            delta = json.loads(line[5:])["choices"][0]["delta"]
            reasoning += delta.get("reasoning_content") or ""
            content += delta.get("content") or ""
    assert reasoning == "Timer B is 64*T1."
    assert content == "Timer B is 32 seconds by default."
