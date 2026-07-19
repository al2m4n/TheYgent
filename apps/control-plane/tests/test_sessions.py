"""Client-write session endpoints — a session a direct-to-inference chat persists itself.

The run paths keep writing their own turns; these endpoints are the second writer:
POST /sessions (idempotent ensure + whole-value metadata replace), POST /sessions/{id}/turns
(a user/assistant pair with run_id NULL, same positioning mechanics as run turns), and
DELETE /sessions/{id} (messages dropped, runs detached — never deleted). Proven against the
same real Postgres + real Alembic schema as the rest of the fast suite.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from _db import count_messages
from fastapi.testclient import TestClient
from ulid import ULID


def _run(client: TestClient, *, session_id: str, text: str = "hi") -> dict:
    resp = client.post(
        "/runs",
        json={"input": text, "model": "triage-fast", "stream": False, "session_id": session_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _turns(client: TestClient, session_id: str, user: str, assistant: str):
    return client.post(
        f"/sessions/{session_id}/turns",
        json={"user_content": user, "assistant_content": assistant},
    )


def test_create_session_mints_id_and_returns_summary(client: TestClient) -> None:
    resp = client.post("/sessions", json={"metadata": {"kind": "chat", "model": "triage-fast"}})
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"]  # server-minted (same idgen as runs)
    assert body["message_count"] == 0
    assert body["preview"] is None
    assert body["metadata"] == {"kind": "chat", "model": "triage-fast"}


def test_create_session_is_idempotent_and_replaces_metadata_whole(client: TestClient) -> None:
    sid = str(ULID())
    r1 = client.post("/sessions", json={"id": sid, "metadata": {"kind": "chat", "title": "a"}})
    assert r1.status_code == 201
    assert r1.json()["id"] == sid

    # Re-ensure without metadata: nothing changes (an ensure, not a clear).
    r2 = client.post("/sessions", json={"id": sid})
    assert r2.status_code == 201
    assert r2.json()["metadata"] == {"kind": "chat", "title": "a"}

    # With metadata: whole-value replace, never a merge — `title` is gone.
    r3 = client.post("/sessions", json={"id": sid, "metadata": {"kind": "bench.model"}})
    assert r3.status_code == 201
    assert r3.json()["metadata"] == {"kind": "bench.model"}
    assert client.get(f"/sessions/{sid}").json()["metadata"] == {"kind": "bench.model"}
    # Still exactly one session row.
    assert len(client.get("/sessions").json()["sessions"]) == 1


def test_append_turns_pair_lands_with_null_run_id(client: TestClient) -> None:
    sid = client.post("/sessions", json={}).json()["id"]
    r = _turns(client, sid, "q1", "a1")
    assert r.status_code == 201
    msgs = r.json()["messages"]
    assert [(m["role"], m["content"], m["position"], m["run_id"]) for m in msgs] == [
        ("user", "q1", 0, None),
        ("assistant", "a1", 1, None),
    ]
    # A second pair continues the dense position sequence.
    assert [m["position"] for m in _turns(client, sid, "q2", "a2").json()["messages"]] == [2, 3]
    detail = client.get(f"/sessions/{sid}").json()
    assert [m["run_id"] for m in detail["messages"]] == [None] * 4
    # The list view aggregates the client-written turns like any others.
    summary = client.get("/sessions").json()["sessions"][0]
    assert summary["message_count"] == 4
    assert summary["preview"] == "q1"


def test_client_turns_interleave_with_run_turns(client: TestClient) -> None:
    # The client writer and the run writer share the same FOR-UPDATE + max(position)+1
    # mechanics, so their turns interleave densely in one session.
    sid = str(ULID())
    _run(client, session_id=sid, text="from a run")
    assert [m["position"] for m in _turns(client, sid, "typed", "answered").json()["messages"]] == [
        2,
        3,
    ]
    detail = client.get(f"/sessions/{sid}").json()
    assert [m["position"] for m in detail["messages"]] == [0, 1, 2, 3]
    assert [bool(m["run_id"]) for m in detail["messages"]] == [True, True, False, False]


def test_append_turns_unknown_session_404(client: TestClient) -> None:
    # No implicit create — POST /sessions is the ensure.
    r = _turns(client, "nope", "q", "a")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


def test_append_turns_blank_content_422(client: TestClient, pg_url: str) -> None:
    # The no-blank-turns posture holds for client writes too; neither half lands.
    sid = client.post("/sessions", json={}).json()["id"]
    assert _turns(client, sid, "", "a").status_code == 422
    assert _turns(client, sid, "q", "").status_code == 422
    assert asyncio.run(count_messages(pg_url, sid)) == 0


def test_delete_session_drops_messages_and_detaches_runs(client: TestClient, pg_url: str) -> None:
    sid = str(ULID())
    run_id = _run(client, session_id=sid, text="hello")["runId"]
    assert _turns(client, sid, "q", "a").status_code == 201

    assert client.delete(f"/sessions/{sid}").status_code == 204

    # Messages gone; the run survives, detached (run history outlives the conversation).
    assert asyncio.run(count_messages(pg_url, sid)) == 0
    kept = client.get(f"/runs/{run_id}").json()
    assert kept["status"] == "completed"
    assert kept["session_id"] is None
    # The session itself is gone, and a second delete 404s.
    assert client.get(f"/sessions/{sid}").status_code == 404
    second = client.delete(f"/sessions/{sid}")
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "session_not_found"


def test_append_turn_into_missing_session_appends_nothing(pg_url: str) -> None:
    # DELETE /sessions racing an in-flight run: the FOR UPDATE lock finds no session row, so
    # append_turn must append NOTHING and return [] — inserting would hit the message FK and
    # blow up the caller's whole transaction (for a run, the completed+output write rides in
    # it, so a session deletion would destroy the run's outcome).
    from _db import count_all_messages
    from theygent_control_plane import db
    from theygent_control_plane.store import RunStore

    async def run_case() -> None:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        store = RunStore()
        try:
            async with sm() as s, s.begin():
                appended = await store.append_turn(
                    s,
                    session_id="deleted_under_us",
                    run_id=None,
                    user_content="q",
                    assistant_content="a",
                )
            assert appended == []  # the transaction committed cleanly, nothing written
            assert await count_all_messages(pg_url) == 0
        finally:
            await engine.dispose()

    asyncio.run(run_case())


def test_append_turns_losing_race_with_delete_is_404(client: TestClient) -> None:
    # The endpoint's existence check is unlocked, so a DELETE can land between it and the
    # locked append. Simulate the race deterministically: fake the check to say the session
    # exists while the row is really gone — the locked append then appends nothing and the
    # endpoint must 404, never 201 with an empty pair.
    async def fake_get_chat_session(session: object, session_id: str) -> object:
        # "exists" at check time (ownerless, so the ownership gate passes); gone by append time.
        return SimpleNamespace(user_id=None)

    client.app.state.store.get_chat_session = fake_get_chat_session  # type: ignore[method-assign]
    r = _turns(client, "ghost", "q", "a")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "session_not_found"


def test_list_sessions_carries_metadata(client: TestClient) -> None:
    sid = str(ULID())
    client.post(
        "/sessions", json={"id": sid, "metadata": {"kind": "bench.agent", "agent_id": "agt_x"}}
    )
    # A run-ensured session has no metadata — it lists honestly as null next to a tagged one.
    sid2 = str(ULID())
    _run(client, session_id=sid2)
    by_id = {s["id"]: s for s in client.get("/sessions").json()["sessions"]}
    assert by_id[sid]["metadata"] == {"kind": "bench.agent", "agent_id": "agt_x"}
    assert by_id[sid2]["metadata"] is None
