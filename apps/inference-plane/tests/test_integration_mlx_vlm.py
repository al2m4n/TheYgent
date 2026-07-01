"""M20 integration — REAL MLX vision via mlx_vlm.server on /v1/chat/completions (Apple Silicon).

mlx-vlm ships its OWN OpenAI-compatible server for vision-language models — a DIFFERENT program from
``mlx_lm.server`` (chat), dispatched by the ``(mlx, vision)`` key (M20 §1). Vision rides chat (an
``image_url`` part on ``/v1/chat/completions`` — M18 §1.3), never its own endpoint. MLX vision is
"supported" only because this runs green — skipped by default; skips clean when prereqs absent::

    THEYGENT_MLX_VLM_MODEL=mlx-community/Qwen2.5-VL-3B-Instruct-4bit \
        uv run --package theygent-inference-plane pytest -m integration \
        apps/inference-plane/tests/test_integration_mlx_vlm.py

Prereqs: ``mlx_vlm.server`` resolvable (PATH or THEYGENT_MLX_VLM_BIN; ``uv tool install mlx-vlm``)
+ an mlx-community VLM available.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import MlxVlmLauncher

pytestmark = pytest.mark.integration

_VLM_MODEL = os.environ.get("THEYGENT_MLX_VLM_MODEL")
_HAVE_MLX_VLM = MlxVlmLauncher().ready

_skip = pytest.mark.skipif(
    not _VLM_MODEL or not _HAVE_MLX_VLM,
    reason="needs THEYGENT_MLX_VLM_MODEL + mlx_vlm.server on PATH/THEYGENT_MLX_VLM_BIN",
)

# A 1x1 solid-red PNG as a data URL — no network image fetch, deterministic input.
_RED_DOT = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwC"
    "AAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


@_skip
def test_real_mlx_vlm_chat_with_image() -> None:
    app = create_app(max_resident=2, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/vlm",
            json={
                "binding": "mlx",
                "source": "hf",
                "model": _VLM_MODEL,
                "modality": "vision",
                "params": {"maxTokens": 32},
            },
        )

        # vision=True → derived modalities ["chat", "vision"] (vision is a sub-capability of chat).
        caps = client.get("/admin/models/vlm/capabilities").json()
        assert caps["modalities"] == ["chat", "vision"]
        assert caps["approximate"] is True

        # A real multimodal completion from a lazily-spawned mlx_vlm.server (image_url part).
        r = client.post(
            "/v1/chat/completions",
            json={
                "model": "vlm",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Answer in one word: what is in this image?"},
                            {"type": "image_url", "image_url": {"url": _RED_DOT}},
                        ],
                    }
                ],
            },
        )
        assert r.status_code == 200, r.text
        content = r.json()["choices"][0]["message"]["content"]
        assert content.strip(), "expected real generated text from the VLM"
        assert app.state.manager.state("vlm")["resident"] is True
