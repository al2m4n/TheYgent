"""Integration test — REAL llama.cpp embeddings on /v1/embeddings (the first non-chat managed
slice).

llama.cpp is the "one binary, many modalities via flags" engine: the SAME ``llama-server`` serves
``/v1/embeddings`` once spawned with ``--embeddings``. Embeddings is "supported" only
because this runs green — skipped by default (``-m 'not integration'``) and skips clean when the
prereqs are absent. Run::

    THEYGENT_LLAMACPP_EMBED_GGUF=/path/nomic-embed-text-v1.5.Q4_K_M.gguf \
        uv run --package theygent-inference-plane pytest -m integration \
        apps/inference-plane/tests/test_integration_embeddings.py

Prereqs: ``llama-server`` resolvable (PATH or THEYGENT_LLAMACPP_BIN) + a local embedding GGUF.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import LlamaCppLauncher

pytestmark = pytest.mark.integration

_EMBED_GGUF = os.environ.get("THEYGENT_LLAMACPP_EMBED_GGUF")
_HAVE_LLAMA = LlamaCppLauncher().ready

_skip = pytest.mark.skipif(
    not _EMBED_GGUF or not _HAVE_LLAMA,
    reason="needs THEYGENT_LLAMACPP_EMBED_GGUF (an embedding GGUF) + llama-server on "
    "PATH/THEYGENT_LLAMACPP_BIN",
)


@_skip
def test_real_llamacpp_embeddings() -> None:
    app = create_app(max_resident=2, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/embed",
            json={
                "binding": "llamacpp",
                "source": "local-path",
                "model": _EMBED_GGUF,
                "modality": "embeddings",
            },
        )

        # The handle declares the embeddings modality EXPLICITLY (derivation leaves it alone).
        caps = client.get("/admin/models/embed/capabilities").json()
        assert caps["modalities"] == ["embeddings"]
        assert caps["approximate"] is True

        # A real vector from a lazily-spawned `llama-server --embeddings --pooling mean`.
        r = client.post("/v1/embeddings", json={"model": "embed", "input": "the quick brown fox"})
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["object"] == "list"
        vec = data["data"][0]["embedding"]
        assert isinstance(vec, list)
        # Shape, not just presence: nomic-embed-text-v1.5 is a 768-dim model. (Override the env-var
        # default via THEYGENT_EMBED_DIM for a different model.)
        expected_dim = int(os.environ.get("THEYGENT_EMBED_DIM", "768"))
        assert len(vec) == expected_dim, f"expected {expected_dim}-dim vector, got {len(vec)}"
        assert all(isinstance(x, (int, float)) for x in vec)
        assert app.state.manager.spawn_count == 1
        assert app.state.manager.state("embed")["resident"] is True

        # Determinism: identical input → identical vector (embeddings have no sampling).
        r_again = client.post(
            "/v1/embeddings", json={"model": "embed", "input": "the quick brown fox"}
        )
        assert r_again.json()["data"][0]["embedding"] == vec

        # Batch input → one vector per item, each the same dimension (the OpenAI shape).
        r2 = client.post("/v1/embeddings", json={"model": "embed", "input": ["a", "b", "c"]})
        assert r2.status_code == 200, r2.text
        batch = r2.json()["data"]
        assert len(batch) == 3
        assert all(len(item["embedding"]) == expected_dim for item in batch)
        assert app.state.manager.spawn_count == 1  # reused the resident engine
