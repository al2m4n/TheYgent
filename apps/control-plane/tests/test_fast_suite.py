"""Fast suite (gates PRs) — control-plane against a real (fake-model) inference plane.

Everything is real except the model: the gateway-client really talks HTTP to a threaded
OpenAI-compatible server. Asserts the full run loop.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from _fake_inference import FULL_MESSAGE, FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app


def _parse_sse(text: str) -> list[tuple[str | None, str]]:
    """Return [(event, data), ...] from an SSE body."""
    events: list[tuple[str | None, str]] = []
    event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            events.append((event, line[len("data:") :].strip()))
            event = None
    return events


def test_stream_run_full_loop(client: TestClient, fake_inference: FakeInference) -> None:
    with client.stream(
        "POST",
        "/runs",
        json={"input": "hello", "model": "triage-fast", "stream": True},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse("".join(resp.iter_text()))

    # Opening event carries the run id + streaming status.
    assert events[0][0] == "run"
    opening = json.loads(events[0][1])
    run_id = opening["runId"]
    assert opening["status"] == "streaming"

    # Reassembled deltas match the model output.
    deltas = [json.loads(d)["delta"] for ev, d in events if ev == "delta"]
    assert "".join(deltas) == FULL_MESSAGE

    # Terminal run event = completed, then [DONE].
    assert json.loads(events[-2][1])["status"] == "completed"
    assert events[-1][1] == "[DONE]"

    # The logical id reached the wire verbatim (never rewritten to an engine name).
    assert fake_inference.captured["model"] == "triage-fast"
    # Run-id was forwarded as a header for request identity.
    assert fake_inference.captured["run_id_header"] == run_id

    # GET /runs/{id} reflects the terminal status.
    got = client.get(f"/runs/{run_id}").json()
    assert got["status"] == "completed"
    assert got["model"] == "triage-fast"


def test_non_stream_accumulates(client: TestClient) -> None:
    resp = client.post("/runs", json={"input": "hi", "model": "triage-fast", "stream": False})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["output"] == FULL_MESSAGE
    assert client.get(f"/runs/{body['runId']}").json()["status"] == "completed"


def test_engine_name_rejected(client: TestClient, fake_inference: FakeInference) -> None:
    # Negative test: an engine name is not a logical id and is rejected up front;
    # it must never reach the inference wire.
    for engine in ("mlx", "vllm", "llamacpp"):
        resp = client.post("/runs", json={"input": "x", "model": engine, "stream": False})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "engine_name_not_allowed"
    assert fake_inference.captured["model"] is None  # nothing forwarded


# Both error codes are asserted on BOTH the streaming and non-streaming paths: a pre-stream
# error must surface as a clean status (not a 200-then-broken-stream), and the non-stream twin
# must map identically. In every case the run must end `failed` (carried back via `runId`), not
# left dangling — that's what the error-response `runId` is for.
@pytest.mark.parametrize("stream", [True, False])
@pytest.mark.parametrize(
    ("mode", "status", "code"),
    [
        ("error_503", 503, "engine_unavailable"),
        ("error_404", 404, "model_not_found"),
    ],
)
def test_error_mapping_both_paths(mode: str, status: int, code: str, stream: bool) -> None:
    with FakeInference(mode=mode) as server:
        app = create_app(inference_base_url=server.v1_url)
        with TestClient(app) as client:
            resp = client.post(
                "/runs", json={"input": "x", "model": "triage-fast", "stream": stream}
            )
            # Clean status + code, no hang, no stacktrace leak.
            assert resp.status_code == status
            assert resp.json()["error"]["code"] == code
            # The run is marked failed (not left dangling) — correlate via the returned runId.
            run_id = resp.json()["runId"]
            got = client.get(f"/runs/{run_id}").json()
            assert got["status"] == "failed"
            assert got["error"]


def test_midstream_drop_fails_cleanly() -> None:
    with FakeInference(mode="drop_midstream") as server:
        app = create_app(inference_base_url=server.v1_url)
        with TestClient(app) as client:
            with client.stream(
                "POST", "/runs", json={"input": "x", "model": "triage-fast", "stream": True}
            ) as resp:
                assert resp.status_code == 200  # stream already committed (200-then-fail)
                # Fully draining the iterator is itself the half-open check: if the response
                # hung, this join would block until the test times out rather than return.
                events = _parse_sse("".join(resp.iter_text()))

            # It streamed real content first, THEN dropped mid-stream (not a fail-at-open):
            assert any(ev == "delta" for ev, _ in events)
            # ...and closed cleanly with a terminal failed event followed by [DONE].
            run_id = json.loads(events[0][1])["runId"]
            terminal = json.loads(events[-2][1])
            assert terminal["status"] == "failed"
            assert events[-1][1] == "[DONE]"
            assert client.get(f"/runs/{run_id}").json()["status"] == "failed"


def test_readyz_reachable(client: TestClient) -> None:
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


def test_readyz_unreachable() -> None:
    # Point at a closed port: honest not-ready, not a green light.
    app = create_app(inference_base_url="http://127.0.0.1:1/v1")
    with TestClient(app) as client:
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["status"] == "not-ready"


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


async def test_concurrent_runs_stay_independent(fake_inference: FakeInference) -> None:
    # Two genuinely overlapping runs (asyncio.gather, real HTTP to the fake) must get
    # distinct run ids and independent status — neither's created->completed transition
    # clobbers the other. The Run is the spine everything hangs off, and it is now SHARED
    # state in Postgres (not process-local), so prove independence against the real DB.
    app = create_app(inference_base_url=fake_inference.v1_url)
    transport = httpx.ASGITransport(app=app)
    # Drive the lifespan so the async engine/sessionmaker exist (ASGITransport won't).
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            body = {"input": "hi", "model": "triage-fast", "stream": False}
            r1, r2 = await asyncio.gather(ac.post("/runs", json=body), ac.post("/runs", json=body))

            assert r1.status_code == r2.status_code == 200
            assert r1.json()["runId"] != r2.json()["runId"]
            # Both completed independently; shared Postgres state kept them separate.
            assert r1.json()["status"] == "completed"
            assert r2.json()["status"] == "completed"

            g1, g2 = await asyncio.gather(
                ac.get(f"/runs/{r1.json()['runId']}"), ac.get(f"/runs/{r2.json()['runId']}")
            )
        assert g1.json()["status"] == "completed" and g2.json()["status"] == "completed"
