"""Observability fast suite — the waterfall API, capture policy, authz chokepoint, the
two-sink wiring, and worker attribution, over real PG + the fake inference + the in-process walker.

Field names are **snake_case**, matching the existing theygent API convention (``/runs`` is too) and
the ``SpanView`` dump. The durable-path span/worker-attribution + resume idempotency live in
``test_observability_durable.py`` (DBOS) and the env-gated integration test.
"""

from __future__ import annotations

import json

from _fake_inference import FakeInference
from _ir import llm_ir, trivial_ir, two_input_llm_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_control_plane.observability import Span, build_otlp_sink


def _graph_run(client: TestClient, ir: dict, input_value: str = "hi") -> str:
    r = client.post("/graphs/runs", json={"ir": ir, "input": input_value, "stream": False})
    assert r.status_code == 200, r.text
    return r.json()["runId"]


# ── the span tree (shape, gap derivable, phase attribution, worker attribution) ──


def test_trace_is_a_span_tree_with_one_node_span_per_node(client: TestClient) -> None:
    run_id = _graph_run(client, trivial_ir())
    trace = client.get(f"/runs/{run_id}/trace")
    assert trace.status_code == 200, trace.text
    spans = trace.json()["spans"]
    by_name = {s["name"]: s for s in spans}
    assert run_id in by_name  # the run-root span anchors t0
    for node_id in ("n_in", "n_llm", "n_out"):
        assert node_id in by_name, f"missing node span {node_id}"
    root = by_name[run_id]
    assert root["parent_span_id"] is None  # the root has no parent
    # NODE spans (phase is None) parent to the run root; PHASE spans parent to their node span.
    for s in (s for s in spans if s["node_id"] and s["phase"] is None):
        assert s["parent_span_id"] == root["span_id"]
    seqs = [s["seq"] for s in spans]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)  # strictly monotonic per run
    for s in spans:  # honest timing → durations + gaps are derivable from the data
        assert s["end_ns"] is not None and s["start_ns"] <= s["end_ns"]


def test_gap_between_nodes_is_derivable(client: TestClient) -> None:
    run_id = _graph_run(client, trivial_ir())
    spans = client.get(f"/runs/{run_id}/trace").json()["spans"]
    by = {s["name"]: s for s in spans}
    gap = by["n_llm"]["start_ns"] - by["n_in"]["end_ns"]
    assert gap >= 0  # the between-step timing, derived purely from honest span timestamps


def test_model_generate_phase_span_sits_inside_its_llm_node(client: TestClient) -> None:
    run_id = _graph_run(client, trivial_ir())
    spans = client.get(f"/runs/{run_id}/trace").json()["spans"]
    node = next(s for s in spans if s["name"] == "n_llm")
    gen = next((s for s in spans if s["name"] == "model.generate"), None)
    assert gen is not None, "expected a model.generate phase span under the llm node"
    assert gen["parent_span_id"] == node["span_id"]  # child of its node span
    assert (
        node["start_ns"] <= gen["start_ns"] and gen["end_ns"] <= node["end_ns"]
    )  # window ⊆ parent


def test_interactive_spans_carry_worker_attribution(client: TestClient) -> None:
    run_id = _graph_run(client, trivial_ir())
    spans = client.get(f"/runs/{run_id}/trace").json()["spans"]
    for s in spans:
        assert s["executor_id"] == "inproc"  # the interactive walker's worker id
        assert s["worker_host"]  # host:pid stamped


# ── node I/O capture (post-$in, multi-input, lazy) ──


def test_node_io_full_capture_records_payloads(client: TestClient) -> None:
    run_id = _graph_run(client, trivial_ir(), input_value="summarize this")
    body = client.get(f"/runs/{run_id}/nodes/n_llm/io").json()
    assert body["capture_level"] == "full"
    assert body["inputs"] is not None and body["outputs"] is not None
    assert body["bytes_in"] > 0 and body["bytes_out"] > 0
    assert body["reason"] is None


def test_node_io_inputs_are_post_substitution(client: TestClient) -> None:
    run_id = _graph_run(client, llm_ir("answer: $in"), input_value="the question")
    io = client.get(f"/runs/{run_id}/nodes/n_llm/io").json()
    assert io["inputs"]["in"] == "the question"  # the resolved (post-$in) in-port value


def test_multi_input_node_io_one_entry_per_port(client: TestClient) -> None:
    ir = two_input_llm_ir("file=$in.file q=$in.question", "FILE-BODY", "QUESTION-TEXT")
    run_id = _graph_run(client, ir, input_value="ignored")
    io = client.get(f"/runs/{run_id}/nodes/n_llm/io").json()
    assert set(io["inputs"]) == {"file", "question"}  # one entry per named in-port
    assert io["inputs"]["file"] == "FILE-BODY" and io["inputs"]["question"] == "QUESTION-TEXT"


def test_trace_carries_no_payloads_io_is_lazy(client: TestClient) -> None:
    run_id = _graph_run(client, trivial_ir(), input_value="secret prompt body")
    spans = client.get(f"/runs/{run_id}/trace").json()["spans"]
    assert "secret prompt body" not in json.dumps(spans)  # /trace is payload-free
    io = client.get(f"/runs/{run_id}/nodes/n_llm/io").json()
    assert io["inputs"]["in"] == "secret prompt body"  # …but /io returns it (lazy click-through)


def test_trace_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/runs/nope/trace").status_code == 404
    assert client.get("/runs/nope/nodes/x/io").status_code == 404


def test_prompt_run_is_traced_too(client: TestClient) -> None:
    # A plain /runs prompt run (no IR/walker — Compose's default "prompt" mode) is also a run, so it
    # gets a trace: a run-root span + one `llm` node span (the generation), with the prompt as input
    # and the answer as output. Without this it showed "No trace recorded for this run".
    r = client.post("/runs", json={"input": "what is 2+2", "model": "triage-fast", "stream": False})
    assert r.status_code == 200, r.text
    run_id = r.json()["runId"]
    spans = client.get(f"/runs/{run_id}/trace").json()["spans"]
    names = {s["name"] for s in spans}
    assert run_id in names and "llm" in names  # run-root + the single generation node
    llm = next(s for s in spans if s["node_id"] == "llm")
    assert llm["node_type"] == "llm" and llm["status"] == "ok"
    io = client.get(f"/runs/{run_id}/nodes/llm/io").json()
    assert io["inputs"]["prompt"] == "what is 2+2"  # the prompt, captured as input
    assert io["outputs"]["output"]  # the answer, captured as output


# ── live SSE ──


def test_trace_stream_done_on_terminal_run(client: TestClient) -> None:
    # A completed run's /trace/stream returns immediately with a done event (no hang) — the client
    # falls back to the static /trace snapshot. The live open/close growth is unit-tested at the
    # Telemetry/SpanBus level in test_observability_core.
    run_id = _graph_run(client, trivial_ir())
    with client.stream("GET", f"/runs/{run_id}/trace/stream") as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "event: done" in body


# ── two-sink wiring + redaction ──


def test_otlp_sink_off_by_default_on_when_configured() -> None:
    assert build_otlp_sink(endpoint=None) is None  # always-local default: only the PG sink
    sink = build_otlp_sink(endpoint="http://collector:4318", send=lambda sp, attrs: None)
    assert sink is not None  # configured → the OTLP sink is constructed


def test_otlp_redacts_configured_attr_while_local_keeps_it(monkeypatch) -> None:
    # The RedactingSpanProcessor: the span row keeps every attribute locally; the OTLP copy
    # has the sensitive scalar stripped.
    monkeypatch.setenv("THEYGENT_OTEL_REDACT_ATTRS", "gen_ai.prompt")
    seen: dict = {}
    sink = build_otlp_sink(endpoint="http://c:4318", send=lambda sp, attrs: seen.update(attrs))
    assert sink is not None
    span = Span(
        id="s",
        run_id="r",
        trace_id="t",
        span_id="sp",
        parent_span_id=None,
        name="n",
        attributes={"gen_ai.prompt": "secret", "gen_ai.request.model": "m"},
    )
    sink.export(span)
    assert seen["gen_ai.prompt"] == "[redacted]"  # stripped on the OTLP path
    assert seen["gen_ai.request.model"] == "m"  # non-sensitive scalar passes
    assert span.attributes["gen_ai.prompt"] == "secret"  # the local span keeps it


# ── per-agent capture policy (off/metadata/full, precedence, hash unchanged) + authz ──


def _save_agent(client: TestClient, ir: dict) -> str:
    r = client.post("/agents", json={"ir": ir})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _agent_run(client: TestClient, agent_id: str, input_value: str = "hi") -> str:
    r = client.post(f"/agents/{agent_id}/runs", json={"input": input_value, "stream": False})
    assert r.status_code == 200, r.text
    return r.json()["runId"]


def test_io_policy_off_writes_no_payloads_but_spans_still_land(client: TestClient) -> None:
    agent_id = _save_agent(client, trivial_ir())
    assert (
        client.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "off"}).status_code == 200
    )
    run_id = _agent_run(client, agent_id)
    spans = client.get(f"/runs/{run_id}/trace").json()["spans"]
    assert any(
        s["node_id"] == "n_llm" for s in spans
    )  # timing span still written when capture is off
    io = client.get(f"/runs/{run_id}/nodes/n_llm/io").json()
    assert io["capture_level"] == "off" and io["inputs"] is None
    assert "disabled" in io["reason"].lower()


def test_io_policy_metadata_writes_sizes_only(client: TestClient) -> None:
    agent_id = _save_agent(client, trivial_ir())
    client.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "metadata"})
    run_id = _agent_run(client, agent_id, input_value="some prompt")
    io = client.get(f"/runs/{run_id}/nodes/n_llm/io").json()
    assert io["capture_level"] == "metadata"
    assert io["inputs"] is None and io["outputs"] is None  # no payloads
    assert io["bytes_in"] > 0  # sizes ARE recorded
    assert "sizes only" in (io["reason"] or "").lower()


def test_io_policy_full_writes_payloads(client: TestClient) -> None:
    agent_id = _save_agent(client, trivial_ir())
    client.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "full"})
    run_id = _agent_run(client, agent_id, input_value="capture me")
    io = client.get(f"/runs/{run_id}/nodes/n_llm/io").json()
    assert io["capture_level"] == "full" and io["inputs"]["in"] == "capture me"


def test_io_policy_metadata_ceiling_caps_an_agent_requesting_full(
    fake_inference: FakeInference, pg_url: str, monkeypatch
) -> None:
    # A metadata DEPLOYMENT CEILING (env var) caps an agent that requested full → effective
    # metadata, capped. The ceiling is read at Telemetry construction, so set it BEFORE the app.
    monkeypatch.setenv("THEYGENT_IO_CAPTURE", "metadata")
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app) as c:
        agent_id = _save_agent(c, trivial_ir())
        c.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "full"})
        pol = c.get(f"/agents/{agent_id}/io-policy").json()
        assert pol["io_capture"] == "full"  # the stored request
        assert pol["effective"] == "metadata" and pol["capped"] is True  # the real behavior
        # And the run actually captures metadata, not full.
        run_id = _agent_run(c, agent_id, input_value="x")
        io = c.get(f"/runs/{run_id}/nodes/n_llm/io").json()
        assert io["capture_level"] == "metadata" and io["inputs"] is None


def test_io_policy_edit_does_not_change_content_hash(client: TestClient) -> None:
    agent_id = _save_agent(client, trivial_ir())
    before = [v["content_hash"] for v in client.get(f"/agents/{agent_id}").json()["versions"]]
    client.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "off"})
    after = client.get(f"/agents/{agent_id}").json()
    assert [v["content_hash"] for v in after["versions"]] == before  # policy not in the hashed IR
    assert len(after["versions"]) == len(before)  # no new version minted


def test_io_policy_unknown_agent_is_404(client: TestClient) -> None:
    assert client.get("/agents/nope/io-policy").status_code == 404
    assert client.put("/agents/nope/io-policy", json={"io_capture": "off"}).status_code == 404


def test_authz_denial_gates_payloads_not_the_timeline(client: TestClient, monkeypatch) -> None:
    # With io:read denied (but trace:read allowed), /io returns the no-payload shape + a reason (not
    # a 500/403), while /trace still renders — the "captured but not visible to you" state.
    run_id = _graph_run(client, trivial_ir(), input_value="private")

    def fake_authorize(principal, permission, resource):
        return permission != "io:read"

    monkeypatch.setattr("theygent_control_plane.app.authorize", fake_authorize)
    trace = client.get(f"/runs/{run_id}/trace")
    assert trace.status_code == 200 and trace.json()["spans"]  # the waterfall stays legible
    io = client.get(f"/runs/{run_id}/nodes/n_llm/io")
    assert io.status_code == 200  # NOT a 500/403 — the timeline must not break
    body = io.json()
    assert body["inputs"] is None and "access" in (body["reason"] or "").lower()
