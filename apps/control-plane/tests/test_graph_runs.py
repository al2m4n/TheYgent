"""Fast suite for the IR walker (m5.md §6) — a real graph drives a run over the real HTTP
seam, fake model, real Postgres via testcontainers.

The whole point of M5: ``/graphs/runs`` is behaviourally identical to ``/runs`` for the
trivial graph, but the *path* is IR-driven. So these mirror the M3/M4 assertions (same SSE
shape, same error mapping, same thread replay) — now through the walker — and add the
seam-specific proofs: IR validation, per-node logs, content-hash recording, cycle rejection.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from _db import count_messages, fetch_messages
from _fake_inference import FULL_MESSAGE, FakeInference
from _ir import trivial_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from ulid import ULID


def _parse_sse(text: str) -> list[tuple[str | None, str]]:
    events: list[tuple[str | None, str]] = []
    event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            events.append((event, line[len("data:") :].strip()))
            event = None
    return events


def _graph_run(client: TestClient, *, stream: bool, thread_id: str | None = None) -> dict:
    body: dict = {"ir": trivial_ir(), "input": "name three EU capitals", "stream": stream}
    if thread_id is not None:
        body["thread_id"] = thread_id
    resp = client.post("/graphs/runs", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_durable_only_node_rejected_on_interactive_path(client: TestClient) -> None:
    # F8 regression: a durable-only node (human/subgraph/loop/map) run on the INTERACTIVE
    # /graphs/runs walker must return a CLEAR ``durable_required`` 400 BEFORE a Run — not the
    # walker's raw NotImplementedError mis-mapped to a 502 inference_error "not implemented yet"
    # (which falsely told a builder the human node doesn't exist; it just needs durable mode).
    ir = {
        "schemaVersion": "1.0",
        "id": "agt_human_interactive",
        "name": "h",
        "version": "0.1.0",
        "models": {},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out"}]},
            },
            {
                "id": "n_h",
                "type": "human",
                "kind": "boundary",
                "config": {"prompt": "approve?"},
                "ports": {"in": [{"id": "in"}], "out": [{"id": "out"}]},
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_h",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_h",
                "sourceHandle": "out",
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }
    resp = client.post("/graphs/runs", json={"ir": ir, "input": "approve", "stream": False})
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "durable_required"
    assert "n_h" in body["error"]["message"]


def test_graph_stream_full_loop(client: TestClient, fake_inference: FakeInference) -> None:
    # Happy path: the trivial graph executes and the SSE relay produces the SAME shape /runs
    # does today (the M5 thesis: identical behaviour, IR-driven path).
    with client.stream(
        "POST",
        "/graphs/runs",
        json={"ir": trivial_ir(), "input": "name three EU capitals", "stream": True},
    ) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = _parse_sse("".join(resp.iter_text()))

    assert events[0][0] == "run"
    opening = json.loads(events[0][1])
    run_id = opening["runId"]
    assert opening["status"] == "streaming"

    deltas = [json.loads(d)["delta"] for ev, d in events if ev == "delta"]
    assert "".join(deltas) == FULL_MESSAGE

    assert json.loads(events[-2][1])["status"] == "completed"
    assert events[-1][1] == "[DONE]"

    # The model RESOLVED from the graph's `models["default"]` binding reached the wire as the
    # logical id — never rewritten, never an engine name (§8.4 / §9.1.1).
    assert fake_inference.captured["model"] == "triage-fast"
    assert fake_inference.captured["run_id_header"] == run_id

    # The Run records the graph's identity (§4): id, version, contentHash. A graph run is a Run.
    got = client.get(f"/runs/{run_id}").json()
    assert got["status"] == "completed"
    assert got["graph_id"] == "agt_01J9X8TRIVIAL"
    assert got["graph_version"] == "0.1.0"
    assert got["content_hash"].startswith("sha256:")


def test_graph_non_stream_accumulates(client: TestClient, fake_inference: FakeInference) -> None:
    body = _graph_run(client, stream=False)
    assert body["status"] == "completed"
    assert body["output"] == FULL_MESSAGE
    # $in was substituted with the run input before reaching the wire.
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "name three EU capitals"}
    ]


def test_input_substituted_into_llm_message(
    client: TestClient, fake_inference: FakeInference
) -> None:
    _graph_run(client, stream=False)
    sent = fake_inference.captured["messages"]
    assert sent == [{"role": "user", "content": "name three EU capitals"}]


def test_per_node_logs_keyed_by_run_id(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # The OTel attach-point is exercised, not just present (§5 step 4 / §6): each node's id
    # lands on a structured log keyed by run_id. Spans hang here later.
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _graph_run(client, stream=False)
    run_id = body["runId"]
    # node_id / run_id are structured `extra` fields on the LogRecord (read via getattr).
    by_run = {
        getattr(r, "node_id", None)
        for r in caplog.records
        if r.getMessage() == "graph.node" and getattr(r, "run_id", None) == run_id
    }
    assert by_run == {"n_in", "n_llm", "n_out"}


def test_invalid_ir_rejected_no_run(client: TestClient, pg_url: str) -> None:
    # A bad `kind` for a `type` -> 400 with a precise error and NO Run created (§3.1/§6).
    doc = trivial_ir()
    doc["nodes"][1]["kind"] = "boundary"  # llm must be activity
    resp = client.post("/graphs/runs", json={"ir": doc, "input": "x", "stream": False})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_ir"
    assert "runId" not in resp.json()  # nothing persisted


def test_missing_field_rejected(client: TestClient) -> None:
    doc = trivial_ir()
    del doc["schemaVersion"]  # envelope shape error -> Pydantic -> 400
    resp = client.post("/graphs/runs", json={"ir": doc, "input": "x", "stream": False})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_ir"


def test_cycle_rejected_at_validation(client: TestClient) -> None:
    doc = trivial_ir()
    doc["nodes"][0]["ports"]["in"] = [{"id": "loop", "type": "any"}]
    doc["edges"].append(
        {
            "id": "e3",
            "source": "n_llm",
            "sourceHandle": "ok",
            "target": "n_in",
            "targetHandle": "loop",
            "channel": "data",
        }
    )
    resp = client.post("/graphs/runs", json={"ir": doc, "input": "x", "stream": False})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_ir"


def test_engine_name_binding_rejected(client: TestClient, fake_inference: FakeInference) -> None:
    # The logical-id invariant still holds at the seam (§6): an engine name in the model
    # binding is rejected up front; nothing reaches the inference wire.
    doc = trivial_ir()
    doc["models"]["default"]["model"] = "mlx"
    resp = client.post("/graphs/runs", json={"ir": doc, "input": "x", "stream": False})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "engine_name_not_allowed"
    assert fake_inference.captured["model"] is None


def _recorded_hash(client: TestClient, doc: dict) -> str:
    r = client.post("/graphs/runs", json={"ir": doc, "input": "x", "stream": False}).json()
    return client.get(f"/runs/{r['runId']}").json()["content_hash"]


def test_content_hash_recorded_and_stable(client: TestClient) -> None:
    # The §8.2 promise the Run records: contentHash is content-addressed, NOT layout-addressed.
    # Dragging a node (mutating the view block) must not mint a new version; a real content
    # change must.
    h1 = _recorded_hash(client, trivial_ir())

    # Same graph, node dragged + zoomed — a *different* view block. Hash unchanged (§8.0 rule 2).
    dragged = trivial_ir()
    dragged["view"] = {
        "nodes": {"n_llm": {"position": {"x": 999, "y": 17}, "collapsed": True}},
        "viewport": {"x": -50, "y": 80, "zoom": 2.5},
    }
    assert _recorded_hash(client, dragged) == h1

    # A real content change (the prompt) moves the hash.
    changed = trivial_ir()
    changed["nodes"][1]["config"]["messages"][0]["content"] = "totally different"
    assert _recorded_hash(client, changed) != h1


def test_recorded_hash_is_the_ir_function_and_default_fills(client: TestClient) -> None:
    # Decision D2 (theygent-m10-decisions.md): the Run's recorded contentHash is byte-identical to
    # the IR package's content_hash for the same document — ONE function, so the future registry
    # (M11) and the walker can never disagree. And it canonicalizes the DEFAULT-FILLED model: an IR
    # that omits Port.required hashes identically to one that writes `required: true`.
    from theygent_ir import content_hash, parse_document

    doc = trivial_ir()
    assert _recorded_hash(client, doc) == content_hash(parse_document(doc))

    explicit = trivial_ir()
    for node in explicit["nodes"]:
        for port in node["ports"]["in"] + node["ports"]["out"]:
            port["required"] = True
    assert _recorded_hash(client, explicit) == _recorded_hash(client, doc)


def test_thread_memory_across_graph_runs(client: TestClient, fake_inference: FakeInference) -> None:
    # M4 thread replay, now through the graph path (§6): run #2 in the same thread sees run #1.
    thread_id = str(ULID())
    r1 = _graph_run(client, stream=False, thread_id=thread_id)
    assert r1["status"] == "completed"
    r2 = _graph_run(client, stream=False, thread_id=thread_id)
    assert r2["status"] == "completed"

    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "name three EU capitals"},
        {"role": "assistant", "content": FULL_MESSAGE},
        {"role": "user", "content": "name three EU capitals"},
    ]


def test_graph_thread_pair_persists(client: TestClient, pg_url: str) -> None:
    thread_id = str(ULID())
    _graph_run(client, stream=False, thread_id=thread_id)
    assert asyncio.run(fetch_messages(pg_url, thread_id)) == [
        ("user", "name three EU capitals", 0),
        ("assistant", FULL_MESSAGE, 1),
    ]


def test_error_mapping_stream_surfaces_before_sse_commit(pg_url: str) -> None:
    # The M2/M3 warm-before-stream lesson, now on the graph path: a pre-stream 503/404 (the llm
    # node's open_stream raising before any token) must surface as a CLEAN error status — never
    # a 200 text/event-stream that then breaks. The walker is primed before /graphs/runs commits
    # to an SSE response, so the error is mapped here. Asserted three ways: (1) the status is the
    # mapped 503/404, not 200; (2) the body is a JSON error, not an event-stream; (3) the
    # content-type was never committed to text/event-stream.
    for mode, status, code in (
        ("error_503", 503, "engine_unavailable"),
        ("error_404", 404, "model_not_found"),
    ):
        with FakeInference(mode=mode) as server:
            app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
            with TestClient(app) as client:
                resp = client.post(
                    "/graphs/runs", json={"ir": trivial_ir(), "input": "x", "stream": True}
                )
                assert resp.status_code == status  # mapped clean status, not a 200-then-fail
                assert "text/event-stream" not in resp.headers.get("content-type", "")
                assert resp.json()["error"]["code"] == code  # JSON error body, not an SSE stream
                run_id = resp.json()["runId"]
                assert client.get(f"/runs/{run_id}").json()["status"] == "failed"


def test_error_mapping_non_stream(pg_url: str) -> None:
    for mode, status, code in (
        ("error_503", 503, "engine_unavailable"),
        ("error_404", 404, "model_not_found"),
    ):
        with FakeInference(mode=mode) as server:
            app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
            with TestClient(app) as client:
                resp = client.post(
                    "/graphs/runs", json={"ir": trivial_ir(), "input": "x", "stream": False}
                )
                assert resp.status_code == status
                assert resp.json()["error"]["code"] == code
                run_id = resp.json()["runId"]
                assert client.get(f"/runs/{run_id}").json()["status"] == "failed"


def test_failed_graph_run_writes_no_turns(pg_url: str) -> None:
    # A failed graph run contributes no turn (§4): the thread never holds a dangling user turn.
    thread_id = str(ULID())
    with FakeInference(mode="error_503") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            resp = client.post(
                "/graphs/runs",
                json={"ir": trivial_ir(), "input": "q", "stream": False, "thread_id": thread_id},
            )
            assert resp.status_code == 503
    assert asyncio.run(count_messages(pg_url, thread_id)) == 0


def test_midstream_drop_fails_cleanly(pg_url: str) -> None:
    with FakeInference(mode="drop_midstream") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            with client.stream(
                "POST", "/graphs/runs", json={"ir": trivial_ir(), "input": "x", "stream": True}
            ) as resp:
                assert resp.status_code == 200  # stream already committed (200-then-fail)
                events = _parse_sse("".join(resp.iter_text()))
            assert any(ev == "delta" for ev, _ in events)
            run_id = json.loads(events[0][1])["runId"]
            assert json.loads(events[-2][1])["status"] == "failed"
            assert events[-1][1] == "[DONE]"
            assert client.get(f"/runs/{run_id}").json()["status"] == "failed"


def test_runs_endpoint_unchanged(client: TestClient) -> None:
    # /runs is untouched (§7): a non-graph run still works and records no graph fields.
    resp = client.post("/runs", json={"input": "hi", "model": "triage-fast", "stream": False})
    assert resp.status_code == 200
    got = client.get(f"/runs/{resp.json()['runId']}").json()
    assert got["status"] == "completed"
    assert got["graph_id"] is None
    assert got["content_hash"] is None
