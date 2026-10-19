"""An engine that drops a request gets a named error, not an opaque one — or a blank answer.

mlx-lm 0.31.3's server drops a request when its Qwen3 tool parser raises on a list or object
argument the model wrote as plain text (``ramen restaurant`` for a ``searchStringsArray``). For a
non-streaming call the connection closes with no response; the dispatch layer reported
"Connection error." and retried it twice, rerunning the generation. For a stream, the server's
close-delimited body just ended, and the dispatch layer turned that into a finished, empty answer
(a made-up ``finish_reason: "stop"`` and ``[DONE]``) — as it does with any stream cut off, chunked
or not. Both are now ``engine_dropped_request``, with a message that names the engine log and,
on MLX with tools, the known cause.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _dropping_upstream import (
    DROP,
    DROP_CHUNKED,
    REPLY,
    DroppingUpstreamHandle,
    DroppingUpstreamLauncher,
)
from _payloads import managed_payload, reachable_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.clock import ManualClock
from theygent_inference_plane.gateway import EngineDroppedRequest, is_dropped_connection

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_places",
            "description": "Find places on a map.",
            "parameters": {
                "type": "object",
                "properties": {
                    "searchStringsArray": {"type": "array", "items": {"type": "string"}}
                },
            },
        },
    }
]


@pytest.fixture
def dropping() -> DroppingUpstreamLauncher:
    return DroppingUpstreamLauncher()


@pytest.fixture
def client(dropping: DroppingUpstreamLauncher, tmp_path: Path) -> Iterator[TestClient]:
    app = create_app(
        launcher=dropping,
        clock=ManualClock(),
        enable_reaper=False,
        state_path=tmp_path / "registry.json",  # engine logs get a real, named path
    )
    with TestClient(app) as test_client:
        yield test_client


def _chat(content: str, **extra: object) -> dict[str, object]:
    return {"model": "m", "messages": [{"role": "user", "content": content}], **extra}


def _frames(text: str) -> list[Any]:
    out: list[Any] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            data = line[len("data: ") :]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


def _log_path(tmp_path: Path, binding: str) -> Path:
    return tmp_path / "logs" / f"{binding}-fake-model.chat.log"


# ── non-streaming ────────────────────────────────────────────────────────────


def test_a_dropped_completion_is_named_and_not_retried(
    client: TestClient, dropping: DroppingUpstreamLauncher, tmp_path: Path
) -> None:
    client.put("/admin/models/m", json=managed_payload(binding="mlx"))

    r = client.post("/v1/chat/completions", json=_chat(DROP, tools=_TOOLS))

    assert r.status_code == 502, r.text
    error = r.json()["error"]
    assert error["code"] == "engine_dropped_request"
    assert error["type"] == "server_error"
    assert "closed the connection without sending a response" in error["message"]
    assert str(_log_path(tmp_path, "mlx")) in error["message"]
    assert "tool-call parser" in error["message"]  # the known MLX cause, since tools were offered
    # One attempt: rerunning a local generation that failed this way only fails again.
    assert len(dropping.handles[0].chat_bodies) == 1
    assert "max_retries" not in dropping.handles[0].chat_bodies[0]  # never sent to the engine
    # The engine still serves the next request (only this one was lost).
    ok = client.post("/v1/chat/completions", json=_chat("hi"))
    assert ok.status_code == 200
    assert ok.json()["choices"][0]["message"]["content"] == "".join(REPLY)


def test_the_mlx_hint_needs_tools_on_mlx(client: TestClient) -> None:
    client.put("/admin/models/m", json=managed_payload(binding="mlx"))
    no_tools = client.post("/v1/chat/completions", json=_chat(DROP)).json()["error"]
    assert no_tools["code"] == "engine_dropped_request"
    assert "tool-call parser" not in no_tools["message"]

    client.put("/admin/models/m", json=managed_payload(binding="llamacpp"))
    other = client.post("/v1/chat/completions", json=_chat(DROP, tools=_TOOLS)).json()["error"]
    assert other["code"] == "engine_dropped_request"
    assert "tool-call parser" not in other["message"]


# ── streaming ────────────────────────────────────────────────────────────────


def test_a_dropped_stream_is_named_not_an_empty_answer(client: TestClient, tmp_path: Path) -> None:
    client.put("/admin/models/m", json=managed_payload(binding="mlx"))

    r = client.post("/v1/chat/completions", json=_chat(DROP, tools=_TOOLS, stream=True))

    assert r.status_code == 200  # the engine had already committed the stream
    frames = _frames(r.text)
    # No made-up finish: the caller must not read the cut-off reply as a finished one.
    assert "[DONE]" not in frames
    assert not any(
        isinstance(f, dict) and any(c.get("finish_reason") for c in f.get("choices", []))
        for f in frames
    )
    error = frames[-1]
    assert isinstance(error, dict)
    assert error["error"]["code"] == "engine_dropped_request"
    assert "closed the connection partway through its response" in error["error"]["message"]
    assert str(_log_path(tmp_path, "mlx")) in error["error"]["message"]
    assert "tool-call parser" in error["error"]["message"]


def test_a_finished_close_delimited_stream_is_unchanged(client: TestClient) -> None:
    # The guard against false alarms: the same close-delimited stream, finished properly.
    client.put("/admin/models/m", json=managed_payload(binding="mlx"))

    r = client.post("/v1/chat/completions", json=_chat("hi", tools=_TOOLS, stream=True))

    frames = _frames(r.text)
    assert frames[-1] == "[DONE]"
    assert not any(isinstance(f, dict) and "error" in f for f in frames)
    content = "".join(
        c["delta"].get("content") or ""
        for f in frames
        if isinstance(f, dict)
        for c in f.get("choices", [])
    )
    assert content == "".join(REPLY)


def test_a_framed_stream_cut_off_is_named_too(client: TestClient) -> None:
    # A framed engine (llama-server) dying mid-reply: the dispatch layer read the incomplete
    # chunked body as a clean end too, and passed on the partial reply as finished.
    client.put("/admin/models/m", json=managed_payload(binding="llamacpp"))

    r = client.post("/v1/chat/completions", json=_chat(DROP_CHUNKED, stream=True))

    frames = _frames(r.text)
    assert "[DONE]" not in frames
    error = frames[-1]
    assert isinstance(error, dict)
    assert error["error"]["code"] == "engine_dropped_request"
    assert "partway through its response" in error["error"]["message"]
    assert "tool-call parser" not in error["error"]["message"]


# ── a reachable server ───────────────────────────────────────────────────────


def test_a_reachable_server_dropping_a_request_is_named(tmp_path: Path) -> None:
    # The dropping server stands in for a hosted API; it is never launched by the plane.
    engine = DroppingUpstreamLauncher()
    app = create_app(launcher=engine, clock=ManualClock(), enable_reaper=False)
    with TestClient(app) as client:
        handle = DroppingUpstreamHandle()
        try:
            client.put(
                "/admin/models/m",
                json=reachable_payload(base_url=f"{handle.base_url}/v1"),
            )
            r = client.post("/v1/chat/completions", json=_chat(DROP))
        finally:
            asyncio.run(handle.terminate())
    assert r.status_code == 502, r.text
    error = r.json()["error"]
    assert error["code"] == "engine_dropped_request"
    assert "the server behind 'm' closed the connection" in error["message"]


# ── recognising a drop ───────────────────────────────────────────────────────


def test_a_drop_is_recognised_through_the_wrapping_layers() -> None:
    # The chain the real dispatch layer raised for mlx_lm.server's drop, outermost first.
    inner = ConnectionResetError("Server disconnected")
    try:
        try:
            raise inner
        except ConnectionResetError as exc:
            raise RuntimeError("Connection error.") from exc
    except RuntimeError as outer:
        assert is_dropped_connection(outer)
    assert is_dropped_connection(
        OSError("peer closed connection without sending complete message body")
    )
    assert not is_dropped_connection(RuntimeError("Connection refused"))
    assert not is_dropped_connection(ValueError("bad request"))


def test_the_error_names_where_the_reply_stopped() -> None:
    early = EngineDroppedRequest(mid_stream=False, tools_offered=False)
    late = EngineDroppedRequest(mid_stream=True, tools_offered=True)
    assert early.code == late.code == "engine_dropped_request"
    assert "without sending a response" in str(early)
    assert "partway through its response" in str(late)
