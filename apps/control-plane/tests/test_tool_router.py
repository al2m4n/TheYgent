"""Fast suite for the M6 node types — ``tool`` and ``router`` (m6.md §5).

The milestone where a graph stops being a wrapped prompt and becomes an agent that *does
something*. Everything real except the model: the walker dispatches the two new handlers, a
built-in tool makes a genuine outbound HTTP call to a threaded local server, and a router
branches on an upstream decision with the un-taken branch provably skipped (asserted via the
structured-log seam). Real Postgres via testcontainers (M4 conventions).
"""

from __future__ import annotations

import json
import logging

import pytest
from _fake_inference import FakeInference
from _http import ThreadedHTTP
from _ir import agent_ir, router_ir, tool_echo_ir, tool_http_ir, tool_ok_err_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_control_plane.walker import _PortInputs, _resolve_ref


def _ports(values: dict, declared: list[str] | None = None) -> _PortInputs:
    """A node's in-port map for the unit tests: declared ports default to the fed ones."""
    return _PortInputs(
        values=values, declared=frozenset(declared if declared is not None else values)
    )


def _post(client: TestClient, ir: dict, *, input_: str = "go", stream: bool = False):
    return client.post("/graphs/runs", json={"ir": ir, "input": input_, "stream": stream})


def _skipped_nodes(caplog: pytest.LogCaptureFixture, run_id: str) -> set[str]:
    """Node ids logged with skipped=True for this run (the M6 §4 skip seam)."""
    return {
        str(getattr(r, "node_id", ""))
        for r in caplog.records
        if r.getMessage() == "graph.node"
        and getattr(r, "run_id", None) == run_id
        and getattr(r, "skipped", False)
    }


# ── tool ──────────────────────────────────────────────────────────────────────


def test_tool_echo_happy_path(client: TestClient) -> None:
    # Dispatch + arg templating + ok-binding: echo returns the templated input unchanged.
    body = _post(client, tool_echo_ir(), input_="hello").json()
    assert body["status"] == "completed"
    assert body["output"] == "hello"


def test_tool_http_fetch_real_io(client: TestClient) -> None:
    # Real async tool execution: a genuine outbound GET to a threaded local server. A non-200
    # is a return value (bound to ok), not an error — here a clean 200 with a known body.
    with ThreadedHTTP(body="weather: sunny", status=200) as url:
        body = _post(client, tool_http_ir(url)).json()
    assert body["status"] == "completed"
    result = json.loads(body["output"])
    assert result["status"] == 200
    assert result["body"] == "weather: sunny"


def test_tool_http_fetch_non_200_is_a_value_not_an_error(client: TestClient) -> None:
    # HTTP 404 is a real return value: it binds to ok (the fetch succeeded), not err.
    with ThreadedHTTP(body="nope", status=404) as url:
        body = _post(client, tool_http_ir(url)).json()
    assert body["status"] == "completed"
    assert json.loads(body["output"])["status"] == 404


def test_tool_raises_binds_err_branch_and_completes(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # A transport failure (connection refused) RAISES inside the tool → the err handle is bound
    # with the message and the run COMPLETES (a tool error is a structured output, not a run
    # failure, m6.md §4). Downstream err edges run; downstream ok edges are skipped.
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _post(client, tool_http_ir("http://127.0.0.1:1")).json()
    assert body["status"] == "completed"
    assert body["output"]  # the err message flowed to the output node
    assert "n_ok" in _skipped_nodes(caplog, body["runId"])  # ok branch skipped
    assert "n_err" not in _skipped_nodes(caplog, body["runId"])  # err branch ran


def test_tool_not_found_rejected_at_validation(client: TestClient) -> None:
    # An unknown tool is caught up front → 400, no Run created (m6.md §5) — never a runtime miss.
    doc = tool_echo_ir()
    doc["nodes"][1]["config"]["tool"] = "nope"
    resp = _post(client, doc)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "tool_not_found"
    assert "runId" not in resp.json()


# ── $in.<port> / $in.<port>.<path> value references (shared tool-arg + router-select resolver) ──
# The load-bearing invariant: an undeclared port or a missing path ERRORS clearly, it never
# silently returns None that then surprises a downstream node. Port-first (M10 §1.2). Pinned at the
# unit level here and end-to-end below.


def test_resolve_ref_port_addressing_and_passthrough() -> None:
    assert _resolve_ref("$in", _ports({"in": {"a": 1}}), "n") == {
        "a": 1
    }  # default port whole value
    # $in.<port> selects a named in-port; $in.<port>.<path> drills into it.
    p = _ports({"file": {"body": "x"}, "question": "why?"})
    assert _resolve_ref("$in.question", p, "n") == "why?"
    assert _resolve_ref("$in.file.body", p, "n") == "x"  # nested traversal into a port
    # A JSON-string port value is parsed mid-traversal (the llm-output-feeds-router/tool case).
    js = _ports({"in": '{"payload": {"url": "http://x"}}'})
    assert _resolve_ref("$in.in.payload.url", js, "n") == "http://x"
    assert (
        _resolve_ref("hello", _ports({"in": 1}), "n") == "hello"
    )  # non-$in literal passes through
    assert _resolve_ref({"k": "v"}, _ports({}), "n") == {"k": "v"}  # non-str literal passes through


def test_resolve_ref_errors_are_loud_and_precise() -> None:
    # An undeclared port → loud error naming the available ports (never a silent None).
    with pytest.raises(ValueError, match=r"no in-port named 'ghost'"):
        _resolve_ref("$in.ghost", _ports({"file": 1}), "n")
    # Intermediate port exists, final field absent — must RAISE, not None.
    with pytest.raises(ValueError, match=r"no field 'missing' in in-port 'file'"):
        _resolve_ref("$in.file.missing", _ports({"file": {"b": 1}}), "n")
    # Drilling into a non-dict port value (a scalar) — must raise, not None.
    with pytest.raises(ValueError, match="cannot resolve"):
        _resolve_ref("$in.file.b", _ports({"file": 7}), "n")
    # Bare $in with no default `in` port → loud error naming the ports.
    with pytest.raises(ValueError, match=r"bare \$in needs a default in-port named 'in'"):
        _resolve_ref("$in", _ports({"file": 1}), "n")


# ── router ────────────────────────────────────────────────────────────────────


def test_router_happy_path_skips_untaken_branch(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # Upstream emits {handle: "yes", ...} → only the yes-branch runs; the no-branch is SKIPPED
    # (asserted via the structured-log seam, not just absence of output).
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _post(client, router_ir({"handle": "yes", "payload": 42})).json()
    assert body["status"] == "completed"
    skipped = _skipped_nodes(caplog, body["runId"])
    assert "n_no" in skipped
    assert "n_yes" not in skipped


def test_router_selects_no_branch(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _post(client, router_ir({"handle": "no", "payload": 0})).json()
    assert body["status"] == "completed"
    skipped = _skipped_nodes(caplog, body["runId"])
    assert "n_yes" in skipped
    assert "n_no" not in skipped


def test_router_miss_fails(client: TestClient) -> None:
    # A handle the router doesn't declare → run failed, clear error, no fallback guessing (§3.2).
    resp = _post(client, router_ir({"handle": "maybe", "payload": 1}))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "router_error"
    assert client.get(f"/runs/{resp.json()['runId']}").json()["status"] == "failed"


def test_router_no_handle_field_fails(client: TestClient) -> None:
    # Upstream output is missing `handle` → run failed, clear error (§3.2).
    resp = _post(client, router_ir({"payload": 1}))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "router_error"
    assert client.get(f"/runs/{resp.json()['runId']}").json()["status"] == "failed"


def test_router_nested_missing_path_fails_clearly(client: TestClient) -> None:
    # The $in.a.missing case end-to-end: a nested select whose final key is absent must fail the
    # run with a clear reason — never resolve to None and route on it (no silent foot-gun).
    doc = router_ir({"deep": {"other": 1}})
    doc["nodes"][2]["config"]["select"] = (
        "$in.in.deep.missing"  # n_route; in-port `in` → "deep" exists, "missing" not
    )
    resp = _post(client, doc)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "router_error"
    assert "cannot resolve" in resp.json()["error"]["message"]
    assert client.get(f"/runs/{resp.json()['runId']}").json()["status"] == "failed"


def test_tool_missing_arg_ref_binds_err_branch(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # A tool arg that references a missing path is an unresolved-input failure: it binds the err
    # handle with the clear message and the run continues (§4) — the err branch runs, the ok
    # branch is skipped. The downstream node never receives a surprise None.
    doc = tool_ok_err_ir(
        "echo", {"value": "$in.in.missing"}
    )  # input "go" str: in-port `in` has no .missing
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _post(client, doc).json()
    assert body["status"] == "completed"
    assert "cannot resolve" in body["output"]  # the clear error flowed to the err output node
    assert "n_ok" in _skipped_nodes(caplog, body["runId"])  # ok branch skipped, not fed None


# ── the combined agent shape (the demo, m6.md §6) ─────────────────────────────


def test_combined_agent_graph(pg_url: str) -> None:
    # input → llm → router → {tool → out, out}. The fake makes the llm's routing decision
    # deterministic; the router takes "yes"; the tool extracts the payload. The shape that
    # motivates M6 — an agent, not a wrapped prompt.
    decision = '{"handle": "yes", "payload": {"city": "Prague"}}'
    with FakeInference(response=decision) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = _post(client, agent_ir(), input_="weather in Prague?").json()
    assert body["status"] == "completed"
    # The tool ran on the routed payload; its result is the run output.
    assert json.loads(body["output"]) == {"city": "Prague"}


def test_combined_agent_streams_llm_decision(pg_url: str) -> None:
    # Streaming variant: the llm's routing decision streams as deltas (the SSE shape is
    # unchanged), then the run completes after the router+tool run downstream.
    decision = '{"handle": "no", "payload": null}'
    with FakeInference(response=decision) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/graphs/runs",
                json={"ir": agent_ir(), "input": "hi", "stream": True},
            ) as resp:
                assert resp.status_code == 200
                events = "".join(resp.iter_text())
    # The llm decision streamed as delta events; the terminal event is completed.
    deltas = "".join(
        json.loads(line[len("data:") :].strip())["delta"]
        for line in events.splitlines()
        if line.startswith("data:") and '"delta"' in line
    )
    assert deltas == decision
    assert '"status": "completed"' in events


def test_logical_id_invariant_holds_with_tool(client: TestClient) -> None:
    # Extending M5: an engine name in the model binding is still rejected up front, even on a
    # graph that also has tool/router nodes.
    doc = agent_ir()
    doc["models"]["default"]["model"] = "mlx"
    resp = _post(client, doc)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "engine_name_not_allowed"
