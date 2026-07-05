"""Characterization/golden baseline for the interactive ``llm`` path.

Before refactoring ``_walk_llm`` to delegate to the shared ``execute_llm`` body (so the tool
loop is written once, not twice), this pins the OBSERVABLE behavior of a graph
``llm`` node's streaming: the ``event: reasoning`` / ``event: delta`` SSE sequence, content
reassembly across chunks, the persisted output (answer only — thinking never folded in), and the
honest truncated-empty (``finish_reason: length``) reason. The refactor must keep every assertion
here green (plus the whole llm suite) — that is what makes it "byte-identical".

The streaming relay frames are: ``event: run`` (status: streaming) → ``event: reasoning`` /
``event: delta`` token frames → ``event: run`` (status: completed) → ``data: [DONE]``. The final
output/error are PERSISTED (read via ``GET /runs/{id}``), not carried on the SSE completed frame.
"""

from __future__ import annotations

import json

from _fake_inference import FULL_MESSAGE, FakeInference
from _ir import llm_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app


def _events(text: str) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    event: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event = line[len("event:") :].strip()
        elif line.startswith("data:"):
            out.append((event, line[len("data:") :].strip()))
            event = None
    return out


def _stream_graph(client: TestClient, *, input_: str = "hi") -> list[tuple[str | None, str]]:
    with client.stream(
        "POST", "/graphs/runs", json={"ir": llm_ir("$in"), "input": input_, "stream": True}
    ) as resp:
        return _events("".join(resp.iter_text()))


def _run_frames(events: list[tuple[str | None, str]]) -> list[dict]:
    return [json.loads(d) for ev, d in events if ev == "run"]


def _reasoning(events: list[tuple[str | None, str]]) -> list[str]:
    return [json.loads(d)["reasoning"] for ev, d in events if ev == "reasoning"]


def _deltas(events: list[tuple[str | None, str]]) -> list[str]:
    return [json.loads(d)["delta"] for ev, d in events if ev == "delta"]


def test_graph_llm_reasoning_then_answer_sse_sequence(pg_url: str) -> None:
    # The golden case: a reasoning model's thinking relays as `event: reasoning`, the answer as
    # `event: delta`, in order; the persisted output is the ANSWER ONLY (thinking not folded in).
    with FakeInference(mode="reasoning") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            events = _stream_graph(client)
            run_id = _run_frames(events)[0]["runId"]
            persisted = client.get(f"/runs/{run_id}").json()
    assert "".join(_reasoning(events)) == "thinking step..."  # thinking surfaced, in order
    assert "".join(_deltas(events)) == FULL_MESSAGE  # answer reassembled from its chunks
    # all reasoning frames precede the first delta frame (thinking before answering).
    assert [ev for ev, _ in events if ev in ("reasoning", "delta")] == [
        "reasoning",
        "reasoning",
        "delta",
    ]
    assert _run_frames(events)[-1]["status"] == "completed"
    assert persisted["output"] == FULL_MESSAGE  # the answer only


def test_graph_llm_default_content_reassembled(pg_url: str) -> None:
    # The plain path: content streams in two chunks and reassembles to the full answer.
    with FakeInference() as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            events = _stream_graph(client)
    deltas = _deltas(events)
    assert len(deltas) >= 2  # streamed in pieces (proves reassembly, not one blob)
    assert "".join(deltas) == FULL_MESSAGE
    assert _run_frames(events)[-1]["status"] == "completed"


def test_graph_llm_truncated_empty_is_honest(pg_url: str) -> None:
    # Budget exhausted by thinking (finish_reason: length, empty content): no answer delta, and the
    # run is legible — completed with an honest non-null error reason persisted, not a green blank.
    with FakeInference(mode="empty_length") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            events = _stream_graph(client)
            run_id = _run_frames(events)[0]["runId"]
            persisted = client.get(f"/runs/{run_id}").json()
    assert _deltas(events) == []  # no answer streamed
    assert _run_frames(events)[-1]["status"] == "completed"
    assert persisted.get("error")  # an honest reason, not None


def test_graph_llm_nonstream_binds_output(pg_url: str) -> None:
    # The non-stream path (the durable / future tool-loop path is non-stream): the run's output is
    # the llm's answer bound through the output node.
    with FakeInference() as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = client.post(
                "/graphs/runs", json={"ir": llm_ir("$in"), "input": "hi", "stream": False}
            ).json()
    assert body["status"] == "completed"
    assert body["output"] == FULL_MESSAGE
