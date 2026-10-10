"""An engine that failed is replaced, and its log stays readable.

After one Metal out-of-memory error, llama-server's GPU backend stays in an error state and
fails every later request with "Compute error", while the process still answers health checks.
The plane kept sending requests to it until someone evicted the model by hand, and the engine's
output went to an unnamed temp file nobody could open."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from _fake_upstream import (
    COMPUTE_ERROR,
    COMPUTE_ERROR_MID_STREAM,
    FULL_MESSAGE,
    SERVER_ERROR,
    FakeUpstreamLauncher,
)
from _payloads import managed_payload, reachable_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.clock import ManualClock
from theygent_inference_plane.launcher import EngineLog, EngineLogs
from theygent_inference_plane.manager import EngineFailedError, EngineManager
from theygent_inference_plane.registry import Registry
from theygent_ir import ManagedBinding, parse_registration


def _chat(content: str, **extra: object) -> dict[str, object]:
    return {"model": "m", "messages": [{"role": "user", "content": content}], **extra}


# ── a failed engine is replaced ──────────────────────────────────────────────


@pytest.mark.parametrize("stream", [False, True], ids=["complete", "stream"])
def test_a_compute_error_retires_the_engine(
    client: TestClient, launcher: FakeUpstreamLauncher, stream: bool
) -> None:
    client.put("/admin/models/m", json=managed_payload())
    assert client.post("/v1/chat/completions", json=_chat("hi")).status_code == 200

    r = client.post("/v1/chat/completions", json=_chat(COMPUTE_ERROR, stream=stream))

    assert r.status_code == 503, r.text
    error = r.json()["error"]
    assert error["code"] == "engine_failed"
    assert "ctxSize" in error["message"]  # the fix the caller can make
    assert launcher.handles[0].terminated
    failure = client.get("/admin/models/m").json()["state"]["lastFailure"]
    assert "GPU" in failure["reason"]

    # The next request launches a fresh engine — no manual :evict.
    r = client.post("/v1/chat/completions", json=_chat("hi"))
    assert r.status_code == 200
    assert r.json()["choices"][0]["message"]["content"] == FULL_MESSAGE
    assert launcher.launch_count == 2
    assert "lastFailure" not in client.get("/admin/models/m").json()["state"]


def test_an_ordinary_server_error_keeps_the_engine(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    client.put("/admin/models/m", json=managed_payload())
    r = client.post("/v1/chat/completions", json=_chat(SERVER_ERROR))
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "upstream_error"
    assert not launcher.handles[0].terminated
    assert client.post("/v1/chat/completions", json=_chat("hi")).status_code == 200
    assert launcher.launch_count == 1


def test_an_engine_whose_process_died_is_relaunched(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    client.put("/admin/models/m", json=managed_payload())
    client.post("/v1/chat/completions", json=_chat("hi"))
    launcher.handles[0].exited_with = -9  # killed under us

    r = client.post("/v1/chat/completions", json=_chat("hi"))

    assert r.status_code == 200, r.text
    assert launcher.launch_count == 2
    assert launcher.handles[0].terminated


async def test_a_failed_engine_with_requests_in_flight_is_not_handed_out() -> None:
    launcher = FakeUpstreamLauncher()
    registry = Registry(None)
    registry.put("m", parse_registration(managed_payload()))
    manager = EngineManager(registry, launcher, clock=ManualClock())
    try:
        async with manager.lease("m"):
            await manager.mark_failed("m", "GPU compute error")
            with pytest.raises(EngineFailedError):
                async with manager.lease("m"):
                    pass
            assert not launcher.handles[0].terminated  # the in-flight request finishes first
        assert launcher.handles[0].terminated  # …then the engine goes
        async with manager.lease("m"):
            assert launcher.launch_count == 2
    finally:
        await manager.shutdown()


# ── engine logs ──────────────────────────────────────────────────────────────


def _binding() -> ManagedBinding:
    return ManagedBinding(binding="llamacpp", source="local-path", model="/m/gpt-oss-20b.gguf")


def test_an_engine_log_is_named_and_survives_the_process(tmp_path: Path) -> None:
    logs = EngineLogs(tmp_path)
    log = logs.open(_binding())
    proc = subprocess.Popen(
        [sys.executable, "-c", "print('loading model'); print('Compute error.')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert proc.stdout is not None
    log.drain(proc.stdout)
    proc.wait()
    log.wait_drained()

    assert log.path == tmp_path / "llamacpp-gpt-oss-20b.gguf.chat.log"
    assert logs.read(_binding(), lines=5) == ["loading model", "Compute error."]


def test_an_engine_log_rotates_past_its_cap(tmp_path: Path) -> None:
    path = tmp_path / "e.log"
    log = EngineLog(path, max_bytes=100)
    for i in range(30):
        log.write(f"line {i:02d}\n".encode())
    assert path.stat().st_size <= 100 + 8
    assert (tmp_path / "e.log.1").exists()
    assert "line 29" in log.tail()


def test_logs_endpoint(tmp_path: Path) -> None:
    app = create_app(
        launcher=FakeUpstreamLauncher(), state_path=tmp_path / "registry.json", enable_reaper=False
    )
    with TestClient(app) as client:
        payload = managed_payload(model="/m/gpt-oss-20b.gguf")
        client.put("/admin/models/m", json=payload)
        assert client.get("/admin/models/m/logs").json()["error"]["code"] == "engine_log_not_found"

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        (log_dir / "llamacpp-gpt-oss-20b.gguf.chat.log").write_text("a\nb\nc\n")
        body = client.get("/admin/models/m/logs", params={"lines": 2}).json()
        assert body["lines"] == ["b", "c"]
        assert body["path"].endswith("llamacpp-gpt-oss-20b.gguf.chat.log")

        client.put("/admin/models/hosted", json=reachable_payload(base_url="http://x/v1"))
        r = client.get("/admin/models/hosted/logs")
        assert r.status_code == 404
        assert "reachable" in r.json()["error"]["message"]
        assert client.get("/admin/models/nope/logs").json()["error"]["code"] == "model_not_found"


def test_the_failure_message_points_at_the_engine_log(tmp_path: Path) -> None:
    launcher = FakeUpstreamLauncher()
    app = create_app(launcher=launcher, state_path=tmp_path / "registry.json", enable_reaper=False)
    with TestClient(app) as client:
        client.put("/admin/models/m", json=managed_payload(model="/m/gpt-oss-20b.gguf"))
        r = client.post("/v1/chat/completions", json=_chat(COMPUTE_ERROR))
        assert r.status_code == 503
        assert (
            str(tmp_path / "logs" / "llamacpp-gpt-oss-20b.gguf.chat.log")
            in r.json()["error"]["message"]
        )


def test_a_stream_that_fails_mid_way_retires_the_engine(
    client: TestClient, launcher: FakeUpstreamLauncher
) -> None:
    # A failure after the 200 is committed ends the stream with an error frame; an engine
    # failure there retires the engine just the same.
    client.put("/admin/models/m", json=managed_payload())
    with client.stream(
        "POST", "/v1/chat/completions", json=_chat(COMPUTE_ERROR_MID_STREAM, stream=True)
    ) as r:
        frames = [line for line in r.iter_lines() if line.startswith("data:")]
    error = json.loads(frames[-1][5:])["error"]
    assert "ctxSize" in error["message"], error
    assert launcher.handles[0].terminated
    assert client.post("/v1/chat/completions", json=_chat("hi")).status_code == 200
    assert launcher.launch_count == 2
