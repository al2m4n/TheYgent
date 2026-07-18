"""The local settings store + /admin/settings + /admin/diagnostics.

Settings persist in settings.json beside registry.json — the plane's own state dir, never
the control plane. Resolution is env > stored > default with empty-env == unset; the wire
reports the winning source honestly, an env-pinned knob refuses writes, and a lowered
ceiling applies live (idle excess evicted now, busy engines drain on idle).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _fake_upstream import FakeUpstreamLauncher
from _payloads import managed_payload
from fastapi.testclient import TestClient
from theygent_inference_plane.__main__ import _env, _env_int
from theygent_inference_plane.app import create_app
from theygent_inference_plane.launcher import LlamaCppLauncher, ManagedLauncherSet
from theygent_inference_plane.settings import (
    MAX_RESIDENT_DEFAULT,
    SettingsStore,
    resolve_max_resident,
)

# ── the store ────────────────────────────────────────────────────────────────


def test_store_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    SettingsStore(path).set_max_resident(3)
    # A fresh store rehydrates from disk; the file speaks the wire's camelCase dialect.
    assert SettingsStore(path).max_resident == 3
    assert json.loads(path.read_text()) == {"maxResident": 3}


def test_store_reset_deletes_the_key(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.set_max_resident(4)
    store.set_max_resident(None)
    assert store.max_resident is None
    assert "maxResident" not in json.loads(path.read_text())


def test_store_in_memory_when_no_path(tmp_path: Path) -> None:
    store = SettingsStore()
    store.set_max_resident(5)
    assert store.max_resident == 5
    assert list(tmp_path.iterdir()) == []  # nothing touched disk


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{ not json")
    assert SettingsStore(path).max_resident is None  # recoverable, not fatal


def test_store_ignores_invalid_stored_values(tmp_path: Path) -> None:
    # A hand-edited value that no longer validates resolves as UNSET (the default wins),
    # never half-applies. Bools are not ints here.
    path = tmp_path / "settings.json"
    for bad in ('"lots"', "0", "999", "true", "2.5"):
        path.write_text(f'{{"maxResident": {bad}}}')
        assert SettingsStore(path).max_resident is None, bad


def test_store_persist_leaves_no_temp_files(tmp_path: Path) -> None:
    # Write-temp-then-rename: after a persist the directory holds exactly the settings file.
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    store.set_max_resident(2)
    store.set_max_resident(3)
    assert [p.name for p in tmp_path.iterdir()] == ["settings.json"]
    assert SettingsStore(path).max_resident == 3


# ── resolution precedence (env > stored > default) ───────────────────────────


def test_resolution_precedence() -> None:
    store = SettingsStore()
    assert resolve_max_resident(None, store).source == "default"
    assert resolve_max_resident(None, store).value == MAX_RESIDENT_DEFAULT
    store.set_max_resident(4)
    assert resolve_max_resident(None, store).value == 4
    assert resolve_max_resident(None, store).source == "stored"
    # An explicit value (the env-injection seam) wins over a stored one and pins.
    resolved = resolve_max_resident(6, store)
    assert (resolved.value, resolved.source, resolved.pinned) == (6, "env", True)


def test_env_helpers_treat_empty_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # The entrypoint convention: empty == unset for EVERY env read, and an int parse must
    # never crash on "" (compose passes UI-settable knobs through as ${VAR:-}).
    monkeypatch.delenv("THEYGENT_MAX_RESIDENT", raising=False)
    assert _env_int("THEYGENT_MAX_RESIDENT") is None
    monkeypatch.setenv("THEYGENT_MAX_RESIDENT", "")
    assert _env_int("THEYGENT_MAX_RESIDENT") is None
    monkeypatch.setenv("THEYGENT_MAX_RESIDENT", "  ")
    assert _env_int("THEYGENT_MAX_RESIDENT") is None
    monkeypatch.setenv("THEYGENT_MAX_RESIDENT", "3")
    assert _env_int("THEYGENT_MAX_RESIDENT") == 3
    monkeypatch.setenv("THEYGENT_INFERENCE_PLANE_HOST", "")
    assert _env("THEYGENT_INFERENCE_PLANE_HOST") is None
    monkeypatch.setenv("THEYGENT_INFERENCE_PLANE_HOST", "0.0.0.0")
    assert _env("THEYGENT_INFERENCE_PLANE_HOST") == "0.0.0.0"


# ── GET/PATCH /admin/settings ────────────────────────────────────────────────


def _client(**kwargs) -> TestClient:
    return TestClient(create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False, **kwargs))


def test_get_settings_default_shape() -> None:
    with _client() as c:
        assert c.get("/admin/settings").json() == {
            "maxResident": {
                "value": 2,
                "default": 2,
                "source": "default",
                "envVar": "THEYGENT_MAX_RESIDENT",
                "apply": "live",
            }
        }


def test_patch_stores_applies_and_resets(tmp_path: Path) -> None:
    state_path = tmp_path / "registry.json"
    with _client(state_path=state_path) as c:
        r = c.patch("/admin/settings", json={"maxResident": 3})
        assert r.status_code == 200
        body = r.json()["maxResident"]
        assert (body["value"], body["source"]) == (3, "stored")
        # Applied live to the manager (the /admin/engines snapshot reads the same ceiling)…
        assert c.get("/admin/engines").json()["maxResident"] == 3
        # …and persisted beside registry.json.
        assert json.loads((tmp_path / "settings.json").read_text()) == {"maxResident": 3}

        # null = reset: the stored value is deleted and the default wins again.
        r = c.patch("/admin/settings", json={"maxResident": None})
        body = r.json()["maxResident"]
        assert (body["value"], body["source"]) == (2, "default")
        assert "maxResident" not in json.loads((tmp_path / "settings.json").read_text())


def test_stored_value_survives_restart(tmp_path: Path) -> None:
    state_path = tmp_path / "registry.json"
    with _client(state_path=state_path) as c:
        c.patch("/admin/settings", json={"maxResident": 5})
    # A new app over the same state dir boots with the stored ceiling.
    with _client(state_path=state_path) as c:
        body = c.get("/admin/settings").json()["maxResident"]
        assert (body["value"], body["source"]) == (5, "stored")
        assert c.get("/admin/engines").json()["maxResident"] == 5


def test_env_pinned_reports_and_refuses_writes(tmp_path: Path) -> None:
    # An explicit max_resident is the env-injection seam: reported as source "env" and
    # locked — a write is refused with a clear error naming the variable, and nothing
    # is stored.
    state_path = tmp_path / "registry.json"
    with _client(max_resident=4, state_path=state_path) as c:
        body = c.get("/admin/settings").json()["maxResident"]
        assert (body["value"], body["source"]) == (4, "env")

        r = c.patch("/admin/settings", json={"maxResident": 2})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "env_pinned"
        assert "THEYGENT_MAX_RESIDENT" in r.json()["error"]["message"]
        assert not (tmp_path / "settings.json").exists()
        assert c.get("/admin/engines").json()["maxResident"] == 4


def test_patch_rejects_invalid_values() -> None:
    with _client() as c:
        for bad in (
            {"maxResident": 0},  # below the floor
            {"maxResident": 17},  # above the sane ceiling
            {"maxResident": "3"},  # strings are not ints (strict)
            {"maxResident": True},  # bools are not ints
            {"maxResident": 2, "bogus": 1},  # unknown key (extra="forbid")
            {},  # the one knob is required; an empty patch changes nothing honestly
        ):
            r = c.patch("/admin/settings", json=bad)
            assert r.status_code == 422, bad
            assert r.json()["error"]["code"] == "invalid_settings", bad
        # Nothing half-applied.
        assert c.get("/admin/settings").json()["maxResident"]["value"] == 2


def test_patch_lowered_ceiling_evicts_idle_excess_now() -> None:
    # Two engines resident, ceiling lowered to 1 via PATCH → the excess idle engine is
    # evicted immediately ("apply": "live"), not on the next admission.
    launcher = FakeUpstreamLauncher()
    app = create_app(launcher=launcher, max_resident=None, enable_reaper=False)
    with TestClient(app) as c:
        c.put("/admin/models/a", json=managed_payload(model="a"))
        c.put("/admin/models/b", json=managed_payload(model="b"))
        c.post("/admin/models/a:warm")
        c.post("/admin/models/b:warm")
        assert len(c.get("/admin/engines").json()["resident"]) == 2

        r = c.patch("/admin/settings", json={"maxResident": 1})
        assert r.status_code == 200
        engines = c.get("/admin/engines").json()
        assert engines["maxResident"] == 1
        assert len(engines["resident"]) == 1
        assert sum(1 for h in launcher.handles if h.terminated) == 1


# ── GET /admin/diagnostics ───────────────────────────────────────────────────


class _ResolvedStub:
    """A launcher whose binary resolved — diagnostics reads only these three members."""

    ready = True
    not_ready_reason = None
    resolved_path = "/opt/engines/real-server"

    async def launch(self, binding):  # pragma: no cover - never spawned here
        raise AssertionError("diagnostics must not spawn")


def test_diagnostics_shape_with_missing_binary(tmp_path: Path) -> None:
    # One resolved launcher + one whose binary is missing (a bogus explicit path resolves
    # to not-found without raising) — the same (engine, modality) keys the set was built
    # with come back, nothing is spawned, and missing → status "missing" with a null path.
    launcher_set = ManagedLauncherSet(
        {
            ("llamacpp", "chat"): _ResolvedStub(),
            ("llamacpp", "embeddings"): LlamaCppLauncher(str(tmp_path / "no-such-binary")),
        }
    )
    app = create_app(
        launcher=launcher_set,
        enable_reaper=False,
        state_path=tmp_path / "registry.json",
        model_dir=tmp_path / "models",
    )
    with TestClient(app) as c:
        body = c.get("/admin/diagnostics").json()
    assert body["stateDir"] == str(tmp_path)
    assert body["modelDir"] == str(tmp_path / "models")
    assert body["persistent"] is True
    assert isinstance(body["hfHome"], str) and body["hfHome"]
    by_key = {(b["engine"], b["modality"]): b for b in body["binaries"]}
    assert by_key[("llamacpp", "chat")] == {
        "engine": "llamacpp",
        "modality": "chat",
        "path": "/opt/engines/real-server",
        "status": "resolved",
    }
    assert by_key[("llamacpp", "embeddings")]["status"] == "missing"
    assert by_key[("llamacpp", "embeddings")]["path"] is None


def test_diagnostics_in_memory_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_HOME", "/tmp/hf-home")
    monkeypatch.delenv("HUGGINGFACE_HUB_CACHE", raising=False)
    with _client() as c:
        body = c.get("/admin/diagnostics").json()
    assert body["stateDir"] is None
    assert body["persistent"] is False
    assert body["hfHome"] == "/tmp/hf-home/hub"  # the resolved hub dir, not the raw var
    assert body["binaries"] == []  # a single injected launcher has no (engine, modality) keys
