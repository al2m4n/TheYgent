"""Opt-in integration: real llama-server + a tiny GGUF (plan §"opt-in integration").

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
