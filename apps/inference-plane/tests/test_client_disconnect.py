"""A client hanging up mid-call frees the engine at once.

The server never cancels a non-streaming handler when its client disconnects, so the plane
watches for the hang-up itself and cancels the upstream call — on every non-streaming data-plane
endpoint: chat completions, embeddings, transcriptions, speech and image generation. Without that,
an abandoned request keeps the engine working and its lease's inflight count raised
(non-evictable, un-drainable) until the engine finishes. A TestClient can never drop a
connection, so these tests serve the plane on real uvicorn and hang up a raw socket mid-request,
the way a client that timed out does.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest
from _fake_upstream import (
    HOLD_UNTIL_DISCONNECT,
    FakeUpstreamHandle,
    FakeUpstreamLauncher,
    serve_on_uvicorn,
)
from _payloads import managed_payload, reachable_payload
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.types import Message
from theygent_inference_plane.app import ClientDisconnected, _unless_client_disconnects
from theygent_ir import Capabilities

# Room for a loaded CI box, yet well under the held call's 30 s cap: without the
# cancellation, the upstream would hold the request (and the inflight count) for the whole cap.
_WAIT_SEC = 10.0

# Each non-streaming data-plane endpoint, as the request kwargs for a call carrying ``text``
# where the fake upstream reads it (HOLD_UNTIL_DISCONNECT there makes the upstream hold it).
_ENDPOINTS: dict[str, Callable[[str, str], dict[str, Any]]] = {
    "/v1/chat/completions": lambda model, text: {
        "json": {"model": model, "messages": [{"role": "user", "content": text}]}
    },
    "/v1/embeddings": lambda model, text: {"json": {"model": model, "input": text}},
    # Multipart: the plane parses the whole form before it starts watching the client.
    "/v1/audio/transcriptions": lambda model, text: {
        "data": {"model": model, "language": "en"},
        "files": {"file": ("clip.wav", text.encode(), "audio/wav")},
    },
    "/v1/audio/speech": lambda model, text: {"json": {"model": model, "input": text}},
    "/v1/images/generations": lambda model, text: {"json": {"model": model, "prompt": text}},
}
_PATHS = list(_ENDPOINTS)


def _send_held_call(base_url: str, path: str, model: str) -> socket.socket:
    """Send a non-streaming call the upstream holds open, over a raw connection the test can
    hang up on."""
    url = urlsplit(base_url)
    assert url.hostname is not None and url.port is not None
    request = httpx.Request(
        "POST", f"{base_url}{path}", **_ENDPOINTS[path](model, HOLD_UNTIL_DISCONNECT)
    )
    head = f"POST {path} HTTP/1.1\r\n" + "".join(
        f"{k}: {v}\r\n" for k, v in request.headers.items()
    )
    sock = socket.create_connection((url.hostname, url.port), timeout=_WAIT_SEC)
    sock.sendall(head.encode() + b"\r\n" + request.read())
    return sock


def _eventually(check: Callable[[], bool]) -> bool:
    deadline = time.monotonic() + _WAIT_SEC
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def _state(plane: httpx.Client, model: str) -> dict[str, Any]:
    return plane.get(f"/admin/models/{model}").json()["state"]


def _held_engine(launcher: FakeUpstreamLauncher) -> FakeUpstreamHandle:
    assert _eventually(lambda: bool(launcher.handles) and launcher.handles[0].hold_started.is_set())
    return launcher.handles[0]


@pytest.mark.parametrize("path", _PATHS)
def test_managed_disconnect_cancels_upstream_and_releases_lease(
    app: FastAPI, launcher: FakeUpstreamLauncher, path: str
) -> None:
    with (
        serve_on_uvicorn(app) as base_url,
        httpx.Client(base_url=base_url, timeout=_WAIT_SEC) as plane,
    ):
        plane.put("/admin/models/triage-fast", json=managed_payload())
        sock = _send_held_call(base_url, path, "triage-fast")
        try:
            engine = _held_engine(launcher)
            assert _state(plane, "triage-fast")["inflight"] == 1
        finally:
            sock.close()  # the client hangs up mid-completion

        assert engine.hold_abandoned.wait(_WAIT_SEC)  # the engine saw its request cancelled
        assert _eventually(lambda: _state(plane, "triage-fast")["inflight"] == 0)

        # Released exactly once: an abandoned request is no reason to tear the engine down,
        # and the next request leases and releases it normally (inflight back to 0, never -1).
        assert _state(plane, "triage-fast")["resident"] is True
        r = plane.post(path, **_ENDPOINTS[path]("triage-fast", "hello"))
        assert r.status_code == 200
        assert _state(plane, "triage-fast") == {
            "resident": True,
            "inflight": 0,
            "draining": False,
            "baseUrl": engine.base_url,
        }
        assert launcher.launch_count == 1


# An engine evicted while busy drains and tears down when its last request ends. An abandoned
# request has to end when its client goes, not when the engine would have finished it — for an
# image render, minutes later.
@pytest.mark.parametrize("path", _PATHS)
def test_disconnect_lets_a_draining_engine_tear_down(
    app: FastAPI, launcher: FakeUpstreamLauncher, path: str
) -> None:
    with (
        serve_on_uvicorn(app) as base_url,
        httpx.Client(base_url=base_url, timeout=_WAIT_SEC) as plane,
    ):
        plane.put("/admin/models/triage-fast", json=managed_payload())
        sock = _send_held_call(base_url, path, "triage-fast")
        try:
            engine = _held_engine(launcher)
            assert plane.post("/admin/models/triage-fast:evict").status_code < 300
            state = _state(plane, "triage-fast")
            # Never kill a busy engine: it drains instead.
            assert (state["resident"], state["draining"], state["inflight"]) == (True, True, 1)
            assert engine.terminated is False
        finally:
            sock.close()

        assert _eventually(lambda: engine.terminated)
        # The upstream's server thread may record the hang-up a moment after the teardown: the
        # plane waits for its own side of the call to close, not for the upstream to notice.
        assert engine.hold_abandoned.wait(_WAIT_SEC)
        assert _state(plane, "triage-fast")["resident"] is False


@pytest.mark.parametrize("path", _PATHS)
def test_reachable_disconnect_cancels_upstream(app: FastAPI, path: str) -> None:
    upstream = FakeUpstreamHandle(Capabilities())
    try:
        with (
            serve_on_uvicorn(app) as base_url,
            httpx.Client(base_url=base_url, timeout=_WAIT_SEC) as plane,
        ):
            plane.put(
                "/admin/models/hosted",
                json=reachable_payload(base_url=f"{upstream.base_url}/v1"),
            )
            sock = _send_held_call(base_url, path, "hosted")
            try:
                assert upstream.hold_started.wait(_WAIT_SEC)
            finally:
                sock.close()

            assert upstream.hold_abandoned.wait(_WAIT_SEC)
            assert app.state.manager.spawn_count == 0  # reachable is never lifecycle-managed
    finally:
        asyncio.run(upstream.terminate())


# Racing the call against the client must not swallow or reshape an upstream rejection: a
# non-streaming 4xx still reaches the caller as the structured error, and the lease releases.
def test_upstream_reject_still_maps_while_client_is_watched(
    client: TestClient, app: FastAPI
) -> None:
    client.put("/admin/models/triage-fast", json=managed_payload())
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": "triage-fast",
            "messages": [{"role": "user", "content": "__force_upstream_400__"}],
        },
    )
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["code"] == "upstream_error"
    assert "temperature" in err["message"]
    assert app.state.manager.state("triage-fast")["inflight"] == 0


def _request(receive: Any) -> Request:
    return Request({"type": "http"}, receive=receive)


async def _still_connected() -> Message:
    # After the body is read, a server's receive() blocks until the client goes away.
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def _hung_up() -> Message:
    return {"type": "http.disconnect"}


# The lease is released on the caller's way out, so ClientDisconnected must not surface while
# the cancelled call is still closing its upstream connection.
async def test_disconnect_raises_only_after_the_call_unwound() -> None:
    unwound = asyncio.Event()

    async def call() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)  # closing the upstream connection takes a moment
            unwound.set()
            raise

    with pytest.raises(ClientDisconnected):
        await _unless_client_disconnects(_request(_hung_up), call())
    assert unwound.is_set()


# A handler cancelled from outside (server shutdown) never leaves its upstream call orphaned.
async def test_cancelled_handler_takes_its_upstream_call_down() -> None:
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def call() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    handler = asyncio.create_task(_unless_client_disconnects(_request(_still_connected), call()))
    await started.wait()
    handler.cancel()
    with pytest.raises(asyncio.CancelledError):
        await handler
    assert cancelled.is_set()


async def test_a_finished_call_wins_over_a_connected_client() -> None:
    async def call() -> dict[str, str]:
        return {"object": "chat.completion"}

    assert await _unless_client_disconnects(_request(_still_connected), call()) == {
        "object": "chat.completion"
    }
