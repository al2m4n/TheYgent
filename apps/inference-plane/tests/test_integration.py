"""Opt-in integration: real llama-server + a tiny GGUF.

Skipped by default (``-m 'not integration'``) and skipped cleanly when the
prerequisites are absent. Run with::

    THEYGENT_GGUF_PATH=/path/to/tiny.gguf uv run pytest -m integration

Use a tiny model (SmolLM2-135M / Qwen2.5-0.5B-Instruct, tens of MB) so load is fast.
"""

from __future__ import annotations

import json
import os
import socket

import pytest
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import LlamaCppLauncher

pytestmark = pytest.mark.integration

_GGUF = os.environ.get("THEYGENT_GGUF_PATH")
_HAVE_BINARY = LlamaCppLauncher().ready

_skip = pytest.mark.skipif(
    not _GGUF or not _HAVE_BINARY,
    reason="needs THEYGENT_GGUF_PATH set and llama-server on PATH/THEYGENT_LLAMACPP_BIN",
)


def _port_is_closed(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return False
    except OSError:
        return True


@_skip
def test_real_llama_server_full_loop() -> None:
    assert _GGUF is not None
    launcher = LlamaCppLauncher()
    app = create_app(launcher=launcher, max_resident=1, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/local",
            json={
                "binding": "llamacpp",
                "source": "local-path",
                "model": _GGUF,
                "params": {"maxTokens": 16},
                "lifecycle": {"keepWarm": False, "idleTimeoutSec": 900, "priority": 1},
            },
        )

        # Capabilities probe hits the real /props endpoint (no completion run) —
        # proves the real capability path, not just a hardcoded default.
        caps = client.get("/admin/models/local/capabilities").json()
        assert caps["maxContext"] is not None and caps["maxContext"] > 0

        # Stream a real completion from a lazily-spawned llama-server, and reassemble
        # the deltas to prove actual tokens flowed (not just a [DONE]).
        saw_done = False
        content = ""
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "user", "content": "Say hello."}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            for line in resp.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == "[DONE]":
                    saw_done = True
                    continue
                delta = json.loads(payload)["choices"][0]["delta"]
                content += delta.get("content") or ""
        assert saw_done
        assert content.strip(), "expected non-empty generated text from the real model"

        state = app.state.manager.state("local")
        assert state["resident"] is True
        port = int(state["baseUrl"].rsplit(":", 1)[1])

        # :evict frees the resource — the port is actually released.
        assert client.post("/admin/models/local:evict").status_code == 200
        assert app.state.manager.state("local")["resident"] is False
        assert _port_is_closed(port)


@_skip
def test_real_llama_server_accepts_reasoning_effort() -> None:
    # The dispatch layer used to reject reasoning_effort for any model it doesn't know by name
    # (a local file path always), failing the run before the engine saw the request.
    assert _GGUF is not None
    app = create_app(launcher=LlamaCppLauncher(), max_resident=1, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/local",
            json={
                "binding": "llamacpp",
                "source": "local-path",
                "model": _GGUF,
                "params": {"maxTokens": 16, "reasoning_effort": "low"},
            },
        )
        r = client.post(
            "/v1/chat/completions",
            json={"model": "local", "messages": [{"role": "user", "content": "Say hello."}]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["choices"][0]["message"]["content"].strip()
        client.post("/admin/models/local:evict")


@_skip
def test_real_llama_server_runs_with_the_launch_settings() -> None:
    # The flags are real llama-server flags, and the context it reports is the one set: the
    # default caps the trained context, a registration's ctxSize replaces it.
    from theygent_inference_plane.launcher import DEFAULT_CHAT_CONTEXT
    from theygent_inference_plane.weights import read_gguf_context_length

    assert _GGUF is not None
    trained = read_gguf_context_length(_GGUF)
    expected_default = min(trained, DEFAULT_CHAT_CONTEXT) if trained else DEFAULT_CHAT_CONTEXT
    app = create_app(launcher=LlamaCppLauncher(), max_resident=1, enable_reaper=False)
    with TestClient(app) as client:
        for params, expected in (({}, expected_default), ({"ctxSize": 4096}, 4096)):
            client.put(
                "/admin/models/local",
                json={
                    "binding": "llamacpp",
                    "source": "local-path",
                    "model": _GGUF,
                    "params": params,
                },
            )
            caps = client.get("/admin/models/local/capabilities").json()
            assert caps["maxContext"] == expected, caps
            client.post("/admin/models/local:evict")


@_skip
def test_real_llama_server_output_is_readable_from_its_log(tmp_path) -> None:
    # The engine's own output (model load, slot setup) lands in a named log under the state dir
    # and stays readable through /admin/models/{id}/logs, during the run and after it.
    assert _GGUF is not None
    app = create_app(state_path=tmp_path / "registry.json", max_resident=1, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/local",
            json={"binding": "llamacpp", "source": "local-path", "model": _GGUF},
        )
        assert client.post("/admin/models/local:warm").status_code == 200
        client.post("/admin/models/local:evict")
        body = client.get("/admin/models/local/logs", params={"lines": 2000}).json()
        assert body["path"].startswith(str(tmp_path / "logs"))
        text = "\n".join(body["lines"])
        assert "starting:" in text and "llama" in text.lower()
        assert "n_ctx" in text  # llama-server's own load log, not just our header


@_skip
def test_real_llama_server_registered_as_reachable_is_probed(tmp_path) -> None:
    # A llama-server started by hand (not managed) and registered as openai-compatible reports
    # its real context and tool calling from /props, not a confident "unsupported".
    import subprocess
    import time

    import httpx

    assert _GGUF is not None
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = subprocess.Popen(
        [
            LlamaCppLauncher().resolved_path or "llama-server",
            "-m",
            _GGUF,
            "--port",
            str(port),
            "-c",
            "8192",
            "-np",
            "1",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        app = create_app(enable_reaper=False)
        with TestClient(app) as client:
            client.put(
                "/admin/models/byhand",
                json={
                    "binding": "openai-compatible",
                    "baseUrl": f"http://127.0.0.1:{port}/v1",
                    "model": "byhand",
                },
            )
            caps = client.get("/admin/models/byhand/capabilities").json()
            assert caps["maxContext"] == 8192, caps
            assert caps["toolCalling"] is True
            assert caps["approximate"] is True
    finally:
        server.terminate()
        server.wait(timeout=10)
