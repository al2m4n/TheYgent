"""The artifact HTTP surface — POST /artifacts (store bytes → ref) + GET /artifacts/{ref}.

These let a browser hand audio to an audio agent's run and retrieve the audio the speak node
produced. Guards: round-trip fidelity, an empty upload is rejected, an unknown id 404s, and the
GET serves ONLY stored `art_` ids (never an arbitrary path/url) so it can't read the filesystem.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_upload_then_download_round_trips(client: TestClient) -> None:
    audio = b"RIFF....fake-wav-bytes...."
    up = client.post("/artifacts", content=audio, headers={"Content-Type": "audio/wav"})
    assert up.status_code == 201
    ref = up.json()
    assert ref["ref"].startswith("art_")
    assert ref["contentType"] == "audio/wav"
    assert ref["bytes"] == len(audio)

    down = client.get(f"/artifacts/{ref['ref']}")
    assert down.status_code == 200
    assert down.content == audio
    assert down.headers["content-type"].startswith("audio/wav")


def test_empty_upload_is_rejected(client: TestClient) -> None:
    r = client.post("/artifacts", content=b"", headers={"Content-Type": "audio/wav"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "empty_artifact"


def test_unknown_artifact_is_404(client: TestClient) -> None:
    r = client.get("/artifacts/art_does_not_exist")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "artifact_not_found"


def test_download_rejects_non_stored_ref_no_arbitrary_file_read(client: TestClient) -> None:
    # A ref the store's fetch would otherwise resolve (a path) must be refused here — the GET is
    # a stored-artifact reader, not a read-any-file primitive.
    for bad in ["etc-passwd", "art_with/slash"]:
        r = client.get(f"/artifacts/{bad}")
        assert r.status_code in (400, 404)
        if r.status_code == 400:
            assert r.json()["error"]["code"] == "invalid_artifact_ref"
