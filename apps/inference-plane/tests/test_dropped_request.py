"""An engine that drops a request gets a named error, not an opaque one — or a blank answer.

mlx-lm 0.31.3's server drops a request when its Qwen3 tool parser raises on a list or object
argument the model wrote as plain text (``ramen restaurant`` for a ``searchStringsArray``). For a
non-streaming call the connection closes with no response; the dispatch layer reported
"Connection error." and retried it twice, rerunning the generation. For a stream, the server's
close-delimited body just ended, and the dispatch layer turned that into a finished, empty answer
(a made-up ``finish_reason: "stop"`` and ``[DONE]``) — as it does with any stream cut off, chunked
or not. Both are now ``engine_dropped_request``, with a message that names the engine log and,
on MLX with tools, the known cause.

A reachable server's stream is checked the same way when its registration declares
``requireFinishReason``, and checked against streams recorded from a real hosted server so the
check raises no false alarm on what such a server sends.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from _dropping_upstream import (
    DROP,
    DROP_CHUNKED,
    REPLAY,
    REPLAY_CUT,
    REPLY,
    DroppingUpstreamHandle,
    DroppingUpstreamLauncher,
)
from _hosted_streams import HOSTED_REQUESTS, HOSTED_STREAMS
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
# The dropping server stands in for a hosted API or a server started by hand; the plane never
# launches it. Its streams are checked only when the registration declares
# ``requireFinishReason``: a reachable server's stream shape is whatever sits behind the URL.


@contextlib.contextmanager
def _reachable(tmp_path: Path, **registration: object) -> Iterator[TestClient]:
    handle = DroppingUpstreamHandle()
    app = create_app(
        launcher=DroppingUpstreamLauncher(),
        clock=ManualClock(),
        enable_reaper=False,
        state_path=tmp_path / "registry.json",
    )
    try:
        with TestClient(app) as test_client:
            payload = reachable_payload(base_url=f"{handle.base_url}/v1") | registration
            assert test_client.put("/admin/models/m", json=payload).status_code == 200
            yield test_client
    finally:
        asyncio.run(handle.terminate())


@pytest.fixture
def checked(tmp_path: Path) -> Iterator[TestClient]:
    """A reachable server registered with ``requireFinishReason``."""
    with _reachable(tmp_path, requireFinishReason=True) as test_client:
        yield test_client


@pytest.fixture
def unchecked(tmp_path: Path) -> Iterator[TestClient]:
    """A reachable server registered without it, the default."""
    with _reachable(tmp_path) as test_client:
        yield test_client


def _assert_cut_off(frames: list[Any]) -> None:
    """The stream ended with the named error: no finish of any kind, made up or relayed."""
    assert "[DONE]" not in frames
    assert _finish_reasons(frames) == []
    error = frames[-1]
    assert isinstance(error, dict)
    assert error["error"]["code"] == "engine_dropped_request"
    assert error["error"]["type"] == "server_error"
    message = error["error"]["message"]
    assert "the server behind 'm' closed the connection partway through its response" in message
    assert "engine log" not in message  # a reachable server's log isn't the plane's


def _finish_reasons(frames: list[Any]) -> list[str]:
    return [
        c["finish_reason"]
        for f in frames
        if isinstance(f, dict)
        for c in f.get("choices", [])
        if c.get("finish_reason")
    ]


def _content(frames: list[Any]) -> str:
    return "".join(
        c.get("delta", {}).get("content") or ""
        for f in frames
        if isinstance(f, dict)
        for c in f.get("choices", [])
    )


def _replay(prompt: str, name: str) -> dict[str, Any]:
    """The recorded request's params (tools, max_tokens, stream_options), with ``prompt``."""
    recorded = {k: v for k, v in HOSTED_REQUESTS[name].items() if k not in ("model", "messages")}
    return _chat(prompt + name, **recorded)


def test_a_reachable_server_dropping_a_request_is_named(unchecked: TestClient) -> None:
    # A non-streaming drop is a transport error, named for every upstream without the opt-in.
    r = unchecked.post("/v1/chat/completions", json=_chat(DROP))

    assert r.status_code == 502, r.text
    error = r.json()["error"]
    assert error["code"] == "engine_dropped_request"
    assert "the server behind 'm' closed the connection" in error["message"]


def test_the_opt_in_is_stored_on_the_registration(checked: TestClient) -> None:
    binding = checked.get("/admin/models/m").json()["binding"]
    assert binding["requireFinishReason"] is True


def test_a_reachable_stream_cut_off_is_named(checked: TestClient) -> None:
    # A close-delimited body (mlx_lm.server started by hand) that ends before its finish.
    r = checked.post("/v1/chat/completions", json=_chat(DROP, stream=True))

    assert r.status_code == 200
    _assert_cut_off(_frames(r.text))


def test_a_framed_reachable_stream_cut_off_keeps_what_arrived(checked: TestClient) -> None:
    # A chunked body cut before its last chunk, the framing hosted APIs use. What arrived is
    # relayed; the end is the named error, not a finished, shorter answer.
    r = checked.post(
        "/v1/chat/completions",
        json=_chat(DROP_CHUNKED, stream=True, stream_options={"include_usage": True}),
    )

    frames = _frames(r.text)
    _assert_cut_off(frames)
    assert _content(frames) == REPLY[0]


@pytest.mark.parametrize("name", sorted(HOSTED_STREAMS))
def test_a_hosted_stream_passes_the_check_unchanged(checked: TestClient, name: str) -> None:
    # The guard against false alarms, on streams a real hosted server sent: each one, finished,
    # reaches the caller with the server's own finish_reason and [DONE].
    recorded = [f for f in _frames(b"".join(HOSTED_STREAMS[name]).decode()) if f != "[DONE]"]

    r = checked.post("/v1/chat/completions", json=_replay(REPLAY, name))

    frames = _frames(r.text)
    assert frames[-1] == "[DONE]"
    assert not any(isinstance(f, dict) and "error" in f for f in frames)
    assert _finish_reasons(frames)[-1] == _finish_reasons(recorded)[-1]
    assert _content(frames) == _content(recorded)


@pytest.mark.parametrize("name", sorted(HOSTED_STREAMS))
def test_a_hosted_stream_cut_before_its_finish_is_named(checked: TestClient, name: str) -> None:
    r = checked.post("/v1/chat/completions", json=_replay(REPLAY_CUT, name))

    _assert_cut_off(_frames(r.text))


def test_without_the_opt_in_a_reachable_stream_is_not_checked(unchecked: TestClient) -> None:
    # The default stays off: a reachable server that never sends a finish_reason must not have
    # every stream fail. A cut-off stream then still reads as finished; the dispatch layer's
    # made-up "stop" is all the caller gets.
    r = unchecked.post("/v1/chat/completions", json=_chat(DROP, stream=True))

    frames = _frames(r.text)
    assert frames[-1] == "[DONE]"
    assert _finish_reasons(frames) == ["stop"]


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
