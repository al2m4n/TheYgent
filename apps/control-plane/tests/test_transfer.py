"""Tests for the transfer surface: POST /export + /import, PUT /artifacts/{ref}, and
DELETE /agents/{agent_id}.

Two invariants anchor everything here:

* **Secrets never enter a bundle.** A seeded install with a connection secret, a webhook signing
  secret, and legacy mcp env values exports a bundle carrying NONE of them — no value, no
  ``secret_ref``, no ciphertext, keys only — and import recreates the resources credential-less,
  reporting what the user must re-enter.
* **Import is id-preserving and idempotent.** A truncate-then-import restores rows under their
  original ids and timestamps (span PKs embed run ids; a re-export matches the original bundle on
  every stable part), and importing the same bundle twice skips everything.
"""

from __future__ import annotations

import asyncio
import copy
import json

import asyncpg
import pytest
from _db import plain_dsn, truncate
from _ir import trivial_ir
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from ulid import ULID

ALL_SECTIONS = ["agents", "runs", "traces", "sessions", "mcp", "rag"]

CONNECTION_SECRET = "super-secret-token-abc"
WEBHOOK_SECRET = "hook-signing-secret-xyz"
MCP_ENV_SECRET = "mcp-env-value-123"
MCP_CONN_ENV_SECRET = "ghp_secret123"
MCP_CONN_HEADER_SECRET = "k-abc"


@pytest.fixture
def client(fake_inference, pg_url: str):  # type: ignore[no-untyped-def]
    # A fixed Fernet key (the test_connections pattern) so the seeded connection secret is real
    # encrypted material — and provably absent from every exported bundle.
    key = Fernet.generate_key().decode()
    app = create_app(
        inference_base_url=fake_inference.v1_url, database_url=pg_url, secret_key=[key]
    )
    with TestClient(app) as test_client:
        yield test_client


async def _count(pg_url: str, table: str) -> int:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        return await conn.fetchval(f"SELECT count(*) FROM {table}")
    finally:
        await conn.close()


async def _run_params(pg_url: str, run_id: str) -> dict | None:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        raw = await conn.fetchval("SELECT params FROM run WHERE id = $1", run_id)
        return json.loads(raw) if isinstance(raw, str) else raw
    finally:
        await conn.close()


def _seed(client: TestClient) -> dict[str, str]:
    """One of everything the bundle carries: an agent with two versions + io-policy + a webhook
    trigger, a draft, a session with a traced run (params set), a connection WITH a secret, an
    mcp_server connection WITH env/header values, a legacy mcp server WITH env values, and a rag
    source."""
    ir1 = trivial_ir()
    resp = client.post("/agents", json={"ir": ir1, "name": "Trivial"})
    assert resp.status_code == 201
    agent_id = resp.json()["id"]
    ir2 = trivial_ir()
    ir2["version"] = "0.2.0"
    ir2["models"]["default"]["params"]["temperature"] = 0.5
    assert client.post(f"/agents/{agent_id}/versions", json={"ir": ir2}).status_code == 201
    assert (
        client.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "full"}).status_code == 200
    )
    resp = client.post(
        "/triggers",
        json={
            "agent_id": agent_id,
            "kind": "webhook",
            "version": "0.1.0",
            "config": {"secret": WEBHOOK_SECRET},
        },
    )
    assert resp.status_code == 201
    trigger_id = resp.json()["id"]
    resp = client.post(
        "/drafts", json={"ir": {"id": agent_id, "name": "wip"}, "agent_id": agent_id}
    )
    assert resp.status_code == 201
    draft_id = resp.json()["id"]
    session_id = client.post("/sessions", json={"metadata": {"kind": "chat"}}).json()["id"]
    resp = client.post(
        "/runs",
        json={
            "input": "hi",
            "model": "triage-fast",
            "params": {"temperature": 0.3},
            "stream": False,
            "session_id": session_id,
        },
    )
    assert resp.status_code == 200
    run_id = resp.json()["runId"]
    resp = client.post(
        "/connections",
        json={
            "name": "CRM",
            "kind": "http_auth",
            "config": {"baseUrl": "https://api.example.com", "auth": {"type": "bearer"}},
            "secret": CONNECTION_SECRET,
        },
    )
    assert resp.status_code == 201
    connection_id = resp.json()["id"]
    # An mcp_server-kind connection legitimately carries env/header NAME=value maps in config —
    # the values are credential material and must never export (keys only, values blanked).
    resp = client.post(
        "/connections",
        json={
            "name": "GitHub MCP",
            "kind": "mcp_server",
            "config": {
                "transport": "stdio",
                "command": "github-mcp",
                "env": {"GITHUB_TOKEN": MCP_CONN_ENV_SECRET},
                "headers": {"X-API-Key": MCP_CONN_HEADER_SECRET},
            },
        },
    )
    assert resp.status_code == 201
    mcp_connection_id = resp.json()["id"]
    resp = client.put(
        "/admin/mcp/servers/files",
        json={
            "transport": "stdio",
            "command": "echo",
            "args": ["-n"],
            "env": {"MY_TOKEN": MCP_ENV_SECRET},
        },
    )
    assert resp.status_code == 200
    resp = client.post(
        "/rag/sources",
        json={
            "name": "docs",
            "kind": "crawl",
            "embedding_model": "embed-small",
            "config": {"root_url": "https://example.com"},
        },
    )
    assert resp.status_code == 201
    rag_id = resp.json()["id"]
    return {
        "agent_id": agent_id,
        "trigger_id": trigger_id,
        "draft_id": draft_id,
        "session_id": session_id,
        "run_id": run_id,
        "connection_id": connection_id,
        "mcp_connection_id": mcp_connection_id,
        "rag_id": rag_id,
    }


# ── export → wipe → import → re-export round trip ────────────────────────────────────────────────


def test_export_import_round_trip(client: TestClient, pg_url: str) -> None:
    ids = _seed(client)
    resp = client.post("/export", json={"include": ALL_SECTIONS})
    assert resp.status_code == 200
    bundle = resp.json()
    assert bundle["format_version"] == 1

    # The hard invariant: NO secret material anywhere in the bundle.
    text = json.dumps(bundle)
    assert CONNECTION_SECRET not in text
    assert WEBHOOK_SECRET not in text
    assert MCP_ENV_SECRET not in text
    assert MCP_CONN_ENV_SECRET not in text
    assert MCP_CONN_HEADER_SECRET not in text
    assert "secret_ref" not in text
    assert "ciphertext" not in text
    trigger_entry = bundle["agents"][0]["triggers"][0]
    assert "secret" not in trigger_entry["config"]
    conns_by_id = {c["id"]: c for c in bundle["connections"]}
    conn_entry = conns_by_id[ids["connection_id"]]
    assert conn_entry["has_secret"] is True
    assert conn_entry["config"] == {
        "baseUrl": "https://api.example.com",
        "auth": {"type": "bearer"},
    }
    # The mcp_server connection's env/header KEYS survive (the shape imports cleanly) with
    # every VALUE blanked — never exported, never redacted to a real-looking placeholder.
    mcp_conn_entry = conns_by_id[ids["mcp_connection_id"]]
    assert mcp_conn_entry["config"]["env"] == {"GITHUB_TOKEN": ""}
    assert mcp_conn_entry["config"]["headers"] == {"X-API-Key": ""}
    srv_entry = bundle["mcp_servers"][0]
    assert srv_entry["env_keys"] == ["MY_TOKEN"]
    assert "env" not in srv_entry and "headers" not in srv_entry

    # run.params is on no public endpoint — the DB-level export carries it.
    run_entry = next(r for r in bundle["runs"] if r["id"] == ids["run_id"])
    assert run_entry["params"] == {"temperature": 0.3}
    assert run_entry["session_id"] == ids["session_id"]
    assert len(bundle["spans"]) >= 2  # run root + the llm node span
    assert len(bundle["node_io"]) == 1
    assert len(bundle["sessions"][0]["messages"]) == 2
    # Version documents ride verbatim (stored camelCase doc, contentHash stamped, view aside).
    versions = bundle["agents"][0]["versions"]
    assert [v["version"] for v in versions] == ["0.1.0", "0.2.0"]
    assert versions[0]["ir"]["contentHash"] == versions[0]["content_hash"]

    # Wipe the install, restore from the bundle.
    asyncio.run(truncate(pg_url))
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["agents"] == {
        "created": 1,
        "existing": 0,
        "versions_created": 2,
        "versions_existing": 0,
        "io_policies": 1,
        "io_policies_skipped": 0,
        "triggers_created": 1,
        "triggers_skipped": 0,
    }
    assert report["drafts"] == {"created": 1, "skipped": 0}
    assert report["runs"] == {"created": 1, "skipped": 0, "detached_sessions": 0}
    assert report["spans"]["created"] == len(bundle["spans"])
    assert report["node_io"]["created"] == len(bundle["node_io"])
    assert report["sessions"] == {
        "created": 1,
        "skipped": 0,
        "messages_created": 2,
        "detached_run_refs": 0,
    }
    assert report["connections"] == {
        "created": 2,
        "skipped": 0,
        "needs_secret": [ids["connection_id"]],
        "needs_oauth": [],
        "needs_env": [ids["mcp_connection_id"]],
    }
    assert report["mcp_servers"] == {"created": 1, "skipped": 0, "needs_env": ["files"]}
    assert report["rag_sources"] == {
        "created": 1,
        "skipped": 0,
        "needs_ingest": [ids["rag_id"]],
        "needs_upload": [],
    }
    # The webhook trigger lands disabled + credential-less, said loudly.
    assert [w["code"] for w in report["warnings"]] == ["needs_secret"]
    assert report["warnings"][0]["trigger_id"] == ids["trigger_id"]
    trigger_after = client.get(f"/triggers/{ids['trigger_id']}").json()
    assert trigger_after["enabled"] is False
    assert "secret" not in trigger_after["config"]

    # Re-export: every stable part matches the original bundle byte-for-byte.
    bundle2 = client.post("/export", json={"include": ALL_SECTIONS}).json()
    assert bundle2["runs"] == bundle["runs"]
    assert bundle2["spans"] == bundle["spans"]
    assert bundle2["node_io"] == bundle["node_io"]
    assert bundle2["sessions"] == bundle["sessions"]
    assert bundle2["drafts"] == bundle["drafts"]
    assert bundle2["rag_sources"] == bundle["rag_sources"]

    # Agents: identical except the webhook trigger's `enabled` (legitimately re-armed off).
    a1, a2 = copy.deepcopy(bundle["agents"][0]), copy.deepcopy(bundle2["agents"][0])
    assert a1["triggers"][0].pop("enabled") is True
    assert a2["triggers"][0].pop("enabled") is False
    assert a1 == a2

    # Connection: identical config/ids/timestamps; has_secret honestly false after import.
    conns2_by_id = {c["id"]: c for c in bundle2["connections"]}
    c1 = copy.deepcopy(conns_by_id[ids["connection_id"]])
    c2 = copy.deepcopy(conns2_by_id[ids["connection_id"]])
    assert c1.pop("has_secret") is True
    assert c2.pop("has_secret") is False
    assert c1 == c2
    # The mcp_server connection round-trips exactly: keys survive, values blank in both exports.
    assert conns2_by_id[ids["mcp_connection_id"]] == mcp_conn_entry

    # MCP server: env values never traveled, so the restored registration has no env keys.
    s1, s2 = copy.deepcopy(bundle["mcp_servers"][0]), copy.deepcopy(bundle2["mcp_servers"][0])
    assert s1.pop("env_keys") == ["MY_TOKEN"]
    assert s2.pop("env_keys") == []
    assert s1 == s2

    # And the DB-level fidelity check the endpoint exists for: params round-tripped on the row.
    assert asyncio.run(_run_params(pg_url, ids["run_id"])) == {"temperature": 0.3}
    # Message positions survived exactly (position is THE ordering key).
    session_after = client.get(f"/sessions/{ids['session_id']}").json()
    assert [m["position"] for m in session_after["messages"]] == [0, 1]


def test_import_is_idempotent(client: TestClient, pg_url: str) -> None:
    _seed(client)
    bundle = client.post("/export", json={"include": ALL_SECTIONS}).json()
    asyncio.run(truncate(pg_url))
    assert client.post("/import", json=bundle).status_code == 200

    second = client.post("/import", json=bundle)
    assert second.status_code == 200
    report = second.json()["report"]
    assert report["agents"] == {
        "created": 0,
        "existing": 1,
        "versions_created": 0,
        "versions_existing": 2,
        "io_policies": 0,
        "io_policies_skipped": 1,  # the live policy row is never touched by a re-import
        "triggers_created": 0,
        "triggers_skipped": 1,
    }
    assert report["drafts"] == {"created": 0, "skipped": 1}
    assert report["runs"] == {"created": 0, "skipped": 1, "detached_sessions": 0}
    assert report["spans"]["created"] == 0
    assert report["spans"]["skipped"] == len(bundle["spans"])
    assert report["node_io"]["created"] == 0
    assert report["sessions"]["created"] == 0
    assert report["sessions"]["skipped"] == 1
    assert report["connections"] == {
        "created": 0,
        "skipped": 2,
        "needs_secret": [],
        "needs_oauth": [],
        "needs_env": [],
    }
    assert report["mcp_servers"] == {"created": 0, "skipped": 1, "needs_env": []}
    assert report["rag_sources"] == {
        "created": 0,
        "skipped": 1,
        "needs_ingest": [],
        "needs_upload": [],
    }
    assert report["warnings"] == []

    # No duplicate rows: the third export equals the second (and row counts stay put).
    assert asyncio.run(_count(pg_url, "agent_version")) == 2
    assert asyncio.run(_count(pg_url, "message")) == 2
    assert asyncio.run(_count(pg_url, "span")) == len(bundle["spans"])


def test_version_conflict_reports_and_continues(client: TestClient) -> None:
    resp = client.post("/agents", json={"ir": trivial_ir()})
    assert resp.status_code == 201
    bundle = client.post("/export", json={"include": ["agents", "sessions"]}).json()

    # Same (agent_id, version), DIFFERENT content — immutability must hold on import too.
    mutated = copy.deepcopy(bundle)
    mutated["agents"][0]["versions"][0]["ir"]["models"]["default"]["params"]["temperature"] = 0.9
    mutated["sessions"] = [
        {
            "id": "ses_conflict_test",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "metadata": None,
            "messages": [],
        }
    ]
    resp = client.post("/import", json=mutated)
    assert resp.status_code == 200
    report = resp.json()["report"]
    conflicts = [w for w in report["warnings"] if w["code"] == "version_conflict"]
    assert len(conflicts) == 1
    assert conflicts[0]["version"] == "0.1.0"
    assert report["agents"]["versions_created"] == 0
    # One bad entity never aborts the rest: the session still imported.
    assert report["sessions"]["created"] == 1
    assert client.get("/sessions/ses_conflict_test").status_code == 200


def test_reimport_of_schema_drifted_version_is_not_a_conflict(
    client: TestClient, pg_url: str
) -> None:
    # A version stored under an older IR schema carries a content_hash that no longer matches a
    # re-hash under today's schema. Re-importing its own unchanged export must read as
    # versions_existing (idempotent), not version_conflict — hash inequality alone cannot prove
    # different content across schema epochs.
    resp = client.post("/agents", json={"ir": trivial_ir()})
    assert resp.status_code == 201
    agent_id = resp.json()["id"]
    bundle = client.post("/export", json={"include": ["agents"]}).json()

    async def _drift() -> None:
        conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
        try:
            await conn.execute(
                "UPDATE agent_version SET content_hash = 'sha256:" + "0" * 64 + "'"
                " WHERE agent_id = $1",
                agent_id,
            )
        finally:
            await conn.close()

    asyncio.run(_drift())
    # The bundle mirrors the drifted install: its stamped hash is the (old) stored one.
    bundle["agents"][0]["versions"][0]["content_hash"] = "sha256:" + "0" * 64

    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert [w for w in report["warnings"] if w["code"] == "version_conflict"] == []
    assert report["agents"]["versions_existing"] == 1
    assert report["agents"]["versions_created"] == 0


def test_import_detaches_missing_session_and_run_refs(client: TestClient) -> None:
    # A runs-only bundle referencing a session that is neither on the target nor in the bundle
    # imports with session_id NULL (counted), same posture as DELETE /sessions detaching runs.
    ts = "2026-01-01T00:00:00+00:00"
    bundle = {
        "format_version": 1,
        "runs": [
            {
                "id": "run_transfer_detach",
                "session_id": "ses_gone",
                "status": "completed",
                "model": "triage-fast",
                "params": {"temperature": 0.1},
                "output": "hello",
                "created_at": ts,
                "updated_at": ts,
                "completed_at": ts,
            }
        ],
        "sessions": [
            {
                "id": "ses_transfer_detach",
                "created_at": ts,
                "updated_at": ts,
                "metadata": None,
                "messages": [
                    {
                        "id": "msg_t1",
                        "run_id": "run_never_exported",
                        "role": "user",
                        "content": "q",
                        "position": 0,
                        "created_at": ts,
                    },
                    {
                        "id": "msg_t2",
                        "run_id": None,
                        "role": "assistant",
                        "content": "a",
                        "position": 1,
                        "created_at": ts,
                    },
                ],
            }
        ],
    }
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["runs"] == {"created": 1, "skipped": 0, "detached_sessions": 1}
    assert report["sessions"]["messages_created"] == 2
    assert report["sessions"]["detached_run_refs"] == 1
    run = client.get("/runs/run_transfer_detach").json()
    assert run["session_id"] is None
    detail = client.get("/sessions/ses_transfer_detach").json()
    assert [m["run_id"] for m in detail["messages"]] == [None, None]


def test_import_malformed_message_compensates_session(client: TestClient) -> None:
    # A malformed message fails its session with NOTHING written: no phantom messages_created
    # count, and the session row created earlier in the import is deleted again (compensation)
    # so a corrected re-import can retry instead of hitting whole-session skip-on-exists.
    ts = "2026-01-01T00:00:00+00:00"
    bundle = {
        "format_version": 1,
        "sessions": [
            {
                "id": "ses_compensate",
                "created_at": ts,
                "updated_at": ts,
                "metadata": None,
                "messages": [
                    {
                        "id": "msg_comp_ok",
                        "run_id": None,
                        "role": "user",
                        "content": "q",
                        "position": 0,
                        "created_at": ts,
                    },
                    {
                        # No position — unfillable, fails shape pre-validation.
                        "id": "msg_comp_bad",
                        "run_id": None,
                        "role": "assistant",
                        "content": "a",
                        "created_at": ts,
                    },
                ],
            }
        ],
    }
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["sessions"]["messages_created"] == 0
    assert report["sessions"]["created"] == 0  # compensated: the row was deleted again
    assert [w["code"] for w in report["warnings"]] == ["import_failed"]
    assert report["warnings"][0]["id"] == "ses_compensate"
    # Absent, not an empty husk that would block the retry.
    assert client.get("/sessions/ses_compensate").status_code == 404

    corrected = copy.deepcopy(bundle)
    corrected["sessions"][0]["messages"][1]["position"] = 1
    resp = client.post("/import", json=corrected)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["sessions"] == {
        "created": 1,
        "skipped": 0,
        "messages_created": 2,
        "detached_run_refs": 0,
    }
    assert report["warnings"] == []
    detail = client.get("/sessions/ses_compensate").json()
    assert [m["position"] for m in detail["messages"]] == [0, 1]


def test_import_duplicate_position_fails_in_tx_and_compensates(client: TestClient) -> None:
    # Shape pre-validation cannot catch a duplicate (session_id, position) — the unique index
    # rejects it MID-transaction. Same recovery contract: rollback, warn, compensate.
    ts = "2026-01-01T00:00:00+00:00"
    message = {
        "id": "msg_dup_a",
        "run_id": None,
        "role": "user",
        "content": "q",
        "position": 0,
        "created_at": ts,
    }
    bundle = {
        "format_version": 1,
        "sessions": [
            {
                "id": "ses_dup_position",
                "created_at": ts,
                "updated_at": ts,
                "metadata": None,
                "messages": [message, {**message, "id": "msg_dup_b"}],
            }
        ],
    }
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["sessions"]["messages_created"] == 0
    assert report["sessions"]["created"] == 0
    assert [w["code"] for w in report["warnings"]] == ["import_failed"]
    assert client.get("/sessions/ses_dup_position").status_code == 404

    corrected = copy.deepcopy(bundle)
    corrected["sessions"][0]["messages"][1]["position"] = 1
    resp = client.post("/import", json=corrected)
    assert resp.status_code == 200
    assert resp.json()["report"]["sessions"]["messages_created"] == 2
    assert client.get("/sessions/ses_dup_position").status_code == 200


def test_import_hostile_agent_sublists_warn_and_continue(client: TestClient) -> None:
    # Wrong-typed sub-lists ("versions": 5) must warn and continue — never a TypeError that
    # 500s the whole import mid-way with partial commits and no report.
    ts = "2026-01-01T00:00:00+00:00"
    bundle = {
        "format_version": 1,
        "agents": [
            {
                "id": "agt_hostile",
                "created_at": ts,
                "versions": 5,
                "io_policy": "nope",
                "triggers": 7,
            }
        ],
        "rag_sources": [
            {
                "id": "rag_after_hostile",
                "name": "docs",
                "kind": "crawl",
                "embedding_model": "embed-small",
                "config": {"root_url": "https://example.com"},
                "created_at": ts,
                "updated_at": ts,
            }
        ],
    }
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    # The agent row itself imported; each malformed sub-shape warned loudly.
    assert report["agents"]["created"] == 1
    messages = [w["message"] for w in report["warnings"] if w["code"] == "import_failed"]
    assert any("versions" in m for m in messages)
    assert any("triggers" in m for m in messages)
    assert any("io_policy" in m for m in messages)
    # Later sections still applied.
    assert report["rag_sources"]["created"] == 1
    assert client.get("/agents/agt_hostile").status_code == 200


def test_import_wrong_typed_section_warns_invalid_section(client: TestClient) -> None:
    # A section with the wrong JSON type (a single object, a string, a number) skips LOUDLY:
    # a 200 report with an invalid_section warning per bad section — never a silent no-op.
    bundle = {"format_version": 1, "runs": "nope", "spans": 3, "sessions": {"id": "x"}}
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    bad = {w["section"] for w in report["warnings"] if w["code"] == "invalid_section"}
    assert bad == {"runs", "spans", "sessions"}
    assert "runs" not in report and "spans" not in report and "sessions" not in report


def test_import_never_overwrites_existing_io_policy(client: TestClient) -> None:
    # The io policy is the agent owner's LIVE privacy control — a bundle must never flip it.
    agent_id = client.post("/agents", json={"ir": trivial_ir()}).json()["id"]
    put = client.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "full"})
    assert put.status_code == 200
    bundle = client.post("/export", json={"include": ["agents"]}).json()
    assert bundle["agents"][0]["io_policy"]["io_capture"] == "full"

    # The owner disables capture AFTER the export was taken...
    assert (
        client.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "off"}).status_code == 200
    )
    # ...and importing the stale bundle must not resurrect "full".
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["agents"]["io_policies"] == 0
    assert report["agents"]["io_policies_skipped"] == 1
    assert client.get(f"/agents/{agent_id}/io-policy").json()["io_capture"] == "off"


def test_mcp_server_sse_transport_round_trips(client: TestClient, pg_url: str) -> None:
    # sse is a working transport the registry accepts — it must survive export → import
    # verbatim, never be coerced to a stdio registration (command=NULL) that can never connect.
    resp = client.put(
        "/admin/mcp/servers/remote-sse",
        json={"transport": "sse", "url": "https://host.example/sse"},
    )
    assert resp.status_code == 200
    bundle = client.post("/export", json={"include": ["mcp"]}).json()
    entry = next(s for s in bundle["mcp_servers"] if s["name"] == "remote-sse")
    assert entry["transport"] == "sse"

    asyncio.run(truncate(pg_url))
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    assert resp.json()["report"]["mcp_servers"]["created"] == 1
    view = client.get("/admin/mcp/servers/remote-sse").json()
    assert view["transport"] == "sse"
    assert view["url"] == "https://host.example/sse"

    async def _row_transport() -> str:
        conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
        try:
            return await conn.fetchval(
                "SELECT transport FROM mcp_server WHERE name = $1", "remote-sse"
            )
        finally:
            await conn.close()

    # The persisted row (what the next boot rehydrates from) kept sse verbatim too.
    assert asyncio.run(_row_transport()) == "sse"


def test_import_unknown_mcp_transport_skips_with_warning(client: TestClient) -> None:
    ts = "2026-01-01T00:00:00+00:00"
    bundle = {
        "format_version": 1,
        "mcp_servers": [{"name": "weird", "transport": "carrier-pigeon", "created_at": ts}],
    }
    resp = client.post("/import", json=bundle)
    assert resp.status_code == 200
    report = resp.json()["report"]
    assert report["mcp_servers"]["created"] == 0
    assert report["mcp_servers"]["skipped"] == 1
    warning = next(w for w in report["warnings"] if w.get("section") == "mcp_servers")
    assert warning["id"] == "weird"
    assert "carrier-pigeon" in warning["message"]
    assert client.get("/admin/mcp/servers/weird").status_code == 404


def test_export_walks_artifact_refs(client: TestClient) -> None:
    # artifact_refs come from walking node_io payloads + run.output for {"ref": "art_..."} shapes
    # — seeded via import (which also proves span/node_io row import stands alone).
    ts = "2026-01-01T00:00:00+00:00"
    bundle = {
        "format_version": 1,
        "runs": [
            {
                "id": "run_art_walk",
                "session_id": None,
                "status": "completed",
                "model": "tts-voice",
                "params": None,
                "output": json.dumps({"ref": "art_OUTREF9", "contentType": "audio/wav"}),
                "created_at": ts,
                "updated_at": ts,
                "completed_at": ts,
            }
        ],
        "node_io": [
            {
                "id": "nio_art_walk",
                "run_id": "run_art_walk",
                "node_id": "n_speak",
                "span_id": None,
                "inputs": {"in": "say hi"},
                "outputs": {"ok": {"ref": "art_NODEREF1", "contentType": "audio/wav"}},
                "bytes_in": 6,
                "bytes_out": 42,
                "truncated": False,
                "capture_level": "full",
                "created_at": ts,
            }
        ],
    }
    assert client.post("/import", json=bundle).status_code == 200
    out = client.post("/export", json={"include": ["traces"]}).json()
    refs = {r["ref"]: r["content_type"] for r in out["artifact_refs"]}
    assert refs == {"art_NODEREF1": "audio/wav", "art_OUTREF9": "audio/wav"}


# ── request validation codes ─────────────────────────────────────────────────────────────────────


def test_export_invalid_include(client: TestClient) -> None:
    for body in (
        {},
        {"include": []},
        {"include": ["agents", "bogus"]},
        {"include": "agents"},  # non-list include: the documented 400, never a pydantic 422
        {"include": ["agents", 5]},  # non-string element
        {"include": None},
        [1, 2],  # non-object body
        "nope",
    ):
        resp = client.post("/export", json=body)
        assert resp.status_code == 400, body
        assert resp.json()["error"]["code"] == "invalid_include"


def test_export_traces_implies_runs(client: TestClient) -> None:
    resp = client.post("/export", json={"include": ["traces"]})
    assert resp.status_code == 200
    body = resp.json()
    assert "runs" in body and "spans" in body and "node_io" in body and "artifact_refs" in body
    # Only the requested sections (plus the implied runs) are present.
    for absent in ("agents", "drafts", "sessions", "connections", "mcp_servers", "rag_sources"):
        assert absent not in body


def test_import_invalid_bundle_codes(client: TestClient) -> None:
    resp = client.post("/import", json={"nope": 1})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_bundle"

    resp = client.post("/import", json=[1, 2])
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_bundle"

    resp = client.post("/import", json={"format_version": 2})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "unsupported_bundle_version"


# ── PUT /artifacts/{ref} (preserve-ref restore) ──────────────────────────────────────────────────


def test_put_artifact_round_trip(client: TestClient) -> None:
    ref = f"art_{ULID()}"  # the exact shape POST /artifacts mints (the artifact dir persists
    # across test runs, so a fixed ref would trip skip-on-exists on the second run)
    data = b"\x00\x01fake-wav-bytes"
    resp = client.put(f"/artifacts/{ref}", content=data, headers={"content-type": "audio/wav"})
    assert resp.status_code == 201
    assert resp.json() == {
        "ref": ref,
        "contentType": "audio/wav",
        "bytes": len(data),
        "created": True,
    }

    got = client.get(f"/artifacts/{ref}")
    assert got.status_code == 200
    assert got.content == data
    assert got.headers["content-type"].startswith("audio/wav")

    # Skip-on-exists: a second PUT — even with DIFFERENT bytes and content type — never
    # overwrites. The stored artifact stays readable and the response reports ITS metadata.
    resp = client.put(f"/artifacts/{ref}", content=b"new", headers={"content-type": "text/html"})
    assert resp.status_code == 200
    assert resp.json() == {
        "ref": ref,
        "contentType": "audio/wav",
        "bytes": len(data),
        "created": False,
    }
    after = client.get(f"/artifacts/{ref}")
    assert after.content == data
    assert after.headers["content-type"].startswith("audio/wav")


def test_put_artifact_rejects_bad_ref(client: TestClient) -> None:
    ulid = str(ULID())
    for bad in (
        "not-an-artifact",
        f"art_{ulid}.type",  # another artifact's content-type sidecar — a file this store owns
        "art_TRANSFERTEST01",  # art_ prefix but not the minted 26-char ULID shape
        f"art_{ulid.lower()}",  # minted refs are uppercase Crockford base32
        f"art_{ulid}extra",
    ):
        resp = client.put(f"/artifacts/{bad}", content=b"x")
        assert resp.status_code == 400, bad
        assert resp.json()["error"]["code"] == "invalid_artifact_ref"


# ── DELETE /agents/{agent_id} ────────────────────────────────────────────────────────────────────


def test_delete_agent_cascades_and_spares_breadcrumbs(client: TestClient, pg_url: str) -> None:
    agent_id = client.post("/agents", json={"ir": trivial_ir()}).json()["id"]
    ir2 = trivial_ir()
    ir2["version"] = "0.2.0"
    ir2["name"] = "second"
    assert client.post(f"/agents/{agent_id}/versions", json={"ir": ir2}).status_code == 201
    assert (
        client.put(f"/agents/{agent_id}/io-policy", json={"io_capture": "metadata"}).status_code
        == 200
    )
    trigger_id = client.post(
        "/triggers",
        json={"agent_id": agent_id, "kind": "http", "version": "0.1.0", "config": {}},
    ).json()["id"]
    draft_id = client.post("/drafts", json={"ir": {"name": "wip"}, "agent_id": agent_id}).json()[
        "id"
    ]
    run_resp = client.post(f"/agents/{agent_id}/runs", json={"input": "hi", "stream": False})
    assert run_resp.status_code == 200
    run_id = run_resp.json()["runId"]

    resp = client.delete(f"/agents/{agent_id}")
    assert resp.status_code == 204

    # The agent, its versions, its trigger, and its io-policy row are gone.
    assert client.get(f"/agents/{agent_id}").status_code == 404
    assert client.get(f"/triggers/{trigger_id}").status_code == 404
    assert asyncio.run(_count(pg_url, "agent_version")) == 0
    assert asyncio.run(_count(pg_url, "agent_io_policy")) == 0

    # Drafts keep their agent_id breadcrumb; runs keep graph lineage — both survive by design.
    draft = client.get(f"/drafts/{draft_id}")
    assert draft.status_code == 200
    assert draft.json()["agent_id"] == agent_id
    run = client.get(f"/runs/{run_id}")
    assert run.status_code == 200
    assert run.json()["graph_id"] == agent_id


def test_delete_agent_unknown_404(client: TestClient) -> None:
    resp = client.delete("/agents/agt_never_existed")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "agent_not_found"
