"""M19 §2.2 — the audio model-call nodes (transcribe · speak).

Guards (m19.md §2.2 / §4):
* ``transcribe`` reads an audio REFERENCE → text; ``speak`` text → an audio REFERENCE (the bytes are
  a stored artifact, NOT journaled — the run output carries the ref, never the audio bytes);
* the audio goes to the **inference base URL** (the gateway), never a control-plane route (§10);
* logical-id only — an engine-name binding is rejected up front (the M18 §4 negative test extended).
"""

from __future__ import annotations

import os
import tempfile
from typing import Any

from _fake_inference import FakeInference
from fastapi.testclient import TestClient


def _audio_agent(node_type: str, *, in_handle: str, out_handle: str) -> dict[str, Any]:
    """input -> (transcribe|speak) -> output, wiring the chosen ports."""
    return {
        "schemaVersion": "1.0",
        "id": "agt_audio",
        "name": "audio-agent",
        "version": "0.1.0",
        "models": {"voicebox": {"binding": "mlx", "model": "voicebox-fast"}},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "config": {"modality": "audio" if node_type == "transcribe" else "text"},
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_audio",
                "type": node_type,
                "kind": "activity",
                "config": {"model": "voicebox", "params": {}},
                "ports": {
                    "in": [{"id": in_handle, "type": "any"}],
                    "out": [
                        {"id": out_handle, "type": "any"},
                        {"id": "err", "type": "error"},
                    ],
                },
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "config": {"modality": "text" if node_type == "transcribe" else "audio"},
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_audio",
                "targetHandle": in_handle,
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_audio",
                "sourceHandle": out_handle,
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }


def _run(client: TestClient, ir: dict[str, Any], input_value: Any) -> dict[str, Any]:
    return client.post(
        "/graphs/runs", json={"ir": ir, "input": input_value, "stream": False}
    ).json()


def test_transcribe_audio_ref_to_text(client: TestClient, fake_inference: FakeInference) -> None:
    # The run input is an audio REFERENCE (a local path); the handler fetches the bytes + posts them
    # to the inference plane's /v1/audio/transcriptions (NOT a control-plane route — §10).
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(b"RIFF....fake-wav-bytes")
        path = f.name
    try:
        ir = _audio_agent("transcribe", in_handle="audio", out_handle="text")
        out = _run(client, ir, {"ref": path, "contentType": "audio/wav"})
        assert out["status"] == "completed"
        assert out["output"] == "transcribed text"  # the fake STT response
        assert fake_inference.captured["audio_hit"] is True  # audio went to the inference plane
        assert fake_inference.captured["audio_model"] == "voicebox-fast"  # logical id, not engine
        assert fake_inference.captured["audio_bytes_in"] > 0  # the bytes reached the data plane
    finally:
        os.unlink(path)


def test_speak_text_to_audio_reference(client: TestClient, fake_inference: FakeInference) -> None:
    ir = _audio_agent("speak", in_handle="text", out_handle="audio")
    out = _run(client, ir, "hello there")
    assert out["status"] == "completed"
    # The output is a REFERENCE (a stored artifact), not the audio bytes (the bytes never journal).
    ref = __import__("json").loads(out["output"])
    assert ref["ref"].startswith("art_")
    assert ref["contentType"] == "audio/mpeg"
    assert "FAKE_AUDIO_BYTES" not in out["output"]  # bytes are NOT in the run output
    assert fake_inference.captured["audio_hit"] is True


def test_transcribe_missing_ref_binds_err(client: TestClient) -> None:
    # A bad audio ref binds the err handle (the tool ok/err contract) — but here err isn't wired to
    # output, so the run completes with no output (the M9 empty-output legibility), not a crash.
    ir = _audio_agent("transcribe", in_handle="audio", out_handle="text")
    out = _run(client, ir, {"ref": "/does/not/exist.wav", "contentType": "audio/wav"})
    assert out["status"] == "completed"  # honest empty, not a 500


def test_transcribe_engine_name_rejected(client: TestClient) -> None:
    # Logical-id only (§1.3): an engine-name model binding is rejected up front, before a Run.
    ir = _audio_agent("transcribe", in_handle="audio", out_handle="text")
    ir["models"]["voicebox"] = {"binding": "mlx", "model": "mlx"}  # an engine name
    resp = client.post("/graphs/runs", json={"ir": ir, "input": {"ref": "x"}, "stream": False})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "engine_name_not_allowed"
