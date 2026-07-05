"""Run persistence — the registry is now a Postgres table.

The proof that matters: a run survives a control-plane restart. The earlier in-memory registry
could not do this; with Postgres the run is shared state, not process-local — which is
also what makes the control-plane horizontally scalable. The wire contract is
unchanged; only storage moved.
"""

from __future__ import annotations

import asyncio

from _db import count_all_messages
from _fake_inference import FULL_MESSAGE, FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app


def test_run_survives_restart(fake_inference: FakeInference, pg_url: str) -> None:
    # First "process": create + complete a run, then shut the app down (engine disposed).
    app1 = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app1) as c1:
        created = c1.post(
            "/runs", json={"input": "hi", "model": "triage-fast", "stream": False}
        ).json()
        run_id = created["runId"]
        assert created["status"] == "completed"

    # Simulate a restart: a brand-new app + engine against the SAME database. The previous
    # in-memory dict would have lost the run here.
    app2 = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app2) as c2:
        got = c2.get(f"/runs/{run_id}").json()
        assert got["id"] == run_id
        assert got["status"] == "completed"
        assert got["model"] == "triage-fast"


def test_one_shot_persists_no_messages(client: TestClient, pg_url: str) -> None:
    # A run with no thread_id is a one-shot: it persists the
    # run row but contributes NO conversational turns.
    body = client.post(
        "/runs", json={"input": "hi", "model": "triage-fast", "stream": False}
    ).json()
    assert body["status"] == "completed"
    assert body["output"] == FULL_MESSAGE
    # The run is reachable, but the message table stays empty — nothing thread-shaped.
    assert client.get(f"/runs/{body['runId']}").json()["thread_id"] is None
    assert asyncio.run(count_all_messages(pg_url)) == 0
