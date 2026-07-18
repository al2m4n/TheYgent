"""vLLM real path. UNVERIFIED until run on a CUDA host (GPU CI or cloud box).

vLLM's slot is NVIDIA/CUDA high-throughput serving. This test is **gated on actual
CUDA availability** and skips clean on the build machine (Apple Silicon / any non-CUDA
host). A skipped test is NOT a passing one — vLLM is "interface-only" until this runs
green somewhere with a real GPU. Do NOT satisfy this gate with vllm-metal: that runs
MLX underneath and would be a false green for the CUDA coupling this is meant to prove.

Run on a CUDA host::

    THEYGENT_VLLM_CUDA=1 THEYGENT_VLLM_MODEL=Qwen/Qwen2.5-0.5B-Instruct \
        uv run pytest -m integration apps/inference-plane/tests/test_integration_vllm.py
"""

from __future__ import annotations

import os
import shutil
import socket

import pytest
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.vllm_engine import VllmLauncher

pytestmark = pytest.mark.integration

_VLLM_MODEL = os.environ.get("THEYGENT_VLLM_MODEL")
_CUDA_PRESENT = (
    shutil.which("nvidia-smi") is not None or os.environ.get("THEYGENT_VLLM_CUDA") == "1"
)
_HAVE_VLLM = VllmLauncher().ready

_skip = pytest.mark.skipif(
    not (_CUDA_PRESENT and _HAVE_VLLM and _VLLM_MODEL),
    reason="UNVERIFIED: needs a real CUDA host with vLLM + THEYGENT_VLLM_MODEL",
)


def _port_closed(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return False
    except OSError:
        return True


@_skip
def test_real_vllm_full_loop() -> None:  # pragma: no cover - never runs on a non-CUDA host
    # UNVERIFIED until executed on a CUDA host. Complete (not stubbed) so the rented-GPU
    # session is "set env, run, watch green" — same rigor as the MLX proof: lazy-spawn,
    # stream REAL non-empty tokens, evict releases the process/port.
    import json

    app = create_app(max_resident=1, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/gpu",
            json={
                "binding": "vllm",
                "source": "hf",
                "model": _VLLM_MODEL,
                "params": {"maxTokens": 16},
                "lifecycle": {"keepWarm": False, "idleTimeoutSec": 900, "priority": 1},
            },
        )

        saw_done, content = False, ""
        with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "gpu",
                "messages": [{"role": "user", "content": "Say hello in one word."}],
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
        assert content.strip(), "expected real generated text from vLLM"

        state = app.state.manager.state("gpu")
        assert state["resident"] is True
        port = int(state["baseUrl"].rsplit(":", 1)[1])

        assert client.post("/admin/models/gpu:evict").status_code == 200
        assert app.state.manager.state("gpu")["resident"] is False
        assert _port_closed(port)
