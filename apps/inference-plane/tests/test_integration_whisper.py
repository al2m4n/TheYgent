"""Integration test — REAL whisper.cpp speech-to-text on /v1/audio/transcriptions.

The (llamacpp, audio.transcription) slot is "supported" only because this runs green: the real
``whisper-server`` is spawned for the bound weights, serves the OpenAI route via its configurable
inference path, and transcribes real synthesized speech. Skipped by default
(``-m 'not integration'``) and skips clean when the prereqs are absent. Run::

    THEYGENT_WHISPER_MODEL=/path/ggml-base.en.bin \
        uv run --package theygent-inference-plane pytest -m integration \
        apps/inference-plane/tests/test_integration_whisper.py

Prereqs: ``whisper-server`` resolvable (PATH or THEYGENT_WHISPERCPP_BIN, e.g.
``brew install whisper-cpp``) + a local whisper ggml model + the macOS ``say`` synthesizer and
``ffmpeg`` (to produce real speech audio to transcribe).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile

import pytest
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import WhisperCppLauncher

pytestmark = pytest.mark.integration

_WHISPER_MODEL = os.environ.get("THEYGENT_WHISPER_MODEL")
_HAVE_SERVER = WhisperCppLauncher().ready
_HAVE_SAY = shutil.which("say") is not None
_HAVE_FFMPEG = shutil.which("ffmpeg") is not None

_skip = pytest.mark.skipif(
    not _WHISPER_MODEL or not _HAVE_SERVER or not _HAVE_SAY or not _HAVE_FFMPEG,
    reason="needs THEYGENT_WHISPER_MODEL (a ggml whisper model) + whisper-server on "
    "PATH/THEYGENT_WHISPERCPP_BIN + macOS `say` + ffmpeg",
)


def _speech_wav(text: str, path: str) -> None:
    """Real spoken audio to transcribe — synthesized locally, 16 kHz mono wav."""
    aiff = path.replace(".wav", ".aiff")
    subprocess.run(["say", "-o", aiff, text], check=True)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", aiff, "-ar", "16000", "-ac", "1", path],
        check=True,
    )


@_skip
def test_real_whisper_transcription() -> None:
    app = create_app(max_resident=2, enable_reaper=False)
    with TestClient(app) as client:
        client.put(
            "/admin/models/stt-local",
            json={
                "binding": "llamacpp",
                "source": "local-path",
                "model": _WHISPER_MODEL,
                "modality": "audio.transcription",
            },
        )

        # The handle declares exactly the transcription modality.
        caps = client.get("/admin/models/stt-local/capabilities").json()
        assert caps["modalities"] == ["audio.transcription"]

        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "speech.wav")
            _speech_wav("the quick brown fox jumps over the lazy dog", wav)
            with open(wav, "rb") as fh:
                r = client.post(
                    "/v1/audio/transcriptions",
                    data={"model": "stt-local"},
                    files={"file": ("speech.wav", fh, "audio/wav")},
                )
        assert r.status_code == 200, r.text
        text = r.json()["text"].lower()
        assert "quick brown fox" in text
