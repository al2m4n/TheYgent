"""Tests for the sample-agent catalog (``GET /samples`` + ``POST /samples/install``).

Two guards anchor the surface:

* **The catalog stays valid IR.** Every shipped sample, once its model slots and connection refs
  are filled, must pass the same ``parse_document`` + ``validate_graph`` gate as any published
  agent — a schema change in ``packages/ir`` that breaks a sample fails here, not at a user's
  install click.
* **Installs are ordinary registry writes.** An installed sample is a plain agent (same tables,
  same publish gate, same delete path) whose model bindings are the caller's choices — the
  shipped placeholder bindings must never reach the registry.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_control_plane.samples import load_catalog, render_ir
from theygent_ir import parse_document, validate_graph

_DURABLE_ONLY = {"human", "subgraph", "loop", "map"}


@pytest.fixture
def client(fake_inference, pg_url: str, tmp_path, monkeypatch) -> TestClient:  # type: ignore[no-untyped-def]
    # A per-test artifact dir so the seeded demo SQLite file lands in tmp_path, not the shared
    # system tempdir (seeding is idempotent, but tests must not observe each other's files).
    monkeypatch.setenv("THEYGENT_ARTIFACT_DIR", str(tmp_path / "artifacts"))
    app = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app) as test_client:
        yield test_client


def _slot_models(spec) -> dict[str, dict[str, str]]:  # type: ignore[no-untyped-def]
    return {
        slot: {
            "logical_id": "demo-local" if slot == "local" else "demo-api",
            "binding": "llamacpp" if slot == "local" else "openai-compatible",
        }
        for slot in spec.model_slots
    }


# ── the catalog itself (no HTTP, no DB) ──────────────────────────────────────


def test_every_sample_renders_to_valid_ir() -> None:
    catalog = load_catalog()
    assert len(catalog) == 12
    for spec in catalog:
        models = {
            slot: ("demo-local" if slot != "remote" else "demo-api", "llamacpp")
            for slot in spec.model_slots
        }
        connection_ids = {c.key: "con_01TESTTESTTESTTESTTESTTEST" for c in spec.connections}
        rag_ids = {r.key: "rag_01TESTTESTTESTTESTTESTTES" for r in spec.rag_sources}
        rendered_docs = render_ir(
            spec, models=models, connection_ids=connection_ids, rag_ids=rag_ids
        )
        uses_durable = False
        for i, rendered in enumerate(rendered_docs):
            ir = parse_document(rendered)
            validate_graph(ir)
            # Users identify samples by the name prefix; the hashed marker records origin.
            assert ir.name.startswith("sample-")
            assert (ir.metadata or {}).get("sample") == spec.id
            # No placeholder survives rendering — a leftover would dangle at run time.
            blob = json.dumps(rendered)
            assert "{connection:" not in blob
            assert "{seed:" not in blob
            assert "{rag:" not in blob
            assert "your-" not in blob
            uses_durable = uses_durable or any(n.type in _DURABLE_ONLY for n in ir.nodes)
            if i == len(rendered_docs) - 1:  # the primary is last (children install first)
                assert ir.id == spec.agent_id
        assert spec.agent_id.startswith("agt_sample_")
        # The durable flag must match the graphs — a mislabeled sample would send users down
        # the wrong run path (interactive runs reject durable-only nodes with 400).
        assert spec.durable == uses_durable


def test_render_requires_every_declared_slot() -> None:
    from theygent_control_plane.samples import SampleModelsError

    spec = next(s for s in load_catalog() if s.id == "hybrid-reasoner")
    with pytest.raises(SampleModelsError) as excinfo:
        render_ir(spec, models={"local": ("demo-local", "mlx")}, connection_ids={})
    assert "remote" in str(excinfo.value)


# ── GET /samples ─────────────────────────────────────────────────────────────


def test_list_samples_shape(client: TestClient) -> None:
    resp = client.get("/samples")
    assert resp.status_code == 200, resp.text
    samples = resp.json()["samples"]
    assert [s["id"] for s in samples] == [
        "hybrid-reasoner",
        "private-sql-analyst",
        "expense-approver",
        "tool-belt",
        "second-opinion",
        "pii-firewall",
        "visual-inspector",
        "voice-desk",
        "art-department",
        "handbook-qa",
        "editorial-loop",
        "batch-briefing",
    ]
    by_id = {s["id"]: s for s in samples}
    assert all(s["installed"] is False for s in samples)
    assert by_id["expense-approver"]["durable"] is True
    assert set(by_id["private-sql-analyst"]["model_slots"]) == {"local", "remote"}
    transports = {c["transport"] for c in by_id["tool-belt"]["connections"]}
    assert transports == {"graphql", "openapi"}
    assert by_id["hybrid-reasoner"]["agent_id"] == "agt_sample_hybrid_reasoner"
    # The modality axis rides each slot so the UI can filter the picker.
    assert by_id["visual-inspector"]["model_slots"]["vision"]["modality"] == "vision"
    assert by_id["voice-desk"]["model_slots"]["stt"]["modality"] == "audio.transcription"
    assert by_id["handbook-qa"]["rag_sources"] == [{"key": "handbook", "name": "sample-handbook"}]
    # Composition samples ship their child agent too, children first.
    assert by_id["batch-briefing"]["agent_names"] == [
        "sample-briefing-worker",
        "sample-batch-briefing",
    ]
    assert by_id["batch-briefing"]["trigger"] == {"kind": "schedule", "enabled": False}
    assert by_id["second-opinion"]["trigger"] is None


# ── POST /samples/install ────────────────────────────────────────────────────


def test_install_stamps_chosen_models(client: TestClient) -> None:
    spec = next(s for s in load_catalog() if s.id == "hybrid-reasoner")
    resp = client.post(
        "/samples/install",
        json={"ids": ["hybrid-reasoner"], "models": _slot_models(spec)},
    )
    assert resp.status_code == 200, resp.text
    (entry,) = resp.json()["report"]
    assert entry["status"] == "installed"
    assert entry["agent_id"] == "agt_sample_hybrid_reasoner"

    stored = client.get("/agents/agt_sample_hybrid_reasoner/versions/1.0.0")
    assert stored.status_code == 200, stored.text
    models = stored.json()["ir"]["models"]
    assert models["triage"] == {
        "binding": "llamacpp",
        "model": "demo-local",
        "source": None,
        "params": models["triage"]["params"],
    }
    assert models["remote"]["binding"] == "openai-compatible"
    assert models["remote"]["model"] == "demo-api"

    listed = client.get("/samples").json()["samples"]
    assert next(s for s in listed if s["id"] == "hybrid-reasoner")["installed"] is True


def test_install_creates_connections_and_seeds_db(client: TestClient, tmp_path) -> None:  # type: ignore[no-untyped-def]
    spec = next(s for s in load_catalog() if s.id == "private-sql-analyst")
    resp = client.post(
        "/samples/install",
        json={"ids": ["private-sql-analyst"], "models": _slot_models(spec)},
    )
    assert resp.status_code == 200, resp.text
    (entry,) = resp.json()["report"]
    assert entry["status"] == "installed"
    (conn_entry,) = entry["connections"]
    assert conn_entry["key"] == "crm_db"
    assert conn_entry["created"] is True

    conn = client.get(f"/connections/{conn_entry['connection_id']}")
    assert conn.status_code == 200, conn.text
    body = conn.json()
    assert body["name"] == "sample-crm-sqlite"
    assert body["kind"] == "mcp_server"
    db_path = body["config"]["args"][-1]
    assert str(tmp_path) in db_path  # seeded under the test's THEYGENT_ARTIFACT_DIR

    # The seeded database is real, deterministic data the sample queries.
    rows = sqlite3.connect(db_path).execute("SELECT count(*) FROM customers").fetchone()
    assert rows == (8,)

    # The stored IR references the minted connection id — no placeholder survives.
    stored = client.get("/agents/agt_sample_private_sql_analyst/versions/1.0.0").json()
    query_node = next(n for n in stored["ir"]["nodes"] if n["id"] == "n_query")
    assert query_node["config"]["connection"] == conn_entry["connection_id"]


def test_reinstall_is_already_installed_and_reuses_connections(client: TestClient) -> None:
    spec = next(s for s in load_catalog() if s.id == "private-sql-analyst")
    models = _slot_models(spec)
    first = client.post("/samples/install", json={"ids": ["private-sql-analyst"], "models": models})
    assert first.status_code == 200, first.text

    again = client.post("/samples/install", json={"ids": ["private-sql-analyst"], "models": models})
    (entry,) = again.json()["report"]
    assert entry["status"] == "already_installed"

    # Delete the agent, reinstall: a fresh agent, but the existing connection is reused (found
    # by name) instead of minting a duplicate server per install.
    assert client.delete("/agents/agt_sample_private_sql_analyst").status_code == 204
    third = client.post("/samples/install", json={"ids": ["private-sql-analyst"], "models": models})
    (entry,) = third.json()["report"]
    assert entry["status"] == "installed"
    (conn_entry,) = entry["connections"]
    assert conn_entry["created"] is False

    conns = client.get("/connections").json()["connections"]
    assert sum(1 for c in conns if c["name"] == "sample-crm-sqlite") == 1


def test_missing_model_slot_is_400_and_writes_nothing(client: TestClient) -> None:
    resp = client.post(
        "/samples/install",
        json={
            "ids": ["private-sql-analyst"],
            "models": {"local": {"logical_id": "demo-local", "binding": "mlx"}},
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()["error"]
    assert body["code"] == "sample_models_required"
    assert "remote" in body["message"]
    # Pre-flight runs before any write: no agent, no connection.
    assert client.get("/agents/agt_sample_private_sql_analyst").status_code == 404
    conns = client.get("/connections").json()["connections"]
    assert all(c["name"] != "sample-crm-sqlite" for c in conns)


def test_unknown_sample_is_404(client: TestClient) -> None:
    resp = client.post("/samples/install", json={"ids": ["no-such-sample"], "models": {}})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "sample_not_found"


def test_empty_ids_is_400(client: TestClient) -> None:
    resp = client.post("/samples/install", json={"ids": [], "models": {}})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_install"


_ALL_SLOT_MODELS = {
    "local": {"logical_id": "demo-local", "binding": "llamacpp"},
    "remote": {"logical_id": "demo-api", "binding": "openai-compatible"},
    "vision": {"logical_id": "demo-vision", "binding": "mlx"},
    "stt": {"logical_id": "demo-whisper", "binding": "mlx"},
    "tts": {"logical_id": "demo-voice", "binding": "mlx"},
    "image": {"logical_id": "demo-diffusion", "binding": "mlx"},
    "embedding": {"logical_id": "demo-embed", "binding": "llamacpp"},
}


def test_install_every_shipped_sample(client: TestClient) -> None:
    # The whole catalog installs through the real gates: generated-connection configs pass
    # _validate_connection, children publish before their parents, the retrieval source is
    # created, and the shipped trigger lands DISABLED. (Durable-only samples install fine —
    # the durable check is at RUN time, install is registry-only.)
    all_ids = [s["id"] for s in client.get("/samples").json()["samples"]]
    resp = client.post("/samples/install", json={"ids": all_ids, "models": _ALL_SLOT_MODELS})
    assert resp.status_code == 200, resp.text
    assert [e["status"] for e in resp.json()["report"]] == ["installed"] * 12
    listed = client.get("/samples").json()["samples"]
    assert all(s["installed"] for s in listed)

    conns = {c["name"]: c for c in client.get("/connections").json()["connections"]}
    assert conns["sample-open-meteo"]["config"]["transport"] == "openapi"
    assert conns["sample-countries-graphql"]["config"]["transport"] == "graphql"
    assert conns["sample-crm-sqlite"]["config"]["transport"] == "stdio"

    # Children published before their parents, so the parents' pins resolve.
    for child in ("agt_sample_briefing_worker", "agt_sample_editorial_writer"):
        assert client.get(f"/agents/{child}").status_code == 200

    # The seed-document ingest STARTED for the fresh source — a warning here means the seed
    # path itself is broken (a degraded install), not merely that embedding failed later.
    handbook_entry = next(e for e in resp.json()["report"] if e["id"] == "handbook-qa")
    assert handbook_entry["warnings"] == []

    # The retrieval source exists, pinned to the embedding-slot choice; the primary IR
    # references it by minted id.
    sources = client.get("/rag/sources").json()["sources"]
    handbook = next(s for s in sources if s["name"] == "sample-handbook")
    assert handbook["embedding_model"] == "demo-embed"
    stored = client.get("/agents/agt_sample_handbook_qa/versions/1.0.0").json()
    rag_node = next(n for n in stored["ir"]["nodes"] if n["id"] == "n_handbook")
    assert rag_node["config"]["source"] == handbook["id"]

    # The shipped schedule trigger landed disabled, pinned to the published version.
    trig = next(
        t
        for t in client.get("/triggers").json()["triggers"]
        if t["agent_id"] == "agt_sample_batch_briefing"
    )
    assert trig["kind"] == "schedule"
    assert trig["enabled"] is False
    assert trig["version"] == "1.0.0"


def test_invalid_binding_is_400_and_writes_nothing(client: TestClient) -> None:
    resp = client.post(
        "/samples/install",
        json={
            "ids": ["tool-belt"],
            "models": {"local": {"logical_id": "demo-local", "binding": "bogus-engine"}},
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()["error"]
    assert body["code"] == "sample_model_invalid"
    assert "bogus-engine" in body["message"]
    assert client.get("/agents/agt_sample_tool_belt").status_code == 404
    assert client.get("/connections").json()["connections"] == []


def test_failed_sample_rolls_back_and_batch_continues(client: TestClient, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # The transaction guard: a sample that fails mid-install (here: its SECOND connection config
    # is invalid, after the first was already flushed) must persist NOTHING — connections
    # included — while the rest of the batch still installs and reports.
    from theygent_control_plane import app as app_module
    from theygent_control_plane.samples import SampleConnection, load_catalog

    real = load_catalog()
    good = next(s for s in real if s.id == "hybrid-reasoner")
    broken_ir = dict(next(s for s in real if s.id == "tool-belt").ir)
    broken = next(s for s in real if s.id == "tool-belt").__class__(
        id="broken-sample",
        title="Broken",
        description="a catalog bug",
        capabilities=(),
        durable=False,
        input_example=None,
        model_slots={},
        connections=(
            SampleConnection(
                key="ok",
                name="sample-broken-ok",
                config={"transport": "graphql", "url": "https://example.com/graphql"},
                seed=None,
            ),
            SampleConnection(
                key="bad",
                name="sample-broken-bad",
                config={"transport": "stdio"},  # stdio without command fails validation
                seed=None,
            ),
        ),
        ir=broken_ir,
    )
    monkeypatch.setattr(app_module, "load_catalog", lambda: (broken, good))

    resp = client.post(
        "/samples/install",
        json={
            "ids": ["broken-sample", "hybrid-reasoner"],
            "models": {
                "local": {"logical_id": "demo-local", "binding": "llamacpp"},
                "remote": {"logical_id": "demo-api", "binding": "openai-compatible"},
            },
        },
    )
    assert resp.status_code == 200, resp.text
    by_id = {e["id"]: e for e in resp.json()["report"]}
    assert by_id["broken-sample"]["status"] == "error"
    assert by_id["broken-sample"]["code"] == "sample_invalid"
    assert by_id["hybrid-reasoner"]["status"] == "installed"
    # The failed sample's FIRST connection was flushed before the second failed — the raise must
    # have rolled the whole transaction back.
    conns = client.get("/connections").json()["connections"]
    assert all(not c["name"].startswith("sample-broken") for c in conns)
    assert client.get("/agents/agt_sample_hybrid_reasoner").status_code == 200


def test_deleting_a_child_reads_uninstalled_and_reinstall_repairs(client: TestClient) -> None:
    # A composition sample's child is deletable like any agent; the sample must then read as
    # NOT installed (its parent's pin dangles), and reinstalling must republish only the
    # missing child — never touching the surviving parent.
    models = {"local": {"logical_id": "demo-local", "binding": "llamacpp"}}
    first = client.post("/samples/install", json={"ids": ["editorial-loop"], "models": models})
    assert first.status_code == 200, first.text
    assert first.json()["report"][0]["status"] == "installed"

    assert client.delete("/agents/agt_sample_editorial_writer").status_code == 204
    listed = client.get("/samples").json()["samples"]
    assert next(s for s in listed if s["id"] == "editorial-loop")["installed"] is False

    repair = client.post("/samples/install", json={"ids": ["editorial-loop"], "models": models})
    assert repair.json()["report"][0]["status"] == "installed"
    assert client.get("/agents/agt_sample_editorial_writer").status_code == 200
    parent = client.get("/agents/agt_sample_editorial_loop").json()
    assert len(parent["versions"]) == 1  # the surviving parent was reused, not re-published


def test_viewer_can_browse_but_not_install(client: TestClient) -> None:
    mk = client.post(
        "/users", json={"username": "sam-viewer", "password": "user-pass-123", "role": "viewer"}
    )
    assert mk.status_code == 201, mk.text
    login = client.post("/auth/login", json={"username": "sam-viewer", "password": "user-pass-123"})
    headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/samples", headers=headers).status_code == 200
    denied = client.post(
        "/samples/install", json={"ids": ["hybrid-reasoner"], "models": {}}, headers=headers
    )
    assert denied.status_code == 403
