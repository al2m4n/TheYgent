"""M19 §2.9 — the transform node (deterministic JSON-template reshape, no code sandbox).

A ``transform`` reshapes ``A.out → B.in`` without a code node: its ``expr`` is a JSON document whose
string leaves may be ``$in`` refs, resolved over the journaled inputs. Deterministic, inline (no
I/O), and a bad expr / unresolvable ref fails LOUDLY (422 ``transform_error``), never a silent
empty reshape.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient


def _transform_agent(expr: str) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "id": "agt_transform",
        "name": "transform-agent",
        "version": "0.1.0",
        "models": {},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_t",
                "type": "transform",
                "kind": "orchestration",
                "config": {"expr": expr},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "out", "type": "any"}],
                },
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_t",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_t",
                "sourceHandle": "out",
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


def test_transform_reshapes_object(client: TestClient) -> None:
    ir = _transform_agent('{"city": "$in.in.location.city", "when": "$in.in.date"}')
    out = _run(client, ir, {"location": {"city": "berlin"}, "date": "monday"})
    assert out["status"] == "completed"
    assert json.loads(out["output"]) == {"city": "berlin", "when": "monday"}


def test_transform_preserves_typed_values(client: TestClient) -> None:
    # A pure $in ref keeps its (non-string) type — a list stays a list, a number a number.
    ir = _transform_agent('{"items": "$in.in.items", "n": "$in.in.count"}')
    out = _run(client, ir, {"items": ["a", "b"], "count": 7})
    assert json.loads(out["output"]) == {"items": ["a", "b"], "n": 7}


def test_transform_bad_json_expr_fails_loudly(client: TestClient) -> None:
    resp = client.post(
        "/graphs/runs",
        json={"ir": _transform_agent("{not json"), "input": {}, "stream": False},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "transform_error"


def test_transform_unresolvable_ref_fails_loudly(client: TestClient) -> None:
    ir = _transform_agent('{"x": "$in.in.missing.deep"}')
    resp = client.post("/graphs/runs", json={"ir": ir, "input": {"present": 1}, "stream": False})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "transform_error"
