"""The bundled image wrapper stops a render nobody is waiting for.

``image_server`` shells out to a one-shot diffusion CLI per request, serialized behind a lock, so
an abandoned render holds the accelerator — and every caller queued behind it — for minutes. The
plane closes its connection to the wrapper the moment its own client hangs up (see
test_client_disconnect.py); these tests pin what the wrapper does then, and on the SIGTERM an
engine teardown sends, and the whole chain from a caller hanging up on the plane. They run the
real wrapper against a fake CLI that either never finishes or writes its PNG at once, so no
generator binary or weights are needed.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from _fake_upstream import serve_on_uvicorn
from theygent_inference_plane import image_server
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import ImageServerLauncher

_WAIT_SEC = 10.0
# The wrapper is spawned with the plane's own interpreter and package, whose import alone takes
# seconds; a cold spawn on a loaded CI box gets more room than a request does.
_SPAWN_WAIT_SEC = 60.0

# Called the way the wrapper calls sd-cli (`-m MODEL -p PROMPT -o OUT …`). A prompt starting with
# "slow" is a render that never finishes on its own (`exec` keeps the logged pid the render's
# own); any other prompt writes its PNG at once. Every render that starts logs "<pid> <prompt>".
_FAKE_CLI = """#!/bin/sh
while [ $# -gt 0 ]; do
  case "$1" in
    -p) prompt=$2; shift 2 ;;
    -o) out=$2; shift 2 ;;
    *) shift ;;
  esac
done
echo "$$ $prompt" >> "__LOG__"
case "$prompt" in
  slow*) exec sleep 60 ;;
esac
printf 'PNG' > "$out"
"""


def _eventually(check: Callable[[], bool], wait: float = _WAIT_SEC) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(0.02)
    return check()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _renders(log: Path) -> list[tuple[int, str]]:
    """Every render the fake CLI started, in order: (pid, prompt)."""
    if not log.exists():
        return []
    rows = (line.split(" ", 1) for line in log.read_text().splitlines())
    return [(int(pid), prompt) for pid, prompt in rows]


def _kill_renders(log: Path) -> None:
    """Never leave a fake render running behind a failed test."""
    for pid, _ in _renders(log):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


@dataclass
class _Wrapper:
    url: str
    proc: subprocess.Popen[bytes]
    log: Path

    def renders(self) -> list[tuple[int, str]]:
        return _renders(self.log)

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.url}/health", timeout=1.0).status_code == 200
        except httpx.HTTPError:
            return False


def _fake_cli(tmp_path: Path) -> tuple[Path, Path]:
    log = tmp_path / "renders.log"
    cli = tmp_path / "sd-cli"
    cli.write_text(_FAKE_CLI.replace("__LOG__", str(log)))
    cli.chmod(0o755)
    return cli, log


@pytest.fixture
def wrapper(tmp_path: Path) -> Iterator[_Wrapper]:
    cli, log = _fake_cli(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "theygent_inference_plane.image_server",
            "--engine",
            "sdcpp",
            "--model",
            "model.gguf",
            "--port",
            str(port),
            "--bin",
            str(cli),
        ]
    )
    served = _Wrapper(f"http://127.0.0.1:{port}", proc, log)
    try:
        assert _eventually(served.healthy, _SPAWN_WAIT_SEC)
        yield served
    finally:
        proc.kill()
        proc.wait()
        _kill_renders(log)


def _send(url: str, prompt: str, model: str = "model.gguf") -> socket.socket:
    """POST a generation over a raw connection the test can hang up on."""
    port = int(url.rsplit(":", 1)[1])
    body = json.dumps({"model": model, "prompt": prompt}).encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=_WAIT_SEC)
    sock.sendall(
        b"POST /v1/images/generations HTTP/1.1\r\nHost: wrapper\r\n"
        + b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )
    return sock


def test_a_hung_up_caller_stops_its_render_and_frees_the_queue(wrapper: _Wrapper) -> None:
    running = _send(wrapper.url, "slow-running")
    assert _eventually(lambda: len(wrapper.renders()) == 1)
    [(pid, _)] = wrapper.renders()
    queued = _send(wrapper.url, "slow-queued")
    time.sleep(0.5)  # it has reached the lock and waits behind the running render

    queued.close()
    running.close()

    # The running render is killed rather than finished for nobody, and the queued one gives its
    # turn up without starting: the next caller is served at once.
    assert _eventually(lambda: not _alive(pid))
    r = httpx.post(
        f"{wrapper.url}/v1/images/generations",
        json={"model": "model.gguf", "prompt": "a cat"},
        timeout=_WAIT_SEC,
    )
    assert r.status_code == 200
    assert base64.b64decode(r.json()["data"][0]["b64_json"]) == b"PNG"
    assert [prompt for _, prompt in wrapper.renders()] == ["slow-running", "a cat"]
    assert wrapper.proc.poll() is None


# An engine teardown SIGTERMs the wrapper. A process that exits does not take its children with
# it, so without the wrapper killing the render first, the CLI would keep rendering — holding the
# model in memory — after the plane has freed the slot for another engine.
def test_terminating_the_wrapper_takes_its_render_down(wrapper: _Wrapper) -> None:
    caller = _send(wrapper.url, "slow")
    try:
        assert _eventually(lambda: bool(wrapper.renders()))
        [(pid, _)] = wrapper.renders()
        wrapper.proc.terminate()
        wrapper.proc.wait(timeout=_WAIT_SEC)
        assert _eventually(lambda: not _alive(pid))
    finally:
        caller.close()


def _handler(connection: socket.socket) -> image_server._Handler:
    handler = object.__new__(image_server._Handler)
    handler.connection = connection
    return handler


# Watching the caller replaces a blocking wait on the CLI, so the wall-clock cap is enforced by
# the same loop — a stuck render still fails as a clean error and its CLI is killed.
def test_a_stuck_render_still_times_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(image_server, "_GENERATION_TIMEOUT_SEC", 0.5)
    cli, log = _fake_cli(tmp_path)
    ours, theirs = socket.socketpair()
    with ours, theirs:
        with pytest.raises(image_server._GenerationError, match="timed out"):
            _handler(ours)._render([str(cli), "-p", "slow", "-o", str(tmp_path / "out.png")])
    pid = int(log.read_text().split(" ", 1)[0])
    assert not _alive(pid)


def test_a_connected_caller_is_not_mistaken_for_a_hang_up() -> None:
    ours, theirs = socket.socketpair()
    with ours, theirs:
        assert _handler(ours)._caller_gone() is False
        theirs.close()
        assert _handler(ours)._caller_gone() is True


# The whole chain, across processes: a caller hangs up on a render that a draining engine (evicted
# while busy) is serving. The plane cancels its call, which closes its connection to the wrapper;
# the lease releases and the engine tears down; and the render dies with it rather than running on
# as an orphan after the slot was handed back.
def test_a_hang_up_on_the_plane_stops_the_render_and_leaves_no_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli, log = _fake_cli(tmp_path)
    weights = tmp_path / "model.gguf"
    weights.write_bytes(b"weights")
    monkeypatch.setenv("THEYGENT_SDCPP_BIN", str(cli))
    app = create_app(launcher=ImageServerLauncher("sdcpp"), enable_reaper=False)
    try:
        with (
            serve_on_uvicorn(app) as base_url,
            httpx.Client(base_url=base_url, timeout=_WAIT_SEC) as plane,
        ):
            registered = plane.put(
                "/admin/models/painter",
                json={
                    "binding": "llamacpp",
                    "source": "local-path",
                    "model": str(weights),
                    "modality": "images.generation",
                },
            )
            assert registered.status_code < 300
            caller = _send(base_url, "slow", model="painter")
            try:
                # The plane spawns the wrapper on this first call, which then starts the render.
                assert _eventually(lambda: bool(_renders(log)), _SPAWN_WAIT_SEC)
                [(pid, _)] = _renders(log)
                assert plane.post("/admin/models/painter:evict").status_code < 300
                state = plane.get("/admin/models/painter").json()["state"]
                assert (state["resident"], state["draining"], state["inflight"]) == (True, True, 1)
            finally:
                caller.close()

            def torn_down() -> bool:
                return plane.get("/admin/models/painter").json()["state"]["resident"] is False

            assert _eventually(torn_down)
            assert _eventually(lambda: not _alive(pid))
    finally:
        _kill_renders(log)
