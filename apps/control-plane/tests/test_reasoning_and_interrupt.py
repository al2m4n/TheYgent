"""Fixes for the reasoning-model rough edges found in dogfooding.

A reasoning model (e.g. Qwen3.5-9B-OptiQ) emits a hidden ``reasoning_content`` block BEFORE its
``content`` answer. That produced three real symptoms in multi-turn use:
  * the stream looked frozen during the (long) thinking phase — the control-plane only relayed
    ``content`` deltas;
  * when the token budget was exhausted by thinking, ``content`` came back empty and the run
    completed GREEN with no answer;
  * an aborted stream left the run stuck at ``streaming`` until the next restart's reconcile sweep.

These tests cover the three fixes: reasoning is streamed as a distinct ``event: reasoning``; an
empty completion (``finish_reason: length``) is surfaced honestly; an interrupted stream is
terminalized in-process.
"""

from __future__ import annotations

import json

from _db import get_run_row
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


def test_reasoning_streamed_as_distinct_event(pg_url: str) -> None:
    # The model's thinking is relayed as `event: reasoning` (visible progress, not the answer),
    # and the answer still arrives as `event: delta`. So a thinking model never looks frozen.
    with FakeInference(mode="reasoning") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            with client.stream(
                "POST", "/runs", json={"input": "hi", "model": "triage-fast", "stream": True}
            ) as resp:
                events = _events("".join(resp.iter_text()))
    reasoning = [json.loads(d)["reasoning"] for ev, d in events if ev == "reasoning"]
    deltas = [json.loads(d)["delta"] for ev, d in events if ev == "delta"]
    assert "".join(reasoning) == "thinking step..."  # thinking surfaced
    assert "".join(deltas) == FULL_MESSAGE  # answer still the answer
    assert json.loads(events[-2][1])["status"] == "completed"


def test_reasoning_not_folded_into_output(pg_url: str) -> None:
    # The persisted output is the ANSWER only — the thinking is never mistaken for it.
    with FakeInference(mode="reasoning") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            run_id = client.post(
                "/runs", json={"input": "hi", "model": "triage-fast", "stream": False}
            ).json()["runId"]
            assert client.get(f"/runs/{run_id}").json()["output"] == FULL_MESSAGE


def test_empty_length_completion_is_honest_stream(pg_url: str) -> None:
    # Budget exhausted by thinking → empty content + finish_reason=length. The run completes but
    # carries an honest reason (not error:null) and appends NO session turn.
    with FakeInference(mode="empty_length") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/runs",
                json={"input": "hi", "model": "triage-fast", "stream": True, "session_id": "te"},
            ) as resp:
                events = _events("".join(resp.iter_text()))
            run_id = json.loads(events[0][1])["runId"]
            got = client.get(f"/runs/{run_id}").json()
            assert got["status"] == "completed"
            assert got["output"] == ""
            assert got["error"] is not None
            assert "maxTokens" in got["error"] and "length" in got["error"]
            # No blank assistant turn was stored for the empty answer.
            detail = client.get("/sessions/te").json()
            assert all(m["role"] != "assistant" or m["content"] for m in detail["messages"])


def test_empty_length_completion_is_honest_non_stream(pg_url: str) -> None:
    with FakeInference(mode="empty_length") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            run_id = client.post(
                "/runs", json={"input": "hi", "model": "triage-fast", "stream": False}
            ).json()["runId"]
            got = client.get(f"/runs/{run_id}").json()
            assert got["status"] == "completed" and got["error"] is not None


def test_graph_reasoning_streamed_and_empty_is_honest(pg_url: str) -> None:
    # The graph path makes the same split (Delta.kind) and the same honest empty signal via the
    # walker's empty_reason.
    with FakeInference(mode="reasoning") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/graphs/runs",
                json={"ir": llm_ir("$in"), "input": "hi", "stream": True},
            ) as resp:
                events = _events("".join(resp.iter_text()))
            assert any(ev == "reasoning" for ev, _ in events)
            assert "".join(json.loads(d)["delta"] for ev, d in events if ev == "delta") == (
                FULL_MESSAGE
            )

    with FakeInference(mode="empty_length") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            run_id = client.post(
                "/graphs/runs", json={"ir": llm_ir("$in"), "input": "hi", "stream": False}
            ).json()["runId"]
            got = client.get(f"/runs/{run_id}").json()
            assert got["status"] == "completed" and got["error"] is not None
            assert "n_llm" in got["error"]


def test_fail_if_active_terminalizes_streaming_run(pg_url: str) -> None:
    # The core of the interrupt fix: an aborted stream calls store.fail_if_active, which fails a
    # still-`streaming` run (the in-process equivalent of the startup reconcile sweep) so it never
    # lingers as a zombie. Race-safe: it touches ONLY non-terminal runs.
    import asyncio

    from _db import seed_run
    from theygent_control_plane import db
    from theygent_control_plane.store import RunStore
    from ulid import ULID

    async def run_case() -> None:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        store = RunStore()
        try:
            zombie = str(ULID())
            await seed_run(pg_url, zombie, "streaming")
            async with sm() as s, s.begin():
                changed = await store.fail_if_active(s, zombie, "interrupted: client disconnected")
            assert changed is True
            row = await get_run_row(pg_url, zombie)
            assert row is not None and row["status"] == "failed"
            assert "interrupted" in str(row["error"])

            # A completed run is left untouched — a late cancel can't clobber a real outcome.
            done = str(ULID())
            await seed_run(pg_url, done, "completed")
            async with sm() as s, s.begin():
                changed = await store.fail_if_active(s, done, "interrupted: client disconnected")
            assert changed is False
            row = await get_run_row(pg_url, done)
            assert row is not None and row["status"] == "completed"
        finally:
            await engine.dispose()

    asyncio.run(run_case())
