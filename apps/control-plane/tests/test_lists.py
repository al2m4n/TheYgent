"""Read-only list endpoints for the cockpit.

Thin, paginated, newest-first lists over
already-persisted runs and sessions, plus a session-detail read. The session list/detail
adds NO aggregation/summary endpoint — it only surfaces state the write paths persist.
Proven against the same real Postgres + real Alembic schema as the rest of the fast suite.
"""

from __future__ import annotations

from _fake_inference import FULL_MESSAGE
from _ir import trivial_ir
from fastapi.testclient import TestClient
from ulid import ULID


def _run(client: TestClient, *, session_id: str | None = None, text: str = "hi") -> dict:
    body: dict = {"input": text, "model": "triage-fast", "stream": False}
    if session_id is not None:
        body["session_id"] = session_id
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


def test_list_sessions_summary(client: TestClient) -> None:
    session_id = str(ULID())
    _run(client, session_id=session_id, text="first user message")
    _run(client, session_id=session_id, text="follow up")

    sessions = client.get("/sessions").json()["sessions"]
    assert len(sessions) == 1
    t = sessions[0]
    assert t["id"] == session_id
    # Two runs, user+assistant each = 4 messages; preview is the first user turn (position 0).
    assert t["message_count"] == 4
    assert t["preview"] == "first user message"
    assert t["last_activity"] is not None


def test_list_sessions_excludes_one_shot_runs(client: TestClient) -> None:
    # A run with no session_id never creates a session row — it must not appear here.
    _run(client)
    assert client.get("/sessions").json()["sessions"] == []


def test_session_detail_messages_in_order(client: TestClient) -> None:
    session_id = str(ULID())
    _run(client, session_id=session_id, text="hello")
    _run(client, session_id=session_id, text="again")

    detail = client.get(f"/sessions/{session_id}").json()
    assert detail["id"] == session_id
    msgs = detail["messages"]
    assert [(m["role"], m["content"], m["position"]) for m in msgs] == [
        ("user", "hello", 0),
        ("assistant", FULL_MESSAGE, 1),
        ("user", "again", 2),
        ("assistant", FULL_MESSAGE, 3),
    ]
    # Each message carries the run that produced it.
    assert all(m["run_id"] for m in msgs)


def test_session_detail_unknown_404(client: TestClient) -> None:
    resp = client.get("/sessions/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "session_not_found"


def test_stats_exact_totals(client: TestClient) -> None:
    # The dashboard overview needs exact totals, not the paginated list window.
    assert client.get("/stats").json() == {"runs": 0, "sessions": 0, "agents": 0}

    # Two one-shot runs (no session) + two runs sharing one session → 4 runs, 1 session row.
    _run(client)
    _run(client)
    sid = str(ULID())
    _run(client, session_id=sid, text="first")
    _run(client, session_id=sid, text="second")

    # One saved agent.
    assert client.post("/agents", json={"ir": trivial_ir()}).status_code == 201

    assert client.get("/stats").json() == {"runs": 4, "sessions": 1, "agents": 1}


def test_cors_dev_origin_allowed(client: TestClient) -> None:
    # The cockpit SPA (Vite dev origin) must be allowed.
    resp = client.get("/runs", headers={"Origin": "http://localhost:5173"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5173"


def test_cors_interface_dev_origin_allowed(client: TestClient) -> None:
    # The visual interface SPA runs on its own Vite dev origin (:5174) and also calls the
    # control-plane directly (load/save agents) — its origin must be allowed too.
    resp = client.get("/runs", headers={"Origin": "http://localhost:5174"})
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:5174"
