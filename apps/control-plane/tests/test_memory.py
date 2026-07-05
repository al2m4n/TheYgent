"""Thread-level conversational memory — mechanical replay, proven against
real Postgres + real Alembic schema.

Memory here is deliberately NOT smart: store the turns, replay them verbatim on the next
run in the thread. These tests pin exactly that — the prior turns reach the inference wire
in order, the user+assistant pair lands atomically on success, a failed run contributes
no turn, and position gives deterministic ordering across runs.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from _db import count_messages, fetch_messages
from _fake_inference import FULL_MESSAGE, FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from ulid import ULID


def _one_shot(client: TestClient, thread_id: str, text: str) -> dict:
    resp = client.post(
        "/runs",
        json={"input": text, "model": "triage-fast", "stream": False, "thread_id": thread_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_thread_replay_sends_prior_turns(client: TestClient, fake_inference: FakeInference) -> None:
    # THE core proof memory works: run #2 in the same thread replays run #1's turns.
    thread_id = str(ULID())
    r1 = _one_shot(client, thread_id, "first question")
    assert r1["status"] == "completed"

    r2 = _one_shot(client, thread_id, "second question")
    assert r2["status"] == "completed"

    # The fake captured run #2's request: prior user+assistant turns (in order) prepended
    # to the new input. This is naive full replay — no summarization/truncation.
    assert fake_inference.captured["messages"] == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": FULL_MESSAGE},
        {"role": "user", "content": "second question"},
    ]


def test_atomic_pair_write(client: TestClient, pg_url: str) -> None:
    # On success the user+assistant pair lands together, at the next two positions.
    thread_id = str(ULID())
    _one_shot(client, thread_id, "hello")
    assert asyncio.run(fetch_messages(pg_url, thread_id)) == [
        ("user", "hello", 0),
        ("assistant", FULL_MESSAGE, 1),
    ]


def test_ordering_is_deterministic_across_runs(client: TestClient, pg_url: str) -> None:
    # position (not a timestamp) gives a stable replay order across multiple runs.
    thread_id = str(ULID())
    for text in ("q1", "q2", "q3"):
        _one_shot(client, thread_id, text)

    rows = asyncio.run(fetch_messages(pg_url, thread_id))
    assert [p for _, _, p in rows] == [0, 1, 2, 3, 4, 5]
    assert [r for r, _, _ in rows] == ["user", "assistant"] * 3
    assert [c for r, c, _ in rows if r == "user"] == ["q1", "q2", "q3"]


async def test_concurrent_same_thread_runs_do_not_collide(
    fake_inference: FakeInference, pg_url: str
) -> None:
    # THE race shared Postgres state introduced (a single-instance dict could not have it):
    # N runs in ONE thread completing near-simultaneously each compute next-position from
    # max(position)+1. append_turn serializes them with SELECT … FOR UPDATE on the thread
    # row, and the unique (thread_id, position) index is the loud backstop. Prove that all
    # N complete AND every turn lands at a distinct, dense position — no collision, no loss.
    n = 5
    thread_id = str(ULID())
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    body = {"input": "hi", "model": "triage-fast", "stream": False, "thread_id": thread_id}
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            results = await asyncio.gather(*(ac.post("/runs", json=body) for _ in range(n)))

    assert all(r.status_code == 200 and r.json()["status"] == "completed" for r in results)
    rows = await fetch_messages(pg_url, thread_id)
    # 2 turns/run, all positions distinct and dense — no duplicate, no gap, none lost.
    positions = [p for _, _, p in rows]
    assert positions == list(range(2 * n))
    assert len(set(positions)) == 2 * n
    # Each run contributed exactly one user+assistant pair (no half-write under the race).
    assert [role for role, _, _ in rows] == ["user", "assistant"] * n


def test_failed_run_writes_no_turns(pg_url: str) -> None:
    # A failed (pre-stream) run contributes no turn — the thread never holds a dangling
    # user turn with no answer. The run row still records the failure.
    thread_id = str(ULID())
    with FakeInference(mode="error_503") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            resp = client.post(
                "/runs",
                json={
                    "input": "q",
                    "model": "triage-fast",
                    "stream": False,
                    "thread_id": thread_id,
                },
            )
            assert resp.status_code == 503
            run_id = resp.json()["runId"]
            assert client.get(f"/runs/{run_id}").json()["status"] == "failed"

    assert asyncio.run(count_messages(pg_url, thread_id)) == 0


def test_failed_stream_writes_no_turns(pg_url: str) -> None:
    # Same invariant on the streaming path: a mid-stream drop after a 200 leaves the run
    # failed and the thread empty (no half-written turn).
    thread_id = str(ULID())
    with FakeInference(mode="drop_midstream") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/runs",
                json={
                    "input": "q",
                    "model": "triage-fast",
                    "stream": True,
                    "thread_id": thread_id,
                },
            ) as resp:
                assert resp.status_code == 200
                run_id = None
                for line in "".join(resp.iter_text()).splitlines():
                    if line.startswith("data:"):
                        payload = line[len("data:") :].strip()
                        if payload != "[DONE]":
                            run_id = json.loads(payload).get("runId", run_id)
            assert run_id is not None
            assert client.get(f"/runs/{run_id}").json()["status"] == "failed"

    assert asyncio.run(count_messages(pg_url, thread_id)) == 0
