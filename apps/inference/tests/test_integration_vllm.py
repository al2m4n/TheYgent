"""Tier B — vLLM real path. UNVERIFIED until run on a CUDA host (GPU CI or cloud box).

vLLM's slot is NVIDIA/CUDA high-throughput serving. This test is **gated on actual
CUDA availability** and skips clean on the build machine (Apple Silicon / any non-CUDA
host). A skipped test is NOT a passing one — vLLM is "interface-only" until this runs
green somewhere with a real GPU. Do NOT satisfy this gate with vllm-metal: that runs
MLX underneath and would be a false green for the CUDA coupling this is meant to prove.

Run on a CUDA host::

    THEYGENT_VLLM_CUDA=1 THEYGENT_VLLM_MODEL=Qwen/Qwen2.5-0.5B-Instruct \
        uv run pytest -m integration apps/inference/tests/test_integration_vllm.py
"""

from __future__ import annotations

import os
import shutil

import pytest
from fastapi.testclient import TestClient
from theygent_inference.app import create_app
from theygent_inference.vllm_engine import VllmLauncher

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


@_skip
def test_real_vllm_full_loop() -> None:  # pragma: no cover - never runs on a non-CUDA host
    # UNVERIFIED until executed on a CUDA host. Mirrors the MLX/llama.cpp loop shape.
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
        resp = client.post(
            "/v1/chat/completions",
            json={"model": "gpu", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
        assert app.state.manager.state("gpu")["resident"] is True
        assert client.post("/admin/models/gpu:evict").status_code == 200
        assert app.state.manager.state("gpu")["resident"] is False
