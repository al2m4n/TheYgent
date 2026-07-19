"""The identity layer's contract: setup lockout, login, roles, API keys, users, providers.

What must hold: a fresh install is CLOSED until /auth/setup creates the first admin; every
credential is an opaque bearer stored hashed; roles gate by floor (viewer < editor < admin)
with the settings/users/transfer surfaces admin-only; API keys never widen (≤ owner at mint,
meet-narrowed at use); the last active admin can never be demoted/disabled/deleted; chat
sessions are per-account; and the OIDC flow provisions/links accounts per provider policy.
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any
from urllib.parse import parse_qs, urlsplit

import _auth
import asyncpg
import pytest
from _auth import ADMIN_PASSWORD, ADMIN_USERNAME
from _db import plain_dsn
from _fake_idp import FakeIdp
from _ir import trivial_ir
from fastapi.testclient import TestClient

# Overrides the auto-attached admin default header on a per-request basis (httpx merges
# per-request headers over client defaults; an empty value parses to "no bearer").
_NO_AUTH = {"Authorization": ""}


def _mk_user(client: TestClient, username: str, role: str, password: str = "user-pass-123") -> str:
    resp = client.post("/users", json={"username": username, "password": password, "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _login(client: TestClient, username: str, password: str = "user-pass-123") -> str:
    resp = client.post("/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _sql(pg_url: str, query: str, *args: Any) -> None:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        await conn.execute(query, *args)
    finally:
        await conn.close()


# ── first-run setup + lockout ─────────────────────────────────────────────────


def test_fresh_install_is_closed_until_setup(client: TestClient, pg_url: str) -> None:
    # Rewind to the pre-setup state: no accounts (cascade kills sessions/keys too).
    asyncio.run(_sql(pg_url, "DELETE FROM user_account"))
    _auth.reset_cache()

    status = client.get("/auth/status", headers=_NO_AUTH)
    assert status.status_code == 200  # the boot probe itself is never gated
    assert status.json()["setup_required"] is True

    # Management surface: closed without AND with the (now-dead) suite bearer.
    assert client.get("/agents", headers=_NO_AUTH).status_code == 401
    assert client.get("/agents").status_code == 401

    created = client.post(
        "/auth/setup",
        json={"username": "First.Admin", "password": "first-admin-pw", "display_name": "First"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["token"].startswith("tys_")
    assert body["user"]["role"] == "admin"
    assert body["user"]["username"] == "first.admin"  # case-folded handle
    assert "password" not in body["user"] and "password_hash" not in body["user"]

    # The minted session opens the API; a second setup is a hard 409.
    assert client.get("/agents", headers=_bearer(body["token"])).status_code == 200
    again = client.post("/auth/setup", json={"username": "other", "password": "other-pw-12345"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "setup_already_complete"


def test_setup_validates_username_and_password(client: TestClient, pg_url: str) -> None:
    asyncio.run(_sql(pg_url, "DELETE FROM user_account"))
    _auth.reset_cache()
    bad_name = client.post("/auth/setup", json={"username": "!!", "password": "long-enough-1"})
    assert bad_name.status_code == 400
    assert bad_name.json()["error"]["code"] == "invalid_username"
    short = client.post("/auth/setup", json={"username": "ok-name", "password": "short"})
    assert short.status_code == 400
    assert short.json()["error"]["code"] == "password_too_short"


# ── login ─────────────────────────────────────────────────────────────────────


def test_login_success_wrong_password_and_unknown_user(client: TestClient) -> None:
    token = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert token.startswith("tys_")

    wrong = client.post(
        "/auth/login", json={"username": ADMIN_USERNAME, "password": "not-the-password"}
    )
    unknown = client.post(
        "/auth/login", json={"username": "nobody-here", "password": "not-the-password"}
    )
    # One indistinguishable answer for both miss modes.
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["code"] == unknown.json()["error"]["code"] == "invalid_credentials"


def test_login_is_username_case_insensitive(client: TestClient) -> None:
    token = _login(client, ADMIN_USERNAME.upper(), ADMIN_PASSWORD)
    assert token.startswith("tys_")


def test_disabled_account_cannot_login_and_loses_live_sessions(client: TestClient) -> None:
    user_id = _mk_user(client, "temp-editor", "editor")
    token = _login(client, "temp-editor")
    assert client.get("/auth/me", headers=_bearer(token)).status_code == 200

    assert client.patch(f"/users/{user_id}", json={"disabled": True}).status_code == 200
    # The live session died with the disable; a fresh login is refused with its own code.
    assert client.get("/auth/me", headers=_bearer(token)).status_code == 401
    relogin = client.post(
        "/auth/login", json={"username": "temp-editor", "password": "user-pass-123"}
    )
    assert relogin.status_code == 403
    assert relogin.json()["error"]["code"] == "account_disabled"


def test_expired_session_is_rejected(client: TestClient, pg_url: str) -> None:
    token = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    digest = hashlib.sha256(token.encode()).hexdigest()
    asyncio.run(
        _sql(
            pg_url,
            "UPDATE auth_session SET expires_at = now() - interval '1 hour' WHERE token_hash = $1",
            digest,
        )
    )
    assert client.get("/auth/me", headers=_bearer(token)).status_code == 401


def test_logout_revokes_the_presented_session(client: TestClient) -> None:
    token = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    assert client.post("/auth/logout", headers=_bearer(token)).status_code == 204
    assert client.get("/auth/me", headers=_bearer(token)).status_code == 401


# ── /auth/me + password change ───────────────────────────────────────────────


def test_me_reports_user_role_and_credential(client: TestClient) -> None:
    me = client.get("/auth/me").json()
    assert me["user"]["username"] == ADMIN_USERNAME
    assert me["role"] == "admin"
    assert me["credential"] == "session"

    minted = client.post("/auth/api-keys", json={"name": "probe"}).json()
    via_key = client.get("/auth/me", headers=_bearer(minted["token"])).json()
    assert via_key["credential"] == "api_key"


def test_display_name_update(client: TestClient) -> None:
    updated = client.patch("/auth/me", json={"display_name": "Renamed"})
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Renamed"


def test_password_change_requires_current_and_revokes_other_sessions(
    client: TestClient,
) -> None:
    _mk_user(client, "casey", "viewer")
    first = _login(client, "casey")
    second = _login(client, "casey")

    bad = client.post(
        "/auth/me/password",
        json={"current_password": "wrong", "new_password": "new-pass-abc-1"},
        headers=_bearer(first),
    )
    assert bad.status_code == 403

    ok = client.post(
        "/auth/me/password",
        json={"current_password": "user-pass-123", "new_password": "new-pass-abc-1"},
        headers=_bearer(first),
    )
    assert ok.status_code == 200
    # The changing session survives; every other session died; the new password logs in.
    assert client.get("/auth/me", headers=_bearer(first)).status_code == 200
    assert client.get("/auth/me", headers=_bearer(second)).status_code == 401
    assert (
        client.post(
            "/auth/login", json={"username": "casey", "password": "new-pass-abc-1"}
        ).status_code
        == 200
    )


def test_password_change_refused_for_api_key_credential(client: TestClient) -> None:
    minted = client.post("/auth/api-keys", json={"name": "k"}).json()
    resp = client.post(
        "/auth/me/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "whatever-123"},
        headers=_bearer(minted["token"]),
    )
    assert resp.status_code == 403


# ── the role floor matrix ─────────────────────────────────────────────────────


def test_role_floors_across_the_surface(client: TestClient) -> None:
    _mk_user(client, "vera", "viewer")
    _mk_user(client, "eddy", "editor")
    viewer = _bearer(_login(client, "vera"))
    editor = _bearer(_login(client, "eddy"))

    # Viewer: uses agents — reads the registry, runs models — but builds nothing.
    assert client.get("/agents", headers=viewer).status_code == 200
    assert client.get("/stats", headers=viewer).status_code == 200
    denied = client.post("/drafts", json={"ir": {"nodes": []}}, headers=viewer)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "forbidden"
    assert client.get("/drafts", headers=viewer).status_code == 403
    assert client.get("/connections", headers=viewer).status_code == 403
    assert client.get("/settings", headers=viewer).status_code == 403
    assert client.post("/export", json={"include": ["agents"]}, headers=viewer).status_code == 403

    # Editor: builds agents; still not the install owner.
    assert client.post("/drafts", json={"ir": {"nodes": []}}, headers=editor).status_code == 201
    assert client.get("/connections", headers=editor).status_code == 200
    assert client.get("/settings", headers=editor).status_code == 403
    assert client.get("/users", headers=editor).status_code == 403
    assert client.post("/export", json={"include": ["agents"]}, headers=editor).status_code == 403

    # Admin: everything, including the surfaces above.
    assert client.get("/settings").status_code == 200
    assert client.get("/users").status_code == 200


def test_viewer_can_run_a_published_agent(client: TestClient) -> None:
    assert client.post("/agents", json={"ir": trivial_ir()}).status_code == 201
    _mk_user(client, "runner", "viewer")
    viewer = _bearer(_login(client, "runner"))
    run = client.post(
        "/agents/agt_01J9X8TRIVIAL/runs", json={"input": "hi", "stream": False}, headers=viewer
    )
    assert run.status_code == 200
    assert run.json()["status"] == "completed"
    # ...but cannot submit arbitrary inline IR (that is the builders' surface).
    assert (
        client.post(
            "/graphs/runs", json={"ir": trivial_ir(), "input": "hi"}, headers=viewer
        ).status_code
        == 403
    )


# ── API keys ──────────────────────────────────────────────────────────────────


def test_api_key_mint_list_use_revoke(client: TestClient) -> None:
    minted = client.post("/auth/api-keys", json={"name": "ci"}).json()
    token, key = minted["token"], minted["api_key"]
    assert token.startswith("tyk_")
    assert key["token_prefix"] == token[:12]
    assert key["role"] == "admin"

    listed = client.get("/auth/api-keys").json()["api_keys"]
    assert [k["id"] for k in listed] == [key["id"]]
    assert all("token" not in k and "token_hash" not in k for k in listed)

    assert client.get("/agents", headers=_bearer(token)).status_code == 200
    assert client.delete(f"/auth/api-keys/{key['id']}").status_code == 204
    assert client.get("/agents", headers=_bearer(token)).status_code == 401


def test_api_key_role_cannot_exceed_owner(client: TestClient) -> None:
    _mk_user(client, "vika", "viewer")
    viewer = _bearer(_login(client, "vika"))
    too_wide = client.post(
        "/auth/api-keys", json={"name": "sneaky", "role": "admin"}, headers=viewer
    )
    assert too_wide.status_code == 400
    scoped = client.post("/auth/api-keys", json={"name": "ok"}, headers=viewer)
    assert scoped.status_code == 201
    assert scoped.json()["api_key"]["role"] == "viewer"


def test_demoting_the_owner_narrows_their_keys(client: TestClient) -> None:
    user_id = _mk_user(client, "shift", "editor")
    editor = _bearer(_login(client, "shift"))
    minted = client.post("/auth/api-keys", json={"name": "deploy"}, headers=editor).json()
    key_auth = _bearer(minted["token"])
    assert client.post("/drafts", json={"ir": {"nodes": []}}, headers=key_auth).status_code == 201

    assert client.patch(f"/users/{user_id}", json={"role": "viewer"}).status_code == 200
    # Same key, same stored role — the effective authority is the meet with the CURRENT owner.
    assert client.post("/drafts", json={"ir": {"nodes": []}}, headers=key_auth).status_code == 403
    assert client.get("/agents", headers=key_auth).status_code == 200


def test_cannot_revoke_another_users_key(client: TestClient) -> None:
    _mk_user(client, "kim", "editor")
    kim = _bearer(_login(client, "kim"))
    admin_key = client.post("/auth/api-keys", json={"name": "mine"}).json()["api_key"]
    denied = client.delete(f"/auth/api-keys/{admin_key['id']}", headers=kim)
    assert denied.status_code == 404  # another account's key id looks nonexistent


def test_invoke_accepts_api_keys(client: TestClient) -> None:
    # The unattended surface: closed with no credential (no THEYGENT_INVOKE_TOKEN in the test
    # app), open to a minted API key.
    assert client.post("/agents", json={"ir": trivial_ir()}).status_code == 201
    no_auth = client.post(
        "/agents/agt_01J9X8TRIVIAL/invoke", json={"input": "hi"}, headers=_NO_AUTH
    )
    assert no_auth.status_code == 401
    session_bearer = client.post("/agents/agt_01J9X8TRIVIAL/invoke", json={"input": "hi"})
    assert session_bearer.status_code == 401  # interactive sessions don't open /invoke
    key = client.post("/auth/api-keys", json={"name": "external-fe"}).json()["token"]
    ok = client.post("/agents/agt_01J9X8TRIVIAL/invoke", json={"input": "hi"}, headers=_bearer(key))
    assert ok.status_code == 200
    assert ok.json()["status"] == "completed"


# ── user administration ───────────────────────────────────────────────────────


def test_user_crud_and_username_conflict(client: TestClient) -> None:
    user_id = _mk_user(client, "dana", "editor")
    dup = client.post(
        "/users", json={"username": "DANA", "password": "whatever-123", "role": "viewer"}
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "username_taken"

    users = client.get("/users").json()["users"]
    assert {u["username"] for u in users} == {ADMIN_USERNAME, "dana"}

    patched = client.patch(f"/users/{user_id}", json={"role": "admin"})
    assert patched.status_code == 200
    assert patched.json()["role"] == "admin"

    assert client.delete(f"/users/{user_id}").status_code == 204
    assert client.delete(f"/users/{user_id}").status_code == 404


def test_admin_can_create_sso_only_account(client: TestClient) -> None:
    resp = client.post(
        "/users", json={"username": "sso-only", "role": "viewer", "email": "S@Corp.example"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["has_password"] is False
    assert body["email"] == "s@corp.example"  # stored lowercased
    # No password → password login is a clean invalid_credentials, not a 500.
    login = client.post("/auth/login", json={"username": "sso-only", "password": "anything-1"})
    assert login.status_code == 401


def test_last_admin_guards(client: TestClient) -> None:
    admin_id = next(
        u["id"] for u in client.get("/users").json()["users"] if u["username"] == ADMIN_USERNAME
    )
    for attempt in (
        client.patch(f"/users/{admin_id}", json={"role": "viewer"}),
        client.patch(f"/users/{admin_id}", json={"disabled": True}),
        client.delete(f"/users/{admin_id}"),
    ):
        assert attempt.status_code == 409
        assert attempt.json()["error"]["code"] == "last_admin"

    # With a second active admin the same operations go through.
    _mk_user(client, "backup-admin", "admin")
    assert client.patch(f"/users/{admin_id}", json={"role": "viewer"}).status_code == 200


def test_admin_password_reset_revokes_target_sessions(client: TestClient) -> None:
    user_id = _mk_user(client, "misplaced", "viewer")
    token = _login(client, "misplaced")
    reset = client.post(f"/users/{user_id}/password", json={"password": "fresh-pass-999"})
    assert reset.status_code == 200
    assert client.get("/auth/me", headers=_bearer(token)).status_code == 401
    assert (
        client.post(
            "/auth/login", json={"username": "misplaced", "password": "fresh-pass-999"}
        ).status_code
        == 200
    )


# ── chat-session ownership ────────────────────────────────────────────────────


def test_sessions_are_per_account(client: TestClient, pg_url: str) -> None:
    _mk_user(client, "ann", "viewer")
    _mk_user(client, "bob", "viewer")
    ann, bob = _bearer(_login(client, "ann")), _bearer(_login(client, "bob"))

    created = client.post("/sessions", json={"metadata": {"title": "ann's"}}, headers=ann)
    assert created.status_code == 201
    sid = created.json()["id"]

    # Ann sees it; Bob neither lists nor reads nor deletes it (404, not 403 — no id oracle).
    assert sid in [s["id"] for s in client.get("/sessions", headers=ann).json()["sessions"]]
    assert sid not in [s["id"] for s in client.get("/sessions", headers=bob).json()["sessions"]]
    assert client.get(f"/sessions/{sid}", headers=bob).status_code == 404
    assert client.delete(f"/sessions/{sid}", headers=bob).status_code == 404
    # The admin operates the install: sees and may manage every conversation.
    assert client.get(f"/sessions/{sid}").status_code == 200

    # A pre-auth (ownerless) row stays visible to everyone — history predates accounts.
    asyncio.run(
        _sql(
            pg_url,
            "INSERT INTO chat_session (id, created_at, updated_at) VALUES ($1, now(), now())",
            "legacy-session",
        )
    )
    assert "legacy-session" in [
        s["id"] for s in client.get("/sessions", headers=bob).json()["sessions"]
    ]
    assert client.get("/sessions/legacy-session", headers=bob).status_code == 200


def test_runs_record_their_starter(client: TestClient) -> None:
    _mk_user(client, "starter", "viewer")
    viewer = _bearer(_login(client, "starter"))
    run = client.post(
        "/runs", json={"input": "hi", "model": "triage-fast", "stream": False}, headers=viewer
    )
    assert run.status_code == 200
    run_id = run.json()["runId"]
    me = client.get("/auth/me", headers=viewer).json()["user"]["id"]
    assert client.get(f"/runs/{run_id}").json()["user_id"] == me


def test_runs_are_per_account(client: TestClient, pg_url: str) -> None:
    # Run output is conversation content — the same ownership boundary as /sessions must hold,
    # or the answer text /sessions denies would leak through the sibling /runs surface.
    _mk_user(client, "ann", "viewer")
    _mk_user(client, "bob", "viewer")
    ann, bob = _bearer(_login(client, "ann")), _bearer(_login(client, "bob"))

    made = client.post(
        "/runs", json={"input": "secret", "model": "triage-fast", "stream": False}, headers=ann
    )
    run_id = made.json()["runId"]

    # Ann lists+reads it; Bob neither lists nor reads it (404, not 403 — no id-probing oracle).
    assert run_id in [r["id"] for r in client.get("/runs", headers=ann).json()["runs"]]
    assert run_id not in [r["id"] for r in client.get("/runs", headers=bob).json()["runs"]]
    assert client.get(f"/runs/{run_id}", headers=bob).status_code == 404
    # The admin operates the install: sees and reads every run.
    assert client.get(f"/runs/{run_id}").status_code == 200

    # A pre-auth (ownerless) run stays visible to everyone — history predates accounts.
    asyncio.run(
        _sql(
            pg_url,
            "INSERT INTO run (id, status, model, created_at, updated_at) "
            "VALUES ($1, 'completed', 'm', now(), now())",
            "legacy-run",
        )
    )
    assert "legacy-run" in [r["id"] for r in client.get("/runs", headers=bob).json()["runs"]]
    assert client.get("/runs/legacy-run", headers=bob).status_code == 200


def test_resume_rejects_a_non_owner(client: TestClient, pg_url: str) -> None:
    # A waiting run's resume DELIVERS attacker-controlled input into its human gate — a non-owner
    # must not even learn the run exists (404), let alone drive it. Non-durable mode is fine for
    # this check: the ownership gate runs before the durable-mode gate is reached.
    _mk_user(client, "owner", "editor")
    _mk_user(client, "intruder", "editor")
    owner_id = client.get("/auth/me", headers=_bearer(_login(client, "owner"))).json()["user"]["id"]
    intruder = _bearer(_login(client, "intruder"))
    # Seed a waiting run owned by `owner` directly (a live human gate needs durable mode).
    asyncio.run(
        _sql(
            pg_url,
            "INSERT INTO run (id, status, model, user_id, awaiting_node, runtime, "
            "created_at, updated_at) VALUES ($1, 'waiting', 'm', $2, 'n_h', 'durable', "
            "now(), now())",
            "waiting-run",
            owner_id,
        )
    )
    # The intruder cannot even see it — 404, indistinguishable from a nonexistent run.
    assert (
        client.post("/runs/waiting-run/resume", json={"input": "x"}, headers=intruder).status_code
        == 404
    )


# ── sign-in providers + the OIDC flow ────────────────────────────────────────


def _provider_config(idp: FakeIdp, **overrides: Any) -> dict[str, Any]:
    config: dict[str, Any] = {
        "authorization_endpoint": f"{idp.url}/authorize",
        "token_endpoint": f"{idp.url}/token",
        "userinfo_endpoint": f"{idp.url}/userinfo",
        "client_id": "cid-123",
    }
    config.update(overrides)
    return config


def _create_provider(client: TestClient, idp: FakeIdp, **overrides: Any) -> dict[str, Any]:
    resp = client.post(
        "/auth/providers",
        json={
            "name": "Fake IdP",
            "slug": "fake",
            "config": _provider_config(idp, **overrides),
            "client_secret": "csec-topsecret",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _oidc_roundtrip(client: TestClient, slug: str = "fake") -> str:
    """Drive start → (pretend the IdP redirected back) → callback; returns the fragment. The
    TestClient carries the browser-binding cookie from /start to /callback automatically, so a
    normal flow completes; error paths return an ``error=…`` fragment."""
    start = client.get(
        f"/auth/oidc/{slug}/start",
        params={"return_to": "http://localhost:5174/auth/callback"},
        follow_redirects=False,
        headers=_NO_AUTH,
    )
    assert start.status_code == 302, start.text
    query = parse_qs(urlsplit(start.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"]  # PKCE actually engaged
    state = query["state"][0]
    callback = client.get(
        "/auth/oidc/callback",
        params={"code": "authcode-1", "state": state},
        follow_redirects=False,
        headers=_NO_AUTH,
    )
    assert callback.status_code == 302, callback.text
    location = callback.headers["location"]
    assert location.startswith("http://localhost:5174/auth/callback#")
    return location.split("#", 1)[1]


def _oidc_signin(client: TestClient, slug: str = "fake") -> str:
    """The full happy-path sign-in: roundtrip → redeem the one-time code → session bearer. The
    bearer never rides a redirect URL, so the callback fragment carries only a short-lived code."""
    fragment = _oidc_roundtrip(client, slug)
    assert fragment.startswith("code="), fragment
    code = fragment.removeprefix("code=")
    resp = client.post("/auth/oidc/exchange", json={"code": code}, headers=_NO_AUTH)
    assert resp.status_code == 200, resp.text
    token = resp.json()["token"]
    assert token.startswith("tys_")
    return token


def test_provider_admin_surface_and_secret_redaction(client: TestClient) -> None:
    with FakeIdp() as idp:
        provider = _create_provider(client, idp)
        assert provider["has_client_secret"] is True
        assert "csec-topsecret" not in provider and "client_secret" not in provider

        listed = client.get("/auth/providers").json()["providers"]
        assert "csec-topsecret" not in str(listed)

        # The public surface (login page) sees slug+name only, and only while enabled.
        public = client.get("/auth/status", headers=_NO_AUTH).json()["providers"]
        assert public == [{"slug": "fake", "name": "Fake IdP"}]
        assert (
            client.patch(f"/auth/providers/{provider['id']}", json={"enabled": False}).status_code
            == 200
        )
        assert client.get("/auth/status", headers=_NO_AUTH).json()["providers"] == []

        dup = client.post(
            "/auth/providers",
            json={"name": "x", "slug": "fake", "config": _provider_config(idp)},
        )
        assert dup.status_code == 409

        invalid = client.post(
            "/auth/providers", json={"name": "y", "slug": "half", "config": {"client_id": "z"}}
        )
        assert invalid.status_code == 400
        assert invalid.json()["error"]["code"] == "invalid_provider"

        _mk_user(client, "plain-editor", "editor")
        editor = _bearer(_login(client, "plain-editor"))
        assert client.get("/auth/providers", headers=editor).status_code == 403


def test_oidc_login_provisions_links_and_reuses_the_account(client: TestClient) -> None:
    with FakeIdp() as idp:
        _create_provider(client, idp, default_role="editor")

        token = _oidc_signin(client)
        me = client.get("/auth/me", headers=_bearer(token)).json()
        assert me["user"]["username"] == "casey"
        assert me["user"]["role"] == "editor"
        assert me["user"]["has_password"] is False

        # The exchange really carried PKCE + the stored client secret to the IdP.
        form = idp.captured["token_form"]
        assert form["client_secret"] == "csec-topsecret"
        assert form["code_verifier"]
        assert idp.captured["userinfo_auth"] == "Bearer at-fake-123"

        # Same subject again → the SAME account, not a duplicate.
        second = _oidc_signin(client)
        assert (
            client.get("/auth/me", headers=_bearer(second)).json()["user"]["id"] == me["user"]["id"]
        )
        users = client.get("/users").json()["users"]
        assert len([u for u in users if u["username"] == "casey"]) == 1


def test_oidc_exchange_code_is_short_lived_and_bearer_stays_out_of_the_url(
    client: TestClient,
) -> None:
    with FakeIdp() as idp:
        _create_provider(client, idp)
        fragment = _oidc_roundtrip(client)
        # The long-lived bearer NEVER appears in the redirect fragment — only a one-time code.
        assert fragment.startswith("code=")
        assert "tys_" not in fragment
        code = fragment.removeprefix("code=")
        # A garbage/forged code is a clean 401, never a 500.
        bad = client.post("/auth/oidc/exchange", json={"code": "not-a-real-code"}, headers=_NO_AUTH)
        assert bad.status_code == 401
        # The real code redeems for the session bearer.
        ok = client.post("/auth/oidc/exchange", json={"code": code}, headers=_NO_AUTH)
        assert ok.status_code == 200
        assert ok.json()["token"].startswith("tys_")


def test_oidc_sign_in_captures_and_refreshes_the_avatar(client: TestClient) -> None:
    # The provider's userinfo picture becomes the account avatar, and refreshes on later sign-ins;
    # a non-http(s) value is dropped (never rendered as an <img src>).
    with FakeIdp() as idp:
        _create_provider(client, idp)
        me = client.get("/auth/me", headers=_bearer(_oidc_signin(client))).json()["user"]
        assert me["avatar_url"] == "https://cdn.example.com/casey.png"

        # A later sign-in with a new picture updates it.
        idp.claims = {**idp.claims, "picture": "https://cdn.example.com/casey-v2.png"}
        me2 = client.get("/auth/me", headers=_bearer(_oidc_signin(client))).json()["user"]
        assert me2["id"] == me["id"]
        assert me2["avatar_url"] == "https://cdn.example.com/casey-v2.png"

        # A non-http(s) picture is ignored (not stored) — the previous URL survives.
        idp.claims = {**idp.claims, "picture": "javascript:alert(1)"}
        me3 = client.get("/auth/me", headers=_bearer(_oidc_signin(client))).json()["user"]
        assert me3["avatar_url"] == "https://cdn.example.com/casey-v2.png"


def test_local_accounts_have_no_avatar(client: TestClient) -> None:
    me = client.get("/auth/me").json()["user"]  # the suite admin, a password account
    assert me["avatar_url"] is None


def test_oidc_undecryptable_client_secret_is_a_clean_bounce_not_a_500(
    client: TestClient, pg_url: str
) -> None:
    # If the stored client secret can't be decrypted (e.g. THEYGENT_SECRET_KEY was unset — an
    # ephemeral key — and the process restarted), the callback must bounce with a clear error,
    # never crash to a 500. Simulate it by corrupting the ciphertext of the only secret row.
    with FakeIdp() as idp:
        _create_provider(client, idp)
        asyncio.run(_sql(pg_url, "UPDATE secret SET ciphertext = 'not-a-valid-fernet-token'"))
        assert _oidc_roundtrip(client) == "error=provider_secret_unreadable"


def test_oidc_callback_requires_the_browser_binding_cookie(client: TestClient) -> None:
    # Login-CSRF defense: a valid (state, code) captured from one browser cannot be replayed
    # into another — the second browser carries no matching binding cookie.
    with FakeIdp() as idp:
        _create_provider(client, idp)
        start = client.get(
            "/auth/oidc/fake/start",
            params={"return_to": "http://localhost:5174/auth/callback"},
            follow_redirects=False,
            headers=_NO_AUTH,
        )
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        # Drop the binding cookie /start set — this is now "a different browser" replaying the
        # captured (state, code). The auth bearer is carried in the header, not the cookie jar,
        # so clearing cookies only removes the OIDC binding.
        client.cookies.clear()
        forged = client.get(
            "/auth/oidc/callback",
            params={"code": "authcode-1", "state": state},
            follow_redirects=False,
            headers=_NO_AUTH,
        )
        assert forged.status_code == 400
        assert forged.json()["error"]["code"] == "invalid_oidc_state"


def test_oidc_does_not_let_a_second_provider_claim_an_active_sso_account(
    client: TestClient,
) -> None:
    # The account-takeover fix: once an SSO account is active (has a linked identity), a SECOND
    # provider asserting the same email must NOT link to it — it provisions a fresh account.
    with FakeIdp() as idp_a, FakeIdp() as idp_b:
        _create_provider(client, idp_a, default_role="admin")  # strict provider "fake"
        # A second provider on a different slug, asserting the SAME email but a different subject.
        resp = client.post(
            "/auth/providers",
            json={
                "name": "Other",
                "slug": "other",
                "config": _provider_config(idp_b),
                "client_secret": "csec-2",
            },
        )
        assert resp.status_code == 201
        idp_b.claims = {"sub": "attacker-sub", "email": "casey@example.com", "email_verified": True}

        first = client.get("/auth/me", headers=_bearer(_oidc_signin(client, "fake"))).json()["user"]
        assert first["role"] == "admin"  # the legit account, provisioned at the admin default role

        # Attacker signs in via provider B with the same email → a DISTINCT account, not a claim
        # of the active admin account.
        second = client.get("/auth/me", headers=_bearer(_oidc_signin(client, "other"))).json()[
            "user"
        ]
        assert second["id"] != first["id"]
        assert second["role"] == "viewer"  # provider B's default — no privilege inherited


def test_oidc_absent_email_verified_does_not_link_to_a_preprovisioned_account(
    client: TestClient,
) -> None:
    # A lenient (plain-OAuth2) provider that omits email_verified must NOT link by email — the
    # pre-provisioned account is left untouched and a new account is provisioned instead.
    with FakeIdp() as idp:
        _create_provider(client, idp)
        idp.claims = {"sub": "sub-x", "email": "casey@example.com", "login": "casey"}  # no verified
        created = client.post(
            "/users",
            json={"username": "casey-corp", "role": "editor", "email": "casey@example.com"},
        )
        me = client.get("/auth/me", headers=_bearer(_oidc_signin(client))).json()["user"]
        assert me["id"] != created.json()["id"]
        assert me["role"] == "viewer"  # freshly provisioned, not the editor pre-provisioned account


def test_oidc_domain_allowlist_and_no_provision(client: TestClient) -> None:
    with FakeIdp() as idp:
        provider = _create_provider(client, idp, allowed_domains=["corp.example"])
        assert _oidc_roundtrip(client) == "error=domain_not_allowed"

        assert (
            client.patch(
                f"/auth/providers/{provider['id']}",
                json={"config": _provider_config(idp, auto_provision=False)},
            ).status_code
            == 200
        )
        assert _oidc_roundtrip(client) == "error=account_not_provisioned"


def test_oidc_allowlist_accepts_a_full_email_not_just_a_domain(client: TestClient) -> None:
    # The personal-instance case: allow ONE specific address, not the whole domain. Listing the
    # exact address lets that person in; a DIFFERENT address in the same domain is denied
    # (proving it's an exact-email allow, not a domain-wide allow that would let anyone at
    # example.com in). The two are distinct subjects so neither reuses the other's linked
    # identity (an existing identity isn't re-gated by the allowlist).
    with FakeIdp() as idp:
        _create_provider(client, idp, allowed_domains=["casey@example.com"])
        token = _oidc_signin(client)  # casey@example.com — exact-email allow → succeeds
        assert client.get("/auth/me", headers=_bearer(token)).json()["user"]["username"] == "casey"

        # A different person at the same domain, never seen before → denied by the exact-email list.
        idp.claims = {
            "sub": "intruder-sub",
            "email": "intruder@example.com",
            "email_verified": True,
            "name": "Intruder",
        }
        assert _oidc_roundtrip(client) == "error=domain_not_allowed"


def test_oidc_links_precreated_sso_account_by_email(client: TestClient) -> None:
    with FakeIdp() as idp:
        _create_provider(client, idp)
        created = client.post(
            "/users",
            json={"username": "casey-corp", "role": "editor", "email": "casey@example.com"},
        )
        assert created.status_code == 201
        token = _oidc_signin(client)
        me = client.get("/auth/me", headers=_bearer(token)).json()["user"]
        # Linked to the admin-provisioned account (verified email, no password, no prior
        # identity), not a new one.
        assert me["id"] == created.json()["id"]
        assert me["role"] == "editor"


def test_oidc_start_rejects_foreign_return_to(client: TestClient) -> None:
    with FakeIdp() as idp:
        _create_provider(client, idp)
        resp = client.get(
            "/auth/oidc/fake/start",
            params={"return_to": "https://evil.example/steal"},
            follow_redirects=False,
            headers=_NO_AUTH,
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_return_to"


def test_oidc_callback_rejects_forged_state(client: TestClient) -> None:
    resp = client.get(
        "/auth/oidc/callback",
        params={"code": "x", "state": "forged-state"},
        follow_redirects=False,
        headers=_NO_AUTH,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_oidc_state"


# ── token hygiene ─────────────────────────────────────────────────────────────


def test_tokens_are_stored_hashed(client: TestClient, pg_url: str) -> None:
    token = _login(client, ADMIN_USERNAME, ADMIN_PASSWORD)
    key = client.post("/auth/api-keys", json={"name": "hygiene"}).json()["token"]

    async def _dump() -> str:
        conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
        try:
            sessions = await conn.fetch("SELECT token_hash FROM auth_session")
            keys = await conn.fetch("SELECT token_hash, token_prefix FROM api_key")
            return str([dict(r) for r in sessions] + [dict(r) for r in keys])
        finally:
            await conn.close()

    stored = asyncio.run(_dump())
    assert token not in stored and key not in stored
    assert hashlib.sha256(token.encode()).hexdigest() in stored


@pytest.mark.parametrize("header", [None, "Bearer tys_forged", "Basic abc"])
def test_bad_credentials_are_a_clean_401(client: TestClient, header: str | None) -> None:
    headers = {"Authorization": header} if header is not None else _NO_AUTH
    resp = client.get("/agents", headers=headers)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"
