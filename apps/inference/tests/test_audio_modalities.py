"""M18 fast suite — the modality descriptor + the embeddings/audio data-plane endpoints.

Everything real except the model weights: the FakeUpstreamLauncher boots a real in-process
OpenAI-compatible server that now also serves ``/v1/embeddings`` + ``/v1/audio/*``, so LiteLLM
really proxies these and the endpoints prove end-to-end. The load-bearing guards:

* ``capabilities`` reports the frozen §1.2 ``modalities`` vocabulary; an unknown key/value rejected.
* ``vision`` is a derived sub-capability of ``chat`` (not its own modality endpoint).
* the logical-id invariant holds on the NEW endpoints too — an engine name is ``model_not_found``,
  never rewritten onto the wire (§9.1.1 / M3 §3.2 negative test, extended to audio/embeddings).
"""

from __future__ import annotations

import pytest
from _fake_upstream import FAKE_AUDIO, FAKE_EMBEDDING, FAKE_TRANSCRIPT, FakeUpstreamLauncher
from _payloads import managed_payload
from fastapi.testclient import TestClient
from pydantic import ValidationError
from theygent_inference.app import create_app
from theygent_ir import Capabilities

# ── the derivation (pure, on the IR Capabilities model) ──────────────────────


def test_modalities_default_is_chat() -> None:
    # A bare chat engine reports ["chat"] — the default, derived from the flags.
    assert Capabilities().modalities == ["chat"]


def test_vision_is_a_sub_capability_of_chat() -> None:
    # vision is derived from the vision flag, NOT its own endpoint (§1.3).
    assert Capabilities(vision=True).modalities == ["chat", "vision"]


def test_non_chat_modalities_declared_explicitly_are_preserved() -> None:
    # An embeddings / Whisper / TTS engine declares its modality; derivation leaves it alone.
    assert Capabilities(modalities=["embeddings"]).modalities == ["embeddings"]
    assert Capabilities(modalities=["audio.transcription"]).modalities == ["audio.transcription"]


def test_unknown_modality_value_is_rejected() -> None:
    # The frozen vocabulary is a Literal — a typo / out-of-vocab key fails loudly (§4 guard).
    with pytest.raises(ValidationError):
        Capabilities(modalities=["video"])  # ty: ignore[invalid-argument-type]


def test_unknown_capability_key_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Capabilities(speaks_klingon=True)  # ty: ignore[unknown-argument]


# ── the capabilities endpoint reports modalities ─────────────────────────────


def test_capabilities_endpoint_reports_modalities() -> None:
    with TestClient(create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False)) as c:
        c.put("/admin/models/chatty", json={"binding": "mlx", "source": "hf", "model": "m"})
        caps = c.get("/admin/models/chatty/capabilities").json()
        assert caps["modalities"] == ["chat"]


def test_capabilities_endpoint_reports_audio_modality() -> None:
    launcher = FakeUpstreamLauncher(advertised=Capabilities(modalities=["audio.transcription"]))
    with TestClient(create_app(launcher=launcher, enable_reaper=False)) as c:
        c.put("/admin/models/whisper-fast", json={"binding": "mlx", "source": "hf", "model": "w"})
        caps = c.get("/admin/models/whisper-fast/capabilities").json()
        assert caps["modalities"] == ["audio.transcription"]


# ── embeddings (managed + reachable) ─────────────────────────────────────────


def test_embeddings_managed(client: TestClient) -> None:
    client.put("/admin/models/embed-fast", json=managed_payload(binding="llamacpp"))
    r = client.post("/v1/embeddings", json={"model": "embed-fast", "input": "hello"})
    assert r.status_code == 200
    data = r.json()
    assert data["object"] == "list"
    assert data["data"][0]["embedding"] == FAKE_EMBEDDING


def test_embeddings_many_inputs(client: TestClient) -> None:
    client.put("/admin/models/embed-fast", json=managed_payload(binding="llamacpp"))
    r = client.post("/v1/embeddings", json={"model": "embed-fast", "input": ["a", "b", "c"]})
    assert r.status_code == 200
    assert len(r.json()["data"]) == 3


# ── speech-to-text (multipart in → {text}) ───────────────────────────────────


def test_transcription_managed(client: TestClient) -> None:
    client.put("/admin/models/stt-fast", json=managed_payload(binding="llamacpp"))
    r = client.post(
        "/v1/audio/transcriptions",
        data={"model": "stt-fast", "language": "en"},
        files={"file": ("clip.wav", b"RIFFfake-wav-bytes", "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json()["text"] == FAKE_TRANSCRIPT


def test_transcription_requires_file(client: TestClient) -> None:
    client.put("/admin/models/stt-fast", json=managed_payload(binding="llamacpp"))
    r = client.post("/v1/audio/transcriptions", data={"model": "stt-fast"})
    assert r.status_code == 422


# ── text-to-speech (json in → audio bytes) ───────────────────────────────────


def test_speech_managed(client: TestClient) -> None:
    client.put("/admin/models/tts-fast", json=managed_payload(binding="llamacpp"))
    r = client.post(
        "/v1/audio/speech",
        json={"model": "tts-fast", "input": "hello", "voice": "alloy", "response_format": "mp3"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("audio/")
    assert r.content == FAKE_AUDIO


# ── the logical-id invariant on the NEW endpoints ────────────────────────────


@pytest.mark.parametrize("engine", ["llamacpp", "mlx", "vllm"])
def test_engine_name_rejected_on_embeddings(client: TestClient, engine: str) -> None:
    r = client.post("/v1/embeddings", json={"model": engine, "input": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


@pytest.mark.parametrize("engine", ["llamacpp", "mlx", "vllm"])
def test_engine_name_rejected_on_transcription(client: TestClient, engine: str) -> None:
    r = client.post(
        "/v1/audio/transcriptions",
        data={"model": engine},
        files={"file": ("c.wav", b"x", "audio/wav")},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


@pytest.mark.parametrize("engine", ["llamacpp", "mlx", "vllm"])
def test_engine_name_rejected_on_speech(client: TestClient, engine: str) -> None:
    r = client.post("/v1/audio/speech", json={"model": engine, "input": "x", "voice": "alloy"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"


def test_unknown_logical_id_on_embeddings(client: TestClient) -> None:
    r = client.post("/v1/embeddings", json={"model": "nope", "input": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "model_not_found"
