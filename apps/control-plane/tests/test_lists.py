"""Read-only list endpoints for the cockpit (M8 §1.1/§1.3/§2).

The one sanctioned cockpit-driven backend addition: thin, paginated, newest-first lists over
already-persisted runs and threads, plus a thread-detail read. They add NO write path and NO
aggregation/summary endpoint (§6) — they only surface state the run path already persists.
Proven against the same real Postgres + real Alembic schema as the rest of the fast suite.
"""

from __future__ import annotations

from _fake_inference import FULL_MESSAGE
from fastapi.testclient import TestClient
from ulid import ULID


def _run(client: TestClient, *, thread_id: str | None = None, text: str = "hi") -> dict:
    body: dict = {"input": text, "model": "triage-fast", "stream": False}
    if thread_id is not None:
        body["thread_id"] = thread_id
    resp = client.post("/runs", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_list_runs_empty(client: TestClient) -> None:
    assert client.get("/runs").json() == {"runs": []}


def test_list_runs_newest_first(client: TestClient) -> None:
    ids = [_run(client, text=f"q{i}")["runId"] for i in range(3)]
    runs = client.get("/runs").json()["runs"]
    # Newest first: reverse of creation order. The row shape is the GET /runs/{id} shape.
    assert [r["id"] for r in runs] == list(reversed(ids))
    assert runs[0]["status"] == "completed"
    assert runs[0]["model"] == "triage-fast"


def test_list_runs_limit_and_cursor(client: TestClient) -> None:
    ids = [_run(client, text=f"q{i}")["runId"] for i in range(5)]
    newest_first = list(reversed(ids))

    page1 = client.get("/runs", params={"limit": 2}).json()["runs"]
    assert [r["id"] for r in page1] == newest_first[:2]

    # Keyset cursor: rows strictly older than the last id on page 1.
    page2 = client.get("/runs", params={"limit": 2, "before": page1[-1]["id"]}).json()["runs"]
    assert [r["id"] for r in page2] == newest_first[2:4]


def test_list_runs_unknown_cursor_ignored(client: TestClient) -> None:
    ids = [_run(client, text=f"q{i}")["runId"] for i in range(2)]
    runs = client.get("/runs", params={"before": "nonexistent"}).json()["runs"]
    assert {r["id"] for r in runs} == set(ids)


def test_list_runs_limit_bounds(client: TestClient) -> None:
    assert client.get("/runs", params={"limit": 0}).status_code == 422
    assert client.get("/runs", params={"limit": 999}).status_code == 422


def test_list_threads_summary(client: TestClient) -> None:
    thread_id = str(ULID())
    _run(client, thread_id=thread_id, text="first user message")
    _run(client, thread_id=thread_id, text="follow up")

    threads = client.get("/threads").json()["threads"]
    assert len(threads) == 1
    t = threads[0]
    assert t["id"] == thread_id
    # Two runs, user+assistant each = 4 messages; preview is the first user turn (position 0).
    assert t["message_count"] == 4
    assert t["preview"] == "first user message"
    assert t["last_activity"] is not None


def test_list_threads_excludes_one_shot_runs(client: TestClient) -> None:
    # A run with no thread_id never creates a thread row (M4 §4) — it must not appear here.
    _run(client)
    assert client.get("/threads").json()["threads"] == []


def test_thread_detail_messages_in_order(client: TestClient) -> None:
    thread_id = str(ULID())
    _run(client, thread_id=thread_id, text="hello")
    _run(client, thread_id=thread_id, text="again")

    detail = client.get(f"/threads/{thread_id}").json()
    assert detail["id"] == thread_id
    msgs = detail["messages"]
    assert [(m["role"], m["content"], m["position"]) for m in msgs] == [
        ("user", "hello", 0),
        ("assistant", FULL_MESSAGE, 1),
        ("user", "again", 2),
        ("assistant", FULL_MESSAGE, 3),
    ]
    # Each message carries the run that produced it.
    assert all(m["run_id"] for m in msgs)


def test_thread_detail_unknown_404(client: TestClient) -> None:
    resp = client.get("/threads/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "thread_not_found"


def test_cors_dev_origin_allowed(client: TestClient) -> None:
    # The cockpit SPA (Vite dev origin) must be allowed (M8 §3.3/§6).
    resp = client.get("/runs", headers={"Origin": "http://localhost:5173"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"
