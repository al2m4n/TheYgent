"""Fast suite for the input/output boundary contract's loud-failure edges.

Three silent behaviors made honest:

* Two output nodes that BOTH execute used to silently last-win (the run output was whichever
  output node topo order visited last) — now a loud 422, same no-silent-ambiguity rule as a
  single-value consumer with two in-ports.
* A graph with no output node (or whose taken branch reaches none) used to complete green with
  ``output: ""``/``error: null`` — and, threaded, append a BLANK assistant turn. Now it carries an
  honest empty-output reason and appends no turn.
* A node type the IR schema declares but no runtime executes (code/rag/…) used to create a Run
  and then die as a misleading 502 ``inference_error`` — now a clean 400 before any Run.
"""

from __future__ import annotations

import asyncio

from _db import get_run_row
from _ir import _doc, _edge, _node, tool_unhandled_err_ir
from fastapi.testclient import TestClient


def _two_live_outputs_ir() -> dict:
    # input fans out to an echo AND directly to a second output; both branches are live, so both
    # output nodes execute — the ambiguous shape.
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_echo",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "$in"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out_a", "output", "boundary", ins=["in"]),
            _node("n_out_z", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_echo"),
            _edge("e2", "n_echo", "ok", "n_out_a"),
            _edge("e3", "n_in", "out", "n_out_z"),
        ],
    )


def test_two_live_outputs_fail_loudly(client: TestClient) -> None:
    body = {"ir": _two_live_outputs_ir(), "input": "x", "stream": False}
    r = client.post("/graphs/runs", json=body)
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "template_error"
    assert "second output node" in body["error"]["message"]
    # The run exists (the ambiguity is a walk-time error) and is honestly failed.
    got = client.get(f"/runs/{body['runId']}").json()
    assert got["status"] == "failed"


def _no_output_ir() -> dict:
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_echo",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "$in"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
        ],
        [_edge("e1", "n_in", "out", "n_echo")],
    )


def test_zero_output_graph_is_honest_and_appends_no_blank_turn(client: TestClient) -> None:
    r = client.post(
        "/graphs/runs",
        json={"ir": _no_output_ir(), "input": "hi", "thread_id": "t_blank_guard", "stream": False},
    )
    assert r.status_code == 200
    body = r.json()
    got = client.get(f"/runs/{body['runId']}").json()
    assert got["status"] == "completed"
    assert got["error"] is not None and "declares no output node" in got["error"]
    # No blank assistant turn: the thread exists but carries no messages.
    thread = client.get("/threads/t_blank_guard").json()
    assert thread["messages"] == []


def _skipped_output_ir() -> dict:
    # Output nodes exist, but the taken router branch reaches none: echo emits {"handle": "yes"},
    # the router activates "yes" (wired nowhere), and the only output node hangs off "no".
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_decide",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": {"handle": "yes"}}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_route",
                "router",
                "orchestration",
                config={"select": "$in.in.handle"},
                ins=["in"],
                outs=["yes", "no"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_decide"),
            _edge("e2", "n_decide", "ok", "n_route"),
            _edge("e3", "n_route", "no", "n_out"),
        ],
    )


def test_taken_branch_without_output_is_honest(client: TestClient) -> None:
    r = client.post(
        "/graphs/runs", json={"ir": _skipped_output_ir(), "input": "x", "stream": False}
    )
    assert r.status_code == 200
    got = client.get(f"/runs/{r.json()['runId']}").json()
    assert got["status"] == "completed"
    assert got["error"] is not None and "no output node executed" in got["error"]


def test_unexecutable_node_type_is_a_clean_400(client: TestClient, pg_url: str) -> None:
    # `code` is legal IR (validate_graph passes) but no runtime executes it: reject before a Run,
    # not a Run that dies as a misleading 502 inference_error.
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node("n_code", "code", "activity", config={}, ins=["in"], outs=["out"]),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_code"),
            _edge("e2", "n_code", "out", "n_out"),
        ],
    )
    r = client.post("/graphs/runs", json={"ir": ir, "input": "x", "stream": False})
    assert r.status_code == 400
    body = r.json()
    assert body["error"]["code"] == "node_type_unavailable"
    assert "n_code" in body["error"]["message"]
    assert "runId" not in body  # rejected before any Run was created


def test_empty_output_reason_names_the_cause(client: TestClient, pg_url: str) -> None:
    # The honest empty-output reason carries the failing node's error MESSAGE, not just its id —
    # here the failed tool bound its error to a DECLARED-but-unwired err port.
    ir = tool_unhandled_err_ir("echo", {"value": "$in.in.nope"})
    created = client.post("/graphs/runs", json={"ir": ir, "input": "go", "stream": False}).json()
    row = asyncio.run(get_run_row(pg_url, created["runId"]))
    assert row is not None
    err = str(row["error"])
    assert "upstream error on node 'n_tool'" in err
    # The cause (the unresolvable-ref message) rides along after the node id.
    assert err.count(":") >= 2 and "nope" in err


def test_empty_output_reason_names_the_cause_without_an_err_port(
    client: TestClient, pg_url: str
) -> None:
    # The dropped-error path: the failing tool declares NO error-typed out-port at all, so the
    # error has no handle to bind — it must still surface as the empty-output CAUSE instead of
    # vanishing (the reserved dropped-error record inside _bind_outcome).
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_tool",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "$in.in.nope"}},
                ins=["in"],
                outs=["ok"],  # no err port — the failure would otherwise vanish
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_tool"), _edge("e2", "n_tool", "ok", "n_out")],
    )
    created = client.post("/graphs/runs", json={"ir": ir, "input": "go", "stream": False}).json()
    row = asyncio.run(get_run_row(pg_url, created["runId"]))
    assert row is not None
    err = str(row["error"])
    assert "upstream error on node 'n_tool'" in err
    assert "nope" in err  # the dropped error's message, not just the node id
