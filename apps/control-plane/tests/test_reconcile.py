"""Fast suite for M9 §2.1 — startup reconciliation of orphaned runs (finding F5.2).

A control-plane crash mid-stream leaves a run stuck at ``streaming`` forever — ``error`` null,
``updated_at`` frozen — a zombie that lies about being alive. The in-process M5 walker can't resume
it (that's the durable-runtime fork, §4), but it must not lie. On startup the control-plane sweeps
every non-terminal run to ``failed`` with a distinct reason so it's distinguishable from a real
inference failure. This is the cheap honest mitigation, nothing more.
"""

from __future__ import annotations

import asyncio

from _db import get_run_row, seed_run
from _fake_inference import FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from ulid import ULID


def test_streaming_run_swept_to_failed_on_startup(
    fake_inference: FakeInference, pg_url: str
) -> None:
    # Seed a run stuck at `streaming` as a crash would have left it (the app never creates one).
    zombie = str(ULID())
    asyncio.run(seed_run(pg_url, zombie, "streaming"))

    # Starting a fresh app runs the lifespan reconciliation sweep.
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app) as client:
        got = client.get(f"/runs/{zombie}").json()
        assert got["status"] == "failed"
        assert "interrupted" in got["error"]
        assert "restarted" in got["error"]  # distinct reason, not a generic inference failure


def test_created_run_also_swept(fake_inference: FakeInference, pg_url: str) -> None:
    stuck = str(ULID())
    asyncio.run(seed_run(pg_url, stuck, "created"))
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app):
        row = asyncio.run(get_run_row(pg_url, stuck))
    assert row is not None and row["status"] == "failed"


def test_durable_run_not_swept(fake_inference: FakeInference, pg_url: str) -> None:
    # A durable run recovers and resumes via its workflow engine after a crash — sweeping it to
    # `failed` would report a false terminal state for a run that is about to complete (and, in
    # the split API/worker topology, fail a run a healthy sibling process is executing).
    durable = str(ULID())
    inproc = str(ULID())
    asyncio.run(seed_run(pg_url, durable, "created", runtime="durable"))
    asyncio.run(seed_run(pg_url, inproc, "created", runtime="inproc"))
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app) as client:
        assert client.get(f"/runs/{durable}").json()["status"] == "created"
        assert client.get(f"/runs/{inproc}").json()["status"] == "failed"


def test_terminal_runs_untouched(fake_inference: FakeInference, pg_url: str) -> None:
    # A completed (or failed) run must be left exactly as it was — the sweep only touches zombies.
    done = str(ULID())
    asyncio.run(seed_run(pg_url, done, "completed"))
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app) as client:
        got = client.get(f"/runs/{done}").json()
        assert got["status"] == "completed"
        assert got["error"] is None
