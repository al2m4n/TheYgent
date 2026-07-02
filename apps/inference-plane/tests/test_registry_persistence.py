"""Fast suite — the inference-plane model registry persists LOCALLY.

The control-plane's MCP registry persists to Postgres; the inference plane's model registry must
NOT — it runs in the user's trust domain and persisting it into the shared control-plane DB would
couple the two planes (a plane-split regression). So it persists to a small
local JSON state file. These tests prove both halves: it survives a restart, and it does so with no
control-plane / database dependency whatsoever (the plane-split guard).
"""

from __future__ import annotations

import json
from pathlib import Path

from _fake_upstream import FakeUpstreamLauncher
from _payloads import managed_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app


def _app(launcher: FakeUpstreamLauncher, state_path: Path):
    return create_app(launcher=launcher, max_resident=2, enable_reaper=False, state_path=state_path)


def test_registration_survives_restart(tmp_path: Path) -> None:
    state = tmp_path / "registry.json"

    # First "process": register a model, then shut down.
    with TestClient(_app(FakeUpstreamLauncher(), state)) as c1:
        assert c1.put("/admin/models/local-fast", json=managed_payload()).status_code == 200
        assert "local-fast" in [m["logicalId"] for m in c1.get("/admin/models").json()["models"]]

    # Restart: a brand-new app + registry against the SAME local state file. Still registered.
    with TestClient(_app(FakeUpstreamLauncher(), state)) as c2:
        models = [m["logicalId"] for m in c2.get("/admin/models").json()["models"]]
        assert "local-fast" in models
        # The binding round-tripped, not just the id — it's still usable on /v1/models.
        assert "local-fast" in [m["id"] for m in c2.get("/v1/models").json()["data"]]


def test_deletion_persists(tmp_path: Path) -> None:
    state = tmp_path / "registry.json"
    with TestClient(_app(FakeUpstreamLauncher(), state)) as c1:
        c1.put("/admin/models/gone", json=managed_payload())
        assert c1.delete("/admin/models/gone").status_code == 204
    with TestClient(_app(FakeUpstreamLauncher(), state)) as c2:
        assert "gone" not in [m["logicalId"] for m in c2.get("/admin/models").json()["models"]]


def test_persisted_to_local_file_not_a_database(tmp_path: Path) -> None:
    # The store is a plain local JSON file — the proof it's local, not a shared DB row.
    state = tmp_path / "registry.json"
    with TestClient(_app(FakeUpstreamLauncher(), state)) as c1:
        c1.put("/admin/models/local-fast", json=managed_payload())
    assert state.exists()
    data = json.loads(state.read_text())
    assert "local-fast" in data["models"]


def test_in_memory_when_no_state_path() -> None:
    # Default (no state_path): pure in-memory — a restart starts empty, and nothing
    # is written to disk. This is what the rest of the fast suite relies on.
    with TestClient(create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False)) as c1:
        c1.put("/admin/models/ephemeral", json=managed_payload())
    with TestClient(create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False)) as c2:
        assert c2.get("/admin/models").json()["models"] == []


def test_legacy_registry_downgrades_to_chat(tmp_path: Path) -> None:
    # A registry.json written before the modality axis was introduced has no `modality` key on
    # its managed bindings. Loading it must default every such binding to "chat" (backward
    # compatibility) and resolve it to the chat launcher — never crash, never skip the row.
    state = tmp_path / "registry.json"
    state.write_text(
        json.dumps(
            {
                "models": {
                    "legacy-chat": {
                        "binding": "mlx",
                        "source": "hf",
                        "model": "mlx-community/old",
                        "params": {"temperature": 0.2},
                        "lifecycle": {"keepWarm": False, "idleTimeoutSec": 900, "priority": 0},
                    }
                }
            }
        )
    )
    with TestClient(_app(FakeUpstreamLauncher(), state)) as c:
        view = c.get("/admin/models/legacy-chat").json()
        assert view["binding"]["modality"] == "chat"  # defaulted on load
        # And it resolves end-to-end on the chat path (the fake serves chat).
        r = c.post("/v1/chat/completions", json={"model": "legacy-chat", "messages": []})
        assert r.status_code == 200


def test_plane_split_no_control_plane_db_dependency() -> None:
    # The plane-split guard: the inference registry must not reach for the control-plane's Postgres.
    # It IMPORTS no SQLAlchemy / asyncpg / control-plane module — persistence is local-only. Checked
    # against the actual imports (via AST), not substrings, so the docstring saying "no asyncpg"
    # doesn't trip it.
    import ast

    import theygent_inference_plane.registry as reg

    tree = ast.parse(Path(reg.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"sqlalchemy", "asyncpg", "psycopg", "theygent_control_plane"}
    assert not (imported & forbidden), f"inference registry must not import {imported & forbidden}"
