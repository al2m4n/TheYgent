"""The real cross-process proof (M3 §6) — control-plane -> real inference -> real MLX.

The fast suite fakes the model; this is the thing it can't give: a real prompt going
across the §8 process boundary to a real ``mlx_lm.server`` and streaming back. We launch
the **real inference plane as a subprocess** (not an in-process import — that keeps the
control-plane free of any inference code dependency and makes the boundary genuine),
register a tiny MLX model over its ``/admin`` surface, then drive ``/runs``.

Skipped by default (``-m 'not integration'``) and skips clean when prerequisites are
absent. Run (Apple Silicon)::

    THEYGENT_MLX_MODEL=mlx-community/Qwen2.5-0.5B-Instruct-4bit \
        uv run --package theygent-control-plane pytest -m integration \
        apps/control-plane/tests/test_integration_mlx.py

Prereqs: ``mlx_lm.server`` resolvable (PATH or ``THEYGENT_MLX_BIN``) + the model cached.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app

pytestmark = pytest.mark.integration

_MLX_MODEL = os.environ.get("THEYGENT_MLX_MODEL")
_HAVE_MLX = bool(shutil.which("mlx_lm.server") or os.environ.get("THEYGENT_MLX_BIN"))

_skip = pytest.mark.skipif(
    not _MLX_MODEL or not _HAVE_MLX,
    reason="needs THEYGENT_MLX_MODEL set and mlx_lm.server on PATH/THEYGENT_MLX_BIN",
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _mlx_payload() -> dict:
    return {
        "binding": "mlx",
        "source": "hf",
        "model": _MLX_MODEL,
        "params": {"maxTokens": 16},
        "lifecycle": {"keepWarm": False, "idleTimeoutSec": 900, "priority": 1},
    }


def _wait_ready(base: str, deadline_s: float = 60.0) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/readyz", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError("inference plane did not become ready")


@_skip
def test_runs_against_real_mlx_subprocess() -> None:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "THEYGENT_INFERENCE_PORT": str(port),
        "THEYGENT_INFERENCE_HOST": "127.0.0.1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "theygent_inference"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_ready(base)
        # Register a logical MLX model over the inference management plane.
        reg = httpx.put(f"{base}/admin/models/local", json=_mlx_payload(), timeout=10.0)
        assert reg.status_code == 200, reg.text

        # Control-plane points at the real inference data plane and owns the run.
        app = create_app(inference_base_url=f"{base}/v1")
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/runs",
                json={
                    "input": "Say hello in one word.",
                    "model": "local",
                    "stream": True,
                },
            ) as resp:
                assert resp.status_code == 200
                assert resp.headers["content-type"].startswith("text/event-stream")
                content, run_id, terminal = "", None, None
                for line in "".join(resp.iter_text()).splitlines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[len("data:") :].strip()
                    if payload == "[DONE]":
                        continue
                    obj = json.loads(payload)
                    if "delta" in obj:
                        content += obj["delta"]
                    elif obj.get("status") == "streaming":
                        run_id = obj["runId"]
                    elif obj.get("status") in ("completed", "failed"):
                        terminal = obj

            assert content.strip(), "expected real generated text from MLX"
            assert terminal is not None and terminal["status"] == "completed"
            assert client.get(f"/runs/{run_id}").json()["status"] == "completed"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
