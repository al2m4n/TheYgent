"""Fast suite for multi-input: a node may declare more than one named in-port.

A node may declare more than one named in-port, each fed by a distinct upstream ``data`` edge,
addressed in templates as ``$in.<port>``. That is what lets ONE ``llm`` node compose file content
AND a question in the same prompt — the expressiveness gap that bare ``$in`` could not express.
Resolution is PORT-FIRST: the segment after ``$in.`` is a port name, with a LOUD error on a miss
(the same "loud failure on a miss" philosophy applied to ports).

Everything real except the model (fake inference) over a real testcontainers Postgres.
Validation-only behaviours (dup edge / unfed required port / undeclared port) are pinned as pure IR
units in ``packages/ir/tests/test_graph.py``; here we prove the endpoint maps one to 400 invalid_ir.
"""

from __future__ import annotations

import logging

from _fake_inference import FakeInference
from _ir import (
    llm_ir,
    output_multi_inport_ir,
    router_multi_inport_ir,
    two_input_llm_ir,
)
from fastapi.testclient import TestClient
from theygent_control_plane.walker import _PortInputs, _render_template, _resolve_ref


def _run(client: TestClient, ir: dict, *, input_: str = "go") -> dict:
    return client.post("/graphs/runs", json={"ir": ir, "input": input_, "stream": False}).json()


def _ports(values: dict, declared: list[str] | None = None) -> _PortInputs:
    """A node's in-port map for the resolver-level unit tests: ``declared`` defaults to the fed
    ports, but can be passed explicitly to model a *declared-but-unfed* optional port."""
    return _PortInputs(
        values=values, declared=frozenset(declared if declared is not None else values)
    )


def test_two_input_composition_is_the_f2_1_proof(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # THE headline: two upstream nodes feed the `file` and `question` in-ports of one llm node, and
    # the rendered prompt contains BOTH values in the right places.
    ir = two_input_llm_ir(
        "Using the file:\n$in.file\n\nAnswer: $in.question",
        file_value="the moon is made of cheese",
        question_value="what is the moon made of?",
    )
    body = _run(client, ir)
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {
            "role": "user",
            "content": "Using the file:\nthe moon is made of cheese\n\n"
            "Answer: what is the moon made of?",
        }
    ]


def test_port_field_drills_into_object_valued_port(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # $in.<port>.<field> combines port selection with field drilling: the `file` port carries an
    # object, and the prompt selects a field of it while also reading the `question` port.
    ir = two_input_llm_ir(
        "Title: $in.file.title\nQ: $in.question",
        file_value={"title": "War and Peace", "pages": 1225},
        question_value="who wrote it?",
    )
    body = _run(client, ir)
    assert body["status"] == "completed"
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "Title: War and Peace\nQ: who wrote it?"}
    ]


def test_unknown_port_is_a_precise_error_no_green_run(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # Port-first miss: $in.nope names no declared in-port → a precise error naming the node, the bad
    # token, and the available ports — never a silent literal, never a green run.
    ir = two_input_llm_ir("$in.nope", file_value="x", question_value="y")
    body = _run(client, ir)
    assert body["error"]["code"] == "template_error"
    msg = body["error"]["message"]
    assert "n_llm" in msg
    assert "no in-port named 'nope'" in msg
    assert "file" in msg and "question" in msg  # available ports listed
    assert fake_inference.captured["model"] is None  # nothing reached the inference wire


def test_bare_in_on_multiport_node_with_no_in_port_errors(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # Bare $in is sugar for a default port named `in`; this llm declares [file, question] and no
    # `in`, so bare $in is a loud error naming the ports — no silent default.
    ir = two_input_llm_ir("$in", file_value="x", question_value="y")
    body = _run(client, ir)
    assert body["error"]["code"] == "template_error"
    msg = body["error"]["message"]
    assert "bare $in" in msg
    assert "file" in msg and "question" in msg
    assert fake_inference.captured["model"] is None


def test_multi_input_node_executes_exactly_once(
    client: TestClient, fake_inference: FakeInference, caplog
) -> None:
    # Topological-order regression: a node with two in-ports executes ONCE, after both producers —
    # multi-input is one node waiting on several producers, not concurrency.
    ir = two_input_llm_ir("$in.file / $in.question", file_value="a", question_value="b")
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _run(client, ir)
    assert body["status"] == "completed"
    ran = [
        r
        for r in caplog.records
        if r.getMessage() == "graph.node"
        and getattr(r, "node_id", None) == "n_llm"
        and getattr(r, "run_id", None) == body["runId"]
        and not getattr(r, "skipped", False)
    ]
    assert len(ran) == 1


def test_duplicate_edge_to_one_in_port_is_rejected_at_validation(client: TestClient) -> None:
    # Two data edges into one in-port is an ambiguous binding → 400 invalid_ir, no Run.
    ir = two_input_llm_ir("$in.file $in.question", file_value="a", question_value="b")
    ir["edges"].append(
        {
            "id": "e6",
            "source": "n_q",
            "sourceHandle": "ok",
            "target": "n_llm",
            "targetHandle": "file",  # a SECOND edge into the `file` port
            "channel": "data",
        }
    )
    resp = client.post("/graphs/runs", json={"ir": ir, "input": "go", "stream": False})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_ir"
    assert "ambiguous multi-input binding" in resp.json()["error"]["message"]
    assert "runId" not in resp.json()


# ── #2: single-value consumers (output / router) must take exactly one in-port ──
# The sharpest seam: "consume one value" meets "a node could be wired to many ports". A multi-port
# output/router must error LOUDLY (the unknown-port philosophy), never silently pick a default port.


def test_output_with_two_in_ports_errors_loudly(client: TestClient) -> None:
    # An output node wired to two ports is ambiguous — which value is the run output? The walker
    # rejects it with a named error, never a silent pick. Validation passes (each port fed once),
    # so this is a clean walk-time failure, not a 400.
    body = _run(client, output_multi_inport_ir())
    assert body.get("status") != "completed"
    assert body["error"]["code"] == "template_error"
    msg = body["error"]["message"]
    assert "n_out" in msg
    assert "single-value consumer" in msg
    assert "'a'" in msg and "'b'" in msg  # both ambiguous ports named


def test_router_with_two_in_ports_errors_loudly(client: TestClient) -> None:
    # Same rule for a router: its `select` may *address* a port ($in.a.handle resolves fine), but
    # the single value it forwards along the branch is ambiguous with two in-ports → loud error.
    body = _run(client, router_multi_inport_ir({"handle": "yes"}))
    assert body.get("status") != "completed"
    assert body["error"]["code"] == "template_error"
    msg = body["error"]["message"]
    assert "n_route" in msg
    assert "single-value consumer" in msg


# ── #3: absent (optional / fed-null) in-port resolver semantics ──
# An ABSENT port value (declared-but-unfed optional, or fed-with-null) resolves to the canonical
# absent value, rendered PER SITE — "" inline, JSON null in structured args. Drilling
# SHORT-CIRCUITS to absent (no error); fed-with-null collapses to the same. Pinned here so agents
# can lean on it. (Undeclared ports and missing fields on a PRESENT value still error loudly —
# see the unknown-port / drill-miss tests.)


def test_absent_port_resolves_to_null_in_structured_args() -> None:
    # $in.<optional_port> as a tool/mcp_tool/router arg resolves to JSON null (Python None) —
    # honestly absent, not "" masquerading as a provided value.
    assert _resolve_ref("$in.opt", _ports({}, declared=["opt"]), "n") is None
    # Bare $in on a declared-but-unfed default `in` port is absent too ($in == $in.in sugar).
    assert _resolve_ref("$in", _ports({}, declared=["in"]), "n") is None


def test_absent_port_renders_to_empty_string_inline() -> None:
    # Inline in an llm template, the same absent value stringifies to "" (the prompt simply
    # contains nothing where the token was) — the per-site rendering split.
    assert _render_template("note=[$in.opt]", _ports({}, declared=["opt"]), "n") == "note=[]"


def test_drilling_into_absent_port_short_circuits_to_absent() -> None:
    # $in.<port>.<path> on an absent (unfed optional) port resolves to absent — it does NOT
    # error on the missing path. Optional absence is author-sanctioned, not silent nonsense.
    assert _resolve_ref("$in.opt.deep.field", _ports({}, declared=["opt"]), "n") is None
    assert _render_template("x=[$in.opt.deep]", _ports({}, declared=["opt"]), "n") == "x=[]"


def test_fed_with_null_collapses_to_absent() -> None:
    # A port fed with an explicit null is indistinguishable from an unfed optional port — both
    # the whole value AND a drill into it resolve to absent, byte-identical (locks the collapse).
    fed_null = _ports({"p": None})
    unfed = _ports({}, declared=["p"])
    assert _resolve_ref("$in.p", fed_null, "n") == _resolve_ref("$in.p", unfed, "n")  # both None
    assert _resolve_ref("$in.p.field", fed_null, "n") == _resolve_ref("$in.p.field", unfed, "n")
    assert _render_template("v=[$in.p.x]", fed_null, "n") == _render_template(
        "v=[$in.p.x]", unfed, "n"
    )


# ── structured (object) run input — the gap the real path caught ──
# A multi-input agent's run input is naturally an OBJECT the graph drills with $in.in.<field>;
# /graphs/runs accepts it (the request `input` is Any, not str — a deliberate contract extension).


def test_object_run_input_flows_through_graphs_runs(
    client: TestClient, fake_inference: FakeInference
) -> None:
    body = client.post(
        "/graphs/runs",
        json={
            "ir": llm_ir("path=$in.in.path q=$in.in.q"),
            "input": {"path": "/etc/hosts", "q": "why"},
            "stream": False,
        },
    ).json()
    assert body["status"] == "completed"
    # The object reached the input node, and the llm drilled both fields out of it.
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "path=/etc/hosts q=why"}
    ]
