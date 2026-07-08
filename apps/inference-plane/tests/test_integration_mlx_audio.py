"""Integration test — REAL mlx-audio text-to-speech on /v1/audio/speech (Apple Silicon).

The (mlx, audio.speech) slot is "supported" only because this runs green: the real
``mlx_audio.server`` is spawned, loads the bound speech model (downloading it on first use), and
synthesizes real audio through the OpenAI route. Skipped by default (``-m 'not integration'``)
and skips clean when the prereqs are absent. Run::

    THEYGENT_TTS_MODEL=mlx-community/Kokoro-82M-bf16 \
        uv run --package theygent-inference-plane pytest -m integration \
        apps/inference-plane/tests/test_integration_mlx_audio.py

Prereqs: ``mlx_audio.server`` resolvable (PATH / THEYGENT_MLX_AUDIO_BIN / importable module) with
its server extras installed, + a speech model reference (first run downloads it).
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import MlxAudioLauncher

pytestmark = pytest.mark.integration

_TTS_MODEL = os.environ.get("THEYGENT_TTS_MODEL")
_HAVE_SERVER = MlxAudioLauncher().ready

_skip = pytest.mark.skipif(
    not _TTS_MODEL or not _HAVE_SERVER,
    reason="needs THEYGENT_TTS_MODEL (a speech model ref, e.g. an mlx-community Kokoro repo) + "
    "mlx_audio.server resolvable (PATH/THEYGENT_MLX_AUDIO_BIN)",
)


@_skip
def test_real_mlx_audio_speech() -> None:
    app = create_app(max_resident=2, enable_reaper=False)
    with TestClient(app) as client:
        # Voice vocabularies are engine-specific — the binding registers the default voice, so
        # plain chat surfaces can speak without knowing the engine's voice names.
        client.put(
            "/admin/models/tts-local",
            json={
                "binding": "mlx",
                "source": "hf",
                "model": _TTS_MODEL,
                "modality": "audio.speech",
                "params": {"voice": os.environ.get("THEYGENT_TTS_VOICE", "af_heart")},
            },
        )

        caps = client.get("/admin/models/tts-local/capabilities").json()
        assert caps["modalities"] == ["audio.speech"]

        r = client.post(
            "/v1/audio/speech",
            json={"model": "tts-local", "input": "Hello from the local speech engine."},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("audio/")
        # Real synthesized audio, not an empty body or an error envelope.
        assert len(r.content) > 10_000


@_skip
def test_real_mlx_audio_speech_voiceless() -> None:
    # No request voice, no binding default: the voiceless direct path lets the engine's own
    # model default speak — the just-installed-a-speech-model flow with zero configuration.
    app = create_app(max_resident=2, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/tts-bare",
            json={
                "binding": "mlx",
                "source": "hf",
                "model": _TTS_MODEL,
                "modality": "audio.speech",
            },
        )
        r = client.post(
            "/v1/audio/speech",
            json={"model": "tts-bare", "input": "Speaking with the engine default voice."},
        )
        assert r.status_code == 200, r.text
        assert r.headers["content-type"].startswith("audio/")
        assert len(r.content) > 10_000
