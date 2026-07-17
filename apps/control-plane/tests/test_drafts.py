"""Fast suite for /drafts — server-side autosaved editor drafts, over a REAL ephemeral
Postgres applied through REAL Alembic migrations. No SQLite, no create_all.

A draft is a mutable, autosaved, possibly-INVALID agent graph — the deliberate opposite of an
agent version (immutable, content-addressed, validated). So these prove the load-bearing
semantic difference: a document POST /agents rejects saves fine as a draft; the ir is never
validated or hashed; ``view`` is split out of the document into its own column and returned
alongside (the registry's ir/view split); ``name``/``node_count`` are re-derived on every save;
list summaries stay light (no ir/view); and ``owner_id`` rides as the null deferred-principal
slot until identity lands.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from _ir import trivial_ir
from fastapi.testclient import TestClient

_FULL_KEYS = {
    "id",
    "agent_id",
    "owner_id",
    "name",
    "node_count",
    "created_at",
    "updated_at",
    "ir",
    "view",
}


def _broken_ir() -> dict[str, Any]:
    """An ir that is legal JSON but an ILLEGAL graph: an unknown node type and the output's
    required in-port left unfed. POST /agents must 400 it; POST /drafts must accept it."""
    doc = trivial_ir()
    doc["nodes"][1]["type"] = "definitely-not-a-node-type"
    del doc["edges"][1]  # n_out's required in-port is now unfed
    return doc


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ── create + read ────────────────────────────────────────────────────────────


def test_create_and_get_round_trip_with_view_split(client: TestClient) -> None:
    ir = trivial_ir()
    ir["view"] = {"nodes": {"n_in": {"x": 10, "y": 20}}}
    resp = client.post("/drafts", json={"ir": ir})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == _FULL_KEYS
    assert body["id"].startswith("drf_")
    # The view is popped OUT of the document into its own field; the stored ir carries no view.
    assert "view" not in body["ir"]
    assert body["view"] == {"nodes": {"n_in": {"x": 10, "y": 20}}}
    expected_ir = {k: v for k, v in ir.items() if k != "view"}
    assert body["ir"] == expected_ir
    assert body["name"] == "trivial-llm"  # derived from the ir's own name
    assert body["node_count"] == 3
    # A draft is never hashed — no contentHash is ever stamped on the stored document.
    assert "contentHash" not in body["ir"]

    again = client.get(f"/drafts/{body['id']}")
    assert again.status_code == 200
    assert again.json() == body


def test_create_with_agent_id_and_filtered_list(client: TestClient) -> None:
    linked = client.post("/drafts", json={"ir": trivial_ir(), "agent_id": "agt_source"}).json()
    unlinked = client.post("/drafts", json={"ir": trivial_ir()}).json()
    assert linked["agent_id"] == "agt_source"
    assert unlinked["agent_id"] is None

    rows = client.get("/drafts", params={"agent_id": "agt_source"}).json()["drafts"]
    assert [r["id"] for r in rows] == [linked["id"]]
    assert client.get("/drafts", params={"agent_id": "agt_other"}).json()["drafts"] == []


def test_invalid_graph_saves_as_draft_but_not_as_agent(client: TestClient) -> None:
    # The load-bearing semantic difference: the registry validates, the draft store does not —
    # a half-wired/unknown-type graph is exactly what an autosave must be able to hold.
    broken = _broken_ir()
    rejected = client.post("/agents", json={"ir": broken})
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == "invalid_ir"

    saved = client.post("/drafts", json={"ir": broken})
    assert saved.status_code == 201, saved.text
    assert saved.json()["ir"]["nodes"][1]["type"] == "definitely-not-a-node-type"


def test_non_object_view_stores_no_layout(client: TestClient) -> None:
    # A layout is an object or absent; a non-object ``view`` is not a layout. It is still
    # popped out of the document (never stored inside the ir) and the draft saves cleanly.
    ir = trivial_ir()
    ir["view"] = "not a layout"
    resp = client.post("/drafts", json={"ir": ir})
    assert resp.status_code == 201, resp.text
    assert resp.json()["view"] is None
    assert "view" not in resp.json()["ir"]


def test_owner_id_is_null_in_responses(client: TestClient) -> None:
    created = client.post("/drafts", json={"ir": trivial_ir()}).json()
    assert created["owner_id"] is None  # the deferred per-user scoping slot
    listed = client.get("/drafts").json()["drafts"]
    assert listed[0]["owner_id"] is None


# ── update ───────────────────────────────────────────────────────────────────


def test_update_rederives_name_and_node_count_and_bumps_updated_at(client: TestClient) -> None:
    created = client.post("/drafts", json={"ir": trivial_ir()}).json()
    assert (created["name"], created["node_count"]) == ("trivial-llm", 3)

    edited = trivial_ir()
    edited["name"] = "renamed-draft"
    edited["nodes"] = edited["nodes"][:2]  # drop a node (an invalid graph — still fine)
    edited["view"] = {"zoom": 2}
    resp = client.put(f"/drafts/{created['id']}", json={"ir": edited})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "renamed-draft"
    assert body["node_count"] == 2
    assert body["view"] == {"zoom": 2}
    assert "view" not in body["ir"]
    assert _ts(body["updated_at"]) > _ts(created["updated_at"])
    assert body["created_at"] == created["created_at"]


def test_name_falls_back_to_id_then_untitled(client: TestClient) -> None:
    created = client.post("/drafts", json={"ir": trivial_ir()}).json()
    # No name → the ir's own id labels the draft.
    resp = client.put(f"/drafts/{created['id']}", json={"ir": {"id": "agt_x", "nodes": "nope"}})
    assert (resp.json()["name"], resp.json()["node_count"]) == ("agt_x", 0)
    # Neither name nor id → "Untitled" (an empty canvas autosave still lists legibly).
    resp = client.put(f"/drafts/{created['id']}", json={"ir": {"nodes": []}})
    assert (resp.json()["name"], resp.json()["node_count"]) == ("Untitled", 0)


def test_update_does_not_accept_agent_id(client: TestClient) -> None:
    # The origin breadcrumb is immutable after create — PUT carries only the document, and a
    # smuggled agent_id field is ignored rather than re-pointing the draft.
    created = client.post("/drafts", json={"ir": trivial_ir(), "agent_id": "agt_source"}).json()
    resp = client.put(
        f"/drafts/{created['id']}", json={"ir": trivial_ir(), "agent_id": "agt_hijack"}
    )
    assert resp.status_code == 200
    assert resp.json()["agent_id"] == "agt_source"


# ── the invalid_draft shape gate ─────────────────────────────────────────────


def test_non_object_ir_rejected_on_create(client: TestClient) -> None:
    for bad in ["not an object", ["a", "list"], 42, None]:
        resp = client.post("/drafts", json={"ir": bad})
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "invalid_draft"


def test_non_object_ir_rejected_on_update(client: TestClient) -> None:
    created = client.post("/drafts", json={"ir": trivial_ir()}).json()
    for bad in ["not an object", ["a", "list"], 42, None]:
        resp = client.put(f"/drafts/{created['id']}", json={"ir": bad})
        assert resp.status_code == 400, resp.text
        assert resp.json()["error"]["code"] == "invalid_draft"
    # The rejected saves left the draft untouched.
    assert client.get(f"/drafts/{created['id']}").json()["ir"] == created["ir"]


# ── 404s + delete ────────────────────────────────────────────────────────────


def test_unknown_draft_404s_on_get_put_delete(client: TestClient) -> None:
    assert client.get("/drafts/drf_nope").status_code == 404
    assert client.get("/drafts/drf_nope").json()["error"]["code"] == "draft_not_found"
    resp = client.put("/drafts/drf_nope", json={"ir": trivial_ir()})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "draft_not_found"
    resp = client.delete("/drafts/drf_nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "draft_not_found"


def test_delete_then_get_404(client: TestClient) -> None:
    created = client.post("/drafts", json={"ir": trivial_ir()}).json()
    resp = client.delete(f"/drafts/{created['id']}")
    assert resp.status_code == 204
    assert resp.content == b""
    assert client.get(f"/drafts/{created['id']}").status_code == 404


# ── list ─────────────────────────────────────────────────────────────────────


def test_list_newest_first_with_before_cursor(client: TestClient) -> None:
    ids = [client.post("/drafts", json={"ir": trivial_ir()}).json()["id"] for _ in range(3)]
    page1 = client.get("/drafts", params={"limit": 2}).json()["drafts"]
    assert [r["id"] for r in page1] == [ids[2], ids[1]]
    page2 = client.get("/drafts", params={"limit": 2, "before": page1[-1]["id"]}).json()["drafts"]
    assert [r["id"] for r in page2] == [ids[0]]
    # An unknown cursor is ignored (first page again), mirroring the other list endpoints.
    ignored = client.get("/drafts", params={"limit": 2, "before": "drf_nope"}).json()["drafts"]
    assert [r["id"] for r in ignored] == [ids[2], ids[1]]


def test_list_recency_follows_edits_not_creation(client: TestClient) -> None:
    # A draft is mutated in place, so "newest first" must mean most recently EDITED — editing
    # the oldest draft moves it to the front, and a truncated page keeps the just-touched work.
    ids = [client.post("/drafts", json={"ir": trivial_ir()}).json()["id"] for _ in range(2)]
    client.put(f"/drafts/{ids[0]}", json={"ir": trivial_ir()})
    top = client.get("/drafts", params={"limit": 1}).json()["drafts"]
    assert [r["id"] for r in top] == [ids[0]]


def test_list_summaries_carry_no_ir_or_view(client: TestClient) -> None:
    ir = trivial_ir()
    ir["view"] = {"zoom": 1}
    client.post("/drafts", json={"ir": ir, "agent_id": "agt_source"})
    rows = client.get("/drafts").json()["drafts"]
    assert len(rows) == 1
    # The summary is the full shape MINUS the document — the list stays light.
    assert set(rows[0]) == _FULL_KEYS - {"ir", "view"}
    assert rows[0]["agent_id"] == "agt_source"
    assert (rows[0]["name"], rows[0]["node_count"]) == ("trivial-llm", 3)
