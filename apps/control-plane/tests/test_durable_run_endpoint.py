"""The durable-run endpoint (``POST /agents/{id}/durable-runs``) — the UI's path to run a
durable-only agent (loop/map/subgraph/human) on the durable runtime and poll its run id.

Durable-OFF must degrade cleanly: the endpoint returns ``durable_required`` (400) BEFORE
touching the registry, so a non-durable server never half-starts a run it can't finish. The
durable-ON happy path (enqueue → 202 ``{run_id}`` → poll ``GET /runs/{id}`` → completed) is
exercised end-to-end against the real DBOS runtime + real Postgres, since it needs the
process-global durable singleton.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_durable_run_endpoint_400_when_not_durable(client: TestClient) -> None:
    # The fast-suite client is create_app(durable=False): app.state.durable_runtime is None, so the
    # guard fires first — a clean 400 with the same code the interactive durable-only guard uses,
    # and (guard-first) no agent resolution / no run row.
    resp = client.post("/agents/any.agent/durable-runs", json={"input": "hello"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "durable_required"
