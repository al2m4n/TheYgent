"""Fast suite for M9 §2.2 (durable run output) + §2.4 (honest empty output).

Before M9 an un-threaded run's output lived ONLY in the live SSE stream (finding F5.3): close the
tab and the answer was gone — ``GET /runs/{id}`` returned metadata, no output. M9 persists the
final output on the run row regardless of threading, additively (the POST wire contract is
unchanged). §2.4 makes "nothing reached the output because an upstream node errored" an honest
signal instead of a green ``completed`` with ``output: ""`` and ``error: null``.
"""

from __future__ import annotations

import asyncio

from _db import get_run_row
from _fake_inference import FULL_MESSAGE, FakeInference
from _ir import tool_unhandled_err_ir, trivial_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app


def test_unthreaded_run_output_persists_and_survives_restart(
    fake_inference: FakeInference, pg_url: str
) -> None:
    # A one-shot /runs (no thread_id) — before M9 it persisted NO output.
    app1 = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app1) as c1:
        created = c1.post(
            "/runs", json={"input": "hi", "model": "triage-fast", "stream": False}
        ).json()
        run_id = created["runId"]
        # GET now returns the output, not just metadata.
        assert c1.get(f"/runs/{run_id}").json()["output"] == FULL_MESSAGE

    # Simulate a restart: new app + engine, SAME database. The output is still there.
    app2 = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app2) as c2:
        got = c2.get(f"/runs/{run_id}").json()
        assert got["status"] == "completed"
        assert got["output"] == FULL_MESSAGE


def test_streamed_run_output_persists(client: TestClient) -> None:
    # The streamed path accumulates the deltas and persists them too, so a closed tab doesn't lose
    # the answer that only ever existed on the wire.
    with client.stream(
        "POST", "/runs", json={"input": "hi", "model": "triage-fast", "stream": True}
    ) as resp:
        assert resp.status_code == 200
        body = "".join(resp.iter_text())
    # The runId is in the opening run event.
    import json

    run_id = next(
        json.loads(line[len("data:") :])["runId"]
        for line in body.splitlines()
        if line.startswith("data:") and "runId" in line
    )
    assert client.get(f"/runs/{run_id}").json()["output"] == FULL_MESSAGE


def test_unthreaded_graph_run_output_persists(client: TestClient) -> None:
    # The cockpit's "Output is not persisted for one-shot runs" gap: an un-threaded /graphs/runs
    # now returns its output from GET /runs/{id}.
    created = client.post(
        "/graphs/runs", json={"ir": trivial_ir(), "input": "x", "stream": False}
    ).json()
    assert created["output"] == FULL_MESSAGE
    assert client.get(f"/runs/{created['runId']}").json()["output"] == FULL_MESSAGE


def test_empty_output_from_upstream_error_is_legible(client: TestClient, pg_url: str) -> None:
    # §2.4: a tool errors (an unresolvable $in.in.<field> arg — the str input has no such field),
    # its `err` handle is wired NOWHERE, and its `ok` path to the output is therefore skipped —
    # nothing reaches the output boundary. The run must NOT report a bare green completed with error
    # null; it carries an honest reason that names the failing node.
    ir = tool_unhandled_err_ir("echo", {"value": "$in.in.nope"})
    created = client.post("/graphs/runs", json={"ir": ir, "input": "go", "stream": False}).json()
    run_id = created["runId"]
    row = asyncio.run(get_run_row(pg_url, run_id))
    assert row is not None
    assert "output empty" in str(row["error"])
    assert "n_tool" in str(row["error"])  # the failing node is named
    # The error field is non-null — no longer masquerading as a clean success.
    got = client.get(f"/runs/{run_id}").json()
    assert got["error"] is not None and "output empty" in got["error"]
