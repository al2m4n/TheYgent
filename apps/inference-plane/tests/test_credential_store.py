"""The user-side credential store + `secret://NAME` resolution.

A local named-secret store (the user's trust domain — never the control plane) backing reachable
bindings' credentialRefs. Covers the write-only-value contract, the resolution order (store first,
then env), and the `/admin/credentials` wire (names out, values never out).
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from _fake_upstream import FakeUpstreamLauncher
from fastapi.testclient import TestClient
from theygent_inference_plane.app import create_app
from theygent_inference_plane.credentials import (
    CredentialResolutionError,
    CredentialStore,
    InvalidCredentialName,
    resolve_credential,
)

# ── the store ────────────────────────────────────────────────────────────────


def test_store_set_get_names_delete() -> None:
    store = CredentialStore()
    store.set("OPENAI_API_KEY", "sk-secret")
    store.set("ANTHROPIC_API_KEY", "sk-ant")
    assert store.names() == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY"]  # sorted
    assert store.get("OPENAI_API_KEY") == "sk-secret"
    assert store.get("missing") is None
    assert store.delete("OPENAI_API_KEY") is True
    assert store.delete("OPENAI_API_KEY") is False  # idempotent
    assert store.names() == ["ANTHROPIC_API_KEY"]


def test_store_rejects_bad_names() -> None:
    store = CredentialStore()
    for bad in ("has space", "1leading-digit", "semi;colon", ""):
        with pytest.raises(InvalidCredentialName):
            store.set(bad, "x")


def test_store_persists_0600(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    CredentialStore(path).set("KEY", "sekret")
    # A fresh store rehydrates from disk…
    assert CredentialStore(path).get("KEY") == "sekret"
    # …the value IS on disk (local, user's domain) but the file is user-only (0600).
    assert "sekret" in json.loads(path.read_text())["credentials"]["KEY"]
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_store_ignores_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "credentials.json"
    path.write_text("{ not json")
    assert CredentialStore(path).names() == []  # recoverable, not fatal


# ── resolution (store first, then env) ───────────────────────────────────────


def test_resolve_prefers_store_then_env(monkeypatch: pytest.MonkeyPatch) -> None:
    store = CredentialStore()
    store.set("FROM_STORE", "store-val")
    assert resolve_credential("secret://FROM_STORE", store) == "store-val"

    monkeypatch.setenv("FROM_ENV", "env-val")
    assert resolve_credential("secret://FROM_ENV", store) == "env-val"  # store miss → env

    # store shadows env when both exist
    store.set("BOTH", "store-wins")
    monkeypatch.setenv("BOTH", "env-loses")
    assert resolve_credential("secret://BOTH", store) == "store-wins"


def test_resolve_requires_scheme_and_errors_when_missing() -> None:
    assert resolve_credential(None) is None
    with pytest.raises(CredentialResolutionError):
        resolve_credential("env:OPENAI_API_KEY")  # the old, wrong placeholder form
    with pytest.raises(CredentialResolutionError):
        resolve_credential("secret://NOWHERE", CredentialStore())


# ── the /admin/credentials wire (values never leave) ─────────────────────────


def _client() -> TestClient:
    return TestClient(create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False))


def test_credentials_routes_write_only() -> None:
    with _client() as c:
        # empty to start
        assert c.get("/admin/credentials").json() == {"credentials": []}

        # PUT a value → stored; the response carries name + hasValue, never the value
        r = c.put("/admin/credentials/OPENAI_API_KEY", json={"value": "sk-topsecret"})
        assert r.status_code == 200
        assert r.json() == {"name": "OPENAI_API_KEY", "hasValue": True}

        # LIST returns names + hasValue only — the secret value is nowhere in the payload
        listing = c.get("/admin/credentials")
        assert listing.json() == {"credentials": [{"name": "OPENAI_API_KEY", "hasValue": True}]}
        assert "sk-topsecret" not in listing.text

        # DELETE removes it; deleting again is a 404
        assert c.delete("/admin/credentials/OPENAI_API_KEY").status_code == 204
        assert c.get("/admin/credentials").json() == {"credentials": []}
        assert c.delete("/admin/credentials/OPENAI_API_KEY").status_code == 404


def test_credentials_routes_reject_bad_input() -> None:
    with _client() as c:
        assert c.put("/admin/credentials/GOOD", json={"value": ""}).status_code == 422
        assert c.put("/admin/credentials/GOOD", json={}).status_code == 422
        r = c.put("/admin/credentials/bad name", json={"value": "x"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "invalid_credential"


def test_put_credential_is_resolvable_by_the_app_store() -> None:
    # A credential added over the wire resolves for a reachable binding's secret:// ref — end to
    # end through the SAME store the data plane uses (app.state.credential_store).
    app = create_app(launcher=FakeUpstreamLauncher(), enable_reaper=False)
    with TestClient(app) as c:
        c.put("/admin/credentials/MY_KEY", json={"value": "resolved-secret"})
        store = app.state.credential_store
        assert resolve_credential("secret://MY_KEY", store) == "resolved-secret"
