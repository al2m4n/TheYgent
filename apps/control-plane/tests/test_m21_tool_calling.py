"""M21 Phase 4 — the autonomous tool-calling loop (interactive walker).

An ``llm`` node with ``tools`` runs a bounded loop: the model emits a ``tool_call``, the walker
executes it via the EXISTING M6/M7/M19 executors, feeds the ``{role:tool}`` result back, and calls
the model again until it answers or ``maxToolIterations`` is hit. Driven by the fake upstream's
``tool_call`` / ``tool_loop`` modes (scripted: tool_call on turn 1, answer once a tool result is in
the transcript) — so this proves the loop end-to-end without a real engine. The real-engine gate is
``test_integration_m21.py`` (Phase 5).
"""

from __future__ import annotations

import json

from _fake_inference import FakeInference
from _ir import llm_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app


def _tool_graph(*, tools: list[str], bindings: dict, max_iter: int = 4) -> dict:
    ir = llm_ir("$in")  # input → llm → output
    ir["tools"] = bindings
    ir["nodes"][1]["config"]["tools"] = tools
    ir["nodes"][1]["config"]["maxToolIterations"] = max_iter
    return ir


_ECHO = {"echo": {"kind": "builtin", "ref": "echo"}}


def _run(client: TestClient, ir: dict, *, input_: str = "do the thing") -> dict:
    return client.post("/graphs/runs", json={"ir": ir, "input": input_, "stream": False}).json()


def test_llm_calls_a_builtin_tool_then_answers(pg_url: str) -> None:
    # The headline: the model decides to call `echo`, the walker runs it, feeds the result back, and
    # the model produces a final answer — all in one graph node.
    with FakeInference(
        mode="tool_call", tool_name="echo", tool_args={"value": "hi"}, response="answer after tool"
    ) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = _run(client, _tool_graph(tools=["echo"], bindings=_ECHO))
    assert body["status"] == "completed"
    assert body["output"] == "answer after tool"
    # The loop fed the tool result back: the second turn's request carried a {role:tool} message.
    msgs = server.captured["messages"]
    assert any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs)
    assert any(
        isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls") for m in msgs
    )


def test_tool_call_streams_a_tool_call_event_then_the_answer(pg_url: str) -> None:
    with FakeInference(
        mode="tool_call", tool_name="echo", tool_args={"value": "hi"}, response="streamed answer"
    ) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/graphs/runs",
                json={
                    "ir": _tool_graph(tools=["echo"], bindings=_ECHO),
                    "input": "go",
                    "stream": True,
                },
            ) as resp:
                text = "".join(resp.iter_text())
    events: list[tuple[str | None, str]] = []
    ev: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            ev = line[len("event:") :].strip()
        elif line.startswith("data:"):
            events.append((ev, line[len("data:") :].strip()))
            ev = None
    # A tool_call progress frame names the tool, then the answer streams as deltas.
    tool_calls = [json.loads(d)["toolCall"] for e, d in events if e == "tool_call"]
    deltas = [json.loads(d)["delta"] for e, d in events if e == "delta"]
    assert tool_calls == ["echo"]
    assert "".join(deltas) == "streamed answer"
    assert [json.loads(d) for e, d in events if e == "run"][-1]["status"] == "completed"


def test_unknown_tool_call_is_fed_back_not_fatal(pg_url: str) -> None:
    # The model hallucinates a tool not in the graph: the loop feeds an honest "unknown tool" result
    # back (never a crash), and the model recovers and answers.
    with FakeInference(
        mode="tool_call", tool_name="ghost", tool_args={}, response="recovered answer"
    ) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = _run(client, _tool_graph(tools=["echo"], bindings=_ECHO))
    assert body["status"] == "completed"
    assert body["output"] == "recovered answer"


def test_tool_loop_hits_the_iteration_cap_honestly(pg_url: str) -> None:
    # A model that never stops calling tools is bounded by maxToolIterations and finishes honestly
    # (completed, but with a non-null error reason — not a green blank).
    with FakeInference(mode="tool_loop", tool_name="echo", tool_args={"value": "x"}) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = _run(client, _tool_graph(tools=["echo"], bindings=_ECHO, max_iter=2))
            # error is persisted (the non-stream body omits it) → read the run.
            persisted = client.get(f"/runs/{body['runId']}").json()
    assert body["status"] == "completed"
    assert persisted.get("error")  # honest reason, not a silent green blank


def test_plain_llm_with_empty_tools_is_unchanged(pg_url: str) -> None:
    # A tools-less llm (empty tools) is the pre-M21 single-shot path — no tool_choice sent, one
    # turn.
    with FakeInference(response="just an answer") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = _run(client, _tool_graph(tools=[], bindings={}))
    assert body["status"] == "completed"
    assert body["output"] == "just an answer"
