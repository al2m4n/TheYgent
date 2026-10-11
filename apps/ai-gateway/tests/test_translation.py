"""Anthropic Messages ⇄ OpenAI chat completions with tool calls: what an agent turn becomes for a
local model, and what the model's streamed tool calls become for Claude Code.

What must hold:

* every recorded Claude Code request translates for a model that follows tool calls, and none
  does for a model that does not;
* tool definitions, tool use and tool results keep their names, ids, arguments and order;
  deferred tools stay out until tool search adds them; thinking is dropped;
* a streamed tool call reaches Claude Code as one whole ``tool_use`` block whose input is the
  arguments the model wrote;
* an answer that opens with a tool call it was not offered, or with arguments that are not a JSON
  object, never reaches Claude Code — the request goes to Anthropic;
* a checked answer (a client that did not stream) carries its text and tool calls as a message.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from _fake_inference import fake_inference
from _fixtures import Exchange, all_exchanges, load
from theygent_ai_gateway import local
from theygent_ai_gateway.local import LocalAnswer, LocalRoute, LocalStream
from theygent_ai_gateway.signals import estimated_tokens
from theygent_ai_gateway.translation import translate

LOCAL = "coder-medium"
SPAWN, LOOP = load("subagent-explore-1"), load("subagent-explore-2")


def messages_exchanges() -> list[Exchange]:
    return [e for e in all_exchanges() if e.path == "/v1/messages" and isinstance(e.body, dict)]


# ── the request ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("exchange", messages_exchanges(), ids=lambda e: e.name)
def test_every_recorded_request_translates_for_a_model_with_tools(exchange: Exchange) -> None:
    translated = translate(exchange.request_bytes(), tools=True)
    assert translated is not None
    if exchange.body.get("tools"):
        assert translate(exchange.request_bytes(), tools=False) is None


def test_a_subagents_tool_loop_keeps_names_ids_arguments_and_order() -> None:
    translated = translate(LOOP.request_bytes(), tools=True)
    assert translated is not None
    declared = [t["name"] for t in LOOP.body["tools"] if not t.get("defer_loading")]
    added = [
        block["tool"]["definition"]["name"]
        for m in LOOP.body["messages"]
        if m["role"] == "system" and isinstance(m["content"], list)
        for block in m["content"]
        if block["type"] == "tool_addition"
    ]
    assert added  # tool search loaded some mid-conversation
    assert [t["function"]["name"] for t in translated.tools or []] == declared + added
    for tool in translated.tools or []:
        assert "$schema" not in tool["function"]["parameters"]
    uses = [
        block
        for m in LOOP.body["messages"]
        if m["role"] == "assistant" and isinstance(m["content"], list)
        for block in m["content"]
        if block["type"] == "tool_use"
    ]
    calls = [c for m in translated.messages for c in m.get("tool_calls", [])]
    assert [(c["id"], c["function"]["name"]) for c in calls] == [(u["id"], u["name"]) for u in uses]
    assert [json.loads(c["function"]["arguments"]) for c in calls] == [u["input"] for u in uses]
    results = [m for m in translated.messages if m["role"] == "tool"]
    assert [r["tool_call_id"] for r in results] == [u["id"] for u in uses]
    # Each result follows the call it answers.
    for result in results:
        position = translated.messages.index(result)
        before = [m for m in translated.messages[:position] if m.get("tool_calls")]
        assert any(c["id"] == result["tool_call_id"] for c in before[-1]["tool_calls"])
    assert translated.params["max_tokens"] == local.LOCAL_AGENT_MAX_TOKENS
    assert translated.tool_names == set(declared + added)


def test_thinking_is_dropped_and_errors_say_so() -> None:
    body = {
        "model": "claude-opus-5-5",
        "max_tokens": 100,
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "messages": [
            {"role": "user", "content": "read it"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": "sig"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "a"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "no such file"}],
                        "is_error": True,
                    },
                    {"type": "text", "text": "try again"},
                ],
            },
        ],
    }
    translated = translate(json.dumps(body).encode(), tools=True)
    assert translated is not None
    assert translated.messages == [
        {"role": "user", "content": "read it"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "toolu_1",
                    "type": "function",
                    "function": {"name": "Read", "arguments": '{"path": "a"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "toolu_1", "content": "[error] no such file"},
        {"role": "user", "content": "try again"},
    ]


def test_tools_added_by_tool_search_join_and_deferred_ones_wait() -> None:
    exchange = load("auxiliary-with-safeguards")
    translated = translate(exchange.request_bytes(), tools=True)
    assert translated is not None
    names = translated.tool_names
    assert "mcp__fixture__tool_1" in names  # added mid-conversation
    assert "DeferredToolPlaceholder" not in names  # deferred: not in the prompt


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ({"type": "auto"}, "auto"),
        ({"type": "any"}, "required"),
        ({"type": "none"}, "none"),
        ({"type": "tool", "name": "Read"}, {"type": "function", "function": {"name": "Read"}}),
    ],
)
def test_tool_choice(choice: dict[str, Any], expected: Any) -> None:
    body = {
        "model": "m",
        "max_tokens": 10,
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "tool_choice": {**choice, "disable_parallel_tool_use": True},
        "messages": [{"role": "user", "content": "x"}],
    }
    translated = translate(json.dumps(body).encode(), tools=True)
    assert translated is not None and translated.tool_choice == expected
    assert translated.params["parallel_tool_calls"] is False


IMAGE = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBO"}}


def test_images_go_only_to_a_model_that_takes_them() -> None:
    body = json.dumps(
        {
            "model": "m",
            "max_tokens": 10,
            "messages": [
                {"role": "user", "content": [IMAGE, {"type": "text", "text": "what is it?"}]}
            ],
        }
    ).encode()
    assert translate(body) is None
    translated = translate(body, images=True)
    assert translated is not None
    assert translated.messages[0]["content"] == [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBO"}},
        {"type": "text", "text": "what is it?"},
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"tools": [{"type": "web_search_20250305", "name": "web_search"}]},
        {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t", "content": [IMAGE]}],
                }
            ]
        },
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "server_tool_use", "id": "s", "name": "web_search", "input": {}}
                    ],
                }
            ]
        },
        {"messages": [{"role": "system", "content": [{"type": "tool_removal", "name": "Read"}]}]},
    ],
)
def test_what_a_local_model_cannot_be_given_does_not_translate(change: dict[str, Any]) -> None:
    body = {
        "model": "m",
        "max_tokens": 10,
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "messages": [{"role": "user", "content": "x"}],
        **change,
    }
    assert translate(json.dumps(body).encode(), tools=True, images=True) is None


# ── the answer ───────────────────────────────────────────────────────────────


def events_of(raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for block in raw.decode().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) == 2 and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


def assembled(events: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """The content blocks Claude Code builds from a stream."""
    blocks: dict[int, dict[str, Any]] = {}
    for name, data in events:
        if name == "content_block_start":
            blocks[data["index"]] = {**data["content_block"], "_json": ""}
        elif name == "content_block_delta":
            delta, block = data["delta"], blocks[data["index"]]
            if delta["type"] == "text_delta":
                block["text"] += delta["text"]
            else:
                block["_json"] += delta["partial_json"]
    out = []
    for index in sorted(blocks):
        block = blocks[index]
        raw = block.pop("_json")
        if block["type"] == "tool_use":
            block["input"] = json.loads(raw)
        out.append(block)
    return out


async def relay(route: LocalRoute, exchange: Exchange) -> LocalStream | LocalAnswer | None:
    return await route.answer(LOCAL, exchange.request_bytes(), tools=True)


async def drain(answer: LocalStream) -> bytes:
    return b"".join([event async for event in answer.events])


async def test_a_streamed_tool_call_arrives_whole() -> None:
    with fake_inference() as fake:
        fake.pieces = ["Let me look", "."]
        fake.tool_calls = [("Bash", ['{"comm', 'and": "ls -la"', "}"])]
        route = LocalRoute(fake.base_url)
        try:
            answer = await relay(route, LOOP)
            assert isinstance(answer, LocalStream)
            events = events_of(await drain(answer))
        finally:
            await route.aclose()
    [asked] = fake.requests
    assert {t["function"]["name"] for t in asked["tools"]} >= {"Bash", "Read"}
    blocks = assembled(events)
    assert blocks[0] == {"type": "text", "text": "Let me look."}
    assert (blocks[1]["type"], blocks[1]["name"], blocks[1]["input"]) == (
        "tool_use",
        "Bash",
        {"command": "ls -la"},
    )
    assert blocks[1]["id"].startswith("toolu_local_")
    [delta] = [d for n, d in events if n == "message_delta"]
    assert delta["delta"]["stop_reason"] == "tool_use"


async def test_an_answer_that_opens_with_a_tool_call_starts_once_it_checks_out() -> None:
    with fake_inference() as fake:
        fake.pieces = []
        fake.tool_calls = [("Read", ['{"file_path": "a.py"}']), ("Grep", ['{"pattern": "x"}'])]
        route = LocalRoute(fake.base_url)
        try:
            answer = await relay(route, LOOP)
            assert isinstance(answer, LocalStream)
            blocks = assembled(events_of(await drain(answer)))
        finally:
            await route.aclose()
    assert [(b["name"], b["input"]) for b in blocks] == [
        ("Read", {"file_path": "a.py"}),
        ("Grep", {"pattern": "x"}),
    ]


@pytest.mark.parametrize(
    "call",
    [("Teleport", ['{"to": "mars"}']), ("Read", ['{"file_path": ']), ("Read", ['["a"]'])],
    ids=["unoffered-tool", "broken-json", "not-an-object"],
)
async def test_an_answer_opening_with_a_bad_tool_call_goes_to_anthropic(
    call: tuple[str, list[str]],
) -> None:
    with fake_inference() as fake:
        fake.pieces = []
        fake.tool_calls = [call]
        route = LocalRoute(fake.base_url)
        try:
            assert await relay(route, LOOP) is None
        finally:
            await route.aclose()


async def test_a_bad_tool_call_after_text_goes_out_with_empty_input() -> None:
    # Past the point of no return: Claude Code answers it with a tool error, as for any model.
    with fake_inference() as fake:
        fake.pieces = ["Checking."]
        fake.tool_calls = [("Read", ['{"file_path": '])]
        route = LocalRoute(fake.base_url)
        try:
            answer = await relay(route, LOOP)
            assert isinstance(answer, LocalStream)
            blocks = assembled(events_of(await drain(answer)))
        finally:
            await route.aclose()
    assert blocks[1]["name"] == "Read" and blocks[1]["input"] == {}


async def test_a_checked_answer_carries_text_and_tool_calls() -> None:
    body = json.loads(LOOP.request_bytes())
    body.pop("stream", None)
    with fake_inference() as fake:
        fake.pieces = ["Reading", " it."]
        fake.tool_calls = [("Read", ['{"file_path": "b.py"}'])]
        route = LocalRoute(fake.base_url)
        try:
            answer = await route.answer(LOCAL, json.dumps(body).encode(), tools=True)
            attempt = await route.attempt(LOCAL, json.dumps(body).encode(), tools=True)
        finally:
            await route.aclose()
    assert isinstance(answer, LocalAnswer)
    message = json.loads(answer.body)
    assert message["stop_reason"] == "tool_use"
    assert message["content"][0] == {"type": "text", "text": "Reading it."}
    assert (message["content"][1]["name"], message["content"][1]["input"]) == (
        "Read",
        {"file_path": "b.py"},
    )
    assert attempt.outcome == "answered" and attempt.tool_calls == ("Read",)
    assert attempt.first_output_ms is not None


async def test_a_shadow_attempt_with_a_bad_call_is_unusable() -> None:
    with fake_inference() as fake:
        fake.pieces = []
        fake.tool_calls = [("Teleport", ["{}"])]
        route = LocalRoute(fake.base_url)
        try:
            attempt = await route.attempt(LOCAL, LOOP.request_bytes(), tools=True)
        finally:
            await route.aclose()
    assert attempt.outcome == "unusable"


async def test_a_large_prompt_earns_reading_time_before_the_first_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model reads the whole prompt before it writes: the first-output deadline grows with it.
    monkeypatch.setattr(local, "FIRST_TOKEN_SECONDS", 0.3)
    reading = estimated_tokens(len(LOOP.request_bytes())) / local.MIN_PROMPT_TOKENS_PER_SECOND
    assert reading > 1.0  # this prompt earns over a second
    with fake_inference() as fake:
        fake.pieces = ["Reading."]
        fake.delay = 0.9  # past the base deadline, within the prompt's reading time
        route = LocalRoute(fake.base_url)
        try:
            answer = await relay(route, LOOP)
            assert isinstance(answer, LocalStream)
            await drain(answer)
        finally:
            await route.aclose()


async def test_a_tool_call_begun_late_still_gets_its_own_time_to_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Output began near the first-output deadline; the call's arguments then take a while. The
    # commit is timed from when output began, not from the start.
    monkeypatch.setattr(local, "FIRST_TOKEN_SECONDS", 0.3)
    monkeypatch.setattr(local, "LOCAL_TIMEOUT_SECONDS", 1.0)
    with fake_inference() as fake:
        fake.pieces = []
        fake.delay = 0.9
        fake.piece_delay = 0.4
        fake.tool_calls = [("Read", ['{"file_path": ', '"a.py"}'])]
        route = LocalRoute(fake.base_url)
        try:
            answer = await relay(route, LOOP)
            assert isinstance(answer, LocalStream)
            blocks = assembled(events_of(await drain(answer)))
        finally:
            await route.aclose()
    assert [b["name"] for b in blocks] == ["Read"]


async def test_a_long_tool_call_written_steadily_is_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Long arguments (a whole file, say) take a slow model longer in all than the time allowed
    # between pieces; it keeps writing, so it is waited for and Claude Code gets the call whole.
    monkeypatch.setattr(local, "LOCAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(local, "MIN_PROMPT_TOKENS_PER_SECOND", 10**9)  # no reading time
    with fake_inference() as fake:
        fake.pieces = []
        fake.piece_delay = 0.3
        fake.tool_calls = [("Read", ['{"file_path": "a.py", ', *['"x": 1, '] * 5, '"y": 2}'])]
        route = LocalRoute(fake.base_url)
        try:
            answer = await relay(route, LOOP)
            assert isinstance(answer, LocalStream)
            blocks = assembled(events_of(await drain(answer)))
        finally:
            await route.aclose()
    assert [b["name"] for b in blocks] == ["Read"]  # 2 s of writing, never 0.5 s without a piece


async def test_a_tool_call_that_stalls_goes_to_anthropic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local, "LOCAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(local, "MIN_PROMPT_TOKENS_PER_SECOND", 10**9)
    with fake_inference() as fake:
        fake.pieces = []
        fake.piece_delay = 1.0
        fake.tool_calls = [("Read", ['{"file_path": ', '"a.py"}'])]
        route = LocalRoute(fake.base_url)
        try:
            assert await relay(route, LOOP) is None
        finally:
            await route.aclose()


async def test_a_tool_call_held_past_the_hold_goes_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Written steadily, but for longer than an answer may be held back.
    monkeypatch.setattr(local, "LOCAL_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(local, "HOLD_SECONDS", 1.0)
    monkeypatch.setattr(local, "MIN_PROMPT_TOKENS_PER_SECOND", 10**9)
    with fake_inference() as fake:
        fake.pieces = []
        fake.piece_delay = 0.3
        fake.tool_calls = [("Read", ['{"file_path": "a.py", ', *['"x": 1, '] * 5, '"y": 2}'])]
        route = LocalRoute(fake.base_url)
        try:
            assert await relay(route, LOOP) is None
        finally:
            await route.aclose()
