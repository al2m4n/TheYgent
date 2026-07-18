"""MLX against a REAL mlx_lm.server on Apple Silicon.

MLX is "supported" only because this runs green. Skipped by
default (`-m 'not integration'`) and skips clean when prerequisites are absent. Run::

    THEYGENT_MLX_MODEL=mlx-community/Qwen2.5-0.5B-Instruct-4bit \
        uv run pytest -m integration apps/inference-plane/tests/test_integration_mlx.py

Prereqs: `mlx_lm.server` resolvable (PATH or THEYGENT_MLX_BIN) + the model cached.
"""

from __future__ import annotations

import asyncio
import os
import socket

import pytest
from _fake_upstream import FakeUpstreamHandle
from _payloads import reachable_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import MlxLauncher
from theygent_ir import Capabilities

pytestmark = pytest.mark.integration

_MLX_MODEL = os.environ.get("THEYGENT_MLX_MODEL")
_HAVE_MLX = MlxLauncher().ready

_skip = pytest.mark.skipif(
    not _MLX_MODEL or not _HAVE_MLX,
    reason="needs THEYGENT_MLX_MODEL set and mlx_lm.server on PATH/THEYGENT_MLX_BIN",
)


def _mlx_payload() -> dict:
    return {
        "binding": "mlx",
        "source": "hf",
        "model": _MLX_MODEL,
        "params": {"maxTokens": 16},
        "lifecycle": {"keepWarm": False, "idleTimeoutSec": 900, "priority": 1},
    }


def _stream_content(resp) -> tuple[bool, str]:
    import json

    saw_done, content = False, ""
    for line in resp.iter_lines():
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            saw_done = True
            continue
        delta = json.loads(payload)["choices"][0]["delta"]
        content += delta.get("content") or ""
    return saw_done, content


def _port_closed(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return False
    except OSError:
        return True


@_skip
def test_real_mlx_full_loop() -> None:
    # Real ManagedLauncherSet: the `mlx` binding routes to the real MlxLauncher.
    app = create_app(max_resident=2, enable_reaper=False)
    with TestClient(app) as client:
        client.put("/admin/models/local", json=_mlx_payload())

        # Capability probe returns a REAL max context (from model config, no /props,
        # no completion run) and is marked approximate (MLX exposes no caps endpoint).
        caps = client.get("/admin/models/local/capabilities").json()
        assert caps["maxContext"] is not None and caps["maxContext"] > 0
        assert caps["approximate"] is True

        # Stream real, non-empty tokens from a lazily-spawned mlx_lm.server.
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "local",
                "messages": [{"role": "user", "content": "Say hello in one word."}],
                "stream": True,
            },
        ) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            saw_done, content = _stream_content(resp)
        assert saw_done
        assert content.strip(), "expected real generated text from MLX"

        state = app.state.manager.state("local")
        assert state["resident"] is True
        port = int(state["baseUrl"].rsplit(":", 1)[1])

        # :evict releases the real process/port; next call re-spawns.
        assert client.post("/admin/models/local:evict").status_code == 200
        assert app.state.manager.state("local")["resident"] is False
        assert _port_closed(port)

        client.post(
            "/v1/chat/completions",
            json={"model": "local", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert app.state.manager.spawn_count == 2


@_skip
def test_real_cross_plane_mlx_calls_hosted() -> None:
    # Real local MLX + a (fake) hosted openai-compatible upstream in one composition.
    app = create_app(max_resident=2, enable_reaper=False)
    hosted = FakeUpstreamHandle(Capabilities())
    try:
        with TestClient(app) as client:
            client.put("/admin/models/local", json=_mlx_payload())
            os.environ["HOSTED_KEY"] = "sk-local-only"
            client.put(
                "/admin/models/hosted",
                json=reachable_payload(
                    base_url=f"{hosted.base_url}/v1", credential_ref="secret://HOSTED_KEY"
                ),
            )

            # Real tokens from the local MLX plane...
            with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "local",
                    "messages": [{"role": "user", "content": "Name a color."}],
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                _, local_text = _stream_content(resp)
            assert local_text.strip()

            # ...flow across the plane boundary as input to the hosted model.
            composed = client.post(
                "/v1/chat/completions",
                json={
                    "model": "hosted",
                    "messages": [{"role": "user", "content": local_text}],
                },
            )
            assert composed.status_code == 200
            # Credential resolved locally, reached only the user-configured upstream.
            assert hosted.last_authorization == "Bearer sk-local-only"
    finally:
        os.environ.pop("HOSTED_KEY", None)
        asyncio.run(hosted.terminate())
