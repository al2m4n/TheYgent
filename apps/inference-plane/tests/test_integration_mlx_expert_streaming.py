"""MLX expert streaming against real mlx and mlx-lm on Apple Silicon.

Skipped by default (`-m 'not integration'`); each test skips clean without its prerequisites.
The self-test needs only mlx-lm (it builds a tiny random MoE in seconds, no download). The
full-model test also needs a cached MLX mixture-of-experts checkpoint, and memory for it fully
loaded, since it compares the two::

    T=apps/inference-plane/tests/test_integration_mlx_expert_streaming.py
    uv run pytest -m integration $T -n0
    THEYGENT_MLX_STREAM_MODEL=mlx-community/Qwen3.6-35B-A3B-4bit uv run pytest -m integration $T -n0

Prereqs: Apple Silicon, `mlx_lm.server` resolvable (PATH or THEYGENT_MLX_BIN) and its interpreter
resolvable (its `#!` line, or THEYGENT_MLX_PYTHON).
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.binary import EngineBinaryNotFound, resolve_mlx_python
from theygent_inference_plane.launcher import EXPERT_STREAM_SERVER, MlxLauncher

pytestmark = pytest.mark.integration

_STREAM_MODEL = os.environ.get("THEYGENT_MLX_STREAM_MODEL")
_STREAM_CACHE_GB = float(os.environ.get("THEYGENT_MLX_STREAM_CACHE_GB") or 2)


def _mlx_python() -> str | None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return None
    launcher = MlxLauncher()
    if launcher._command is None:
        return None
    try:
        return resolve_mlx_python(launcher._command)
    except EngineBinaryNotFound:
        return None


_PYTHON = _mlx_python()
_skip = pytest.mark.skipif(
    _PYTHON is None, reason="needs Apple Silicon and mlx_lm.server with a resolvable interpreter"
)
_skip_model = pytest.mark.skipif(
    _PYTHON is None or not _STREAM_MODEL,
    reason="needs THEYGENT_MLX_STREAM_MODEL (a cached MLX MoE checkpoint) and mlx_lm.server",
)


@_skip
def test_streamed_logits_equal_the_loaded_models() -> None:
    assert _PYTHON is not None
    done = subprocess.run(
        [_PYTHON, "-I", str(EXPERT_STREAM_SERVER), "--selftest"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert "selftest OK" in done.stdout


def _payload(**params: object) -> dict:
    return {
        "binding": "mlx",
        "source": "hf",
        "model": _STREAM_MODEL,
        "params": params,
        "lifecycle": {"keepWarm": False, "idleTimeoutSec": 900, "priority": 1},
    }


def _complete(client: TestClient, model: str) -> str:
    r = client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": "Name three uses of a mixture of experts."}],
            "temperature": 0,
            "max_tokens": 32,
        },
        timeout=600,
    )
    assert r.status_code == 200, r.text
    # A thinking model may spend every token on reasoning, so the whole generation is compared.
    message = r.json()["choices"][0]["message"]
    return (message.get("reasoning_content") or "") + (message.get("content") or "")


@_skip_model
def test_a_streamed_model_answers_exactly_as_the_loaded_model() -> None:
    # One resident engine at a time: the streamed one is evicted before the full load.
    app = create_app(max_resident=1, enable_reaper=False)
    with TestClient(app) as client:
        streamed = _payload(streamExperts=True, expertCacheGb=_STREAM_CACHE_GB)
        assert client.put("/admin/models/streamed", json=streamed).status_code == 200
        assert client.put("/admin/models/loaded", json=_payload()).status_code == 200
        streamed_text = _complete(client, "streamed")
        assert streamed_text.strip(), "expected real generated text from the streamed model"
        assert _complete(client, "loaded") == streamed_text
