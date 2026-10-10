"""Gateway keys, the key set gateways sync, credential profiles, and the gateway's token check.

What must hold:

* developers mint and revoke their own ``tyg_`` keys; admins see and revoke everyone's;
* gateway and service keys never open the management API, and personal-key endpoints never
  show or touch them;
* the key set holds every live gateway key of an enabled account — digests only — with its
  developer's credential profile;
* its version moves with every change to that set (mint, revoke, an account disabled or
  re-enabled or deleted, a profile change), and only then, so a gateway re-fetches exactly when
  it must; ``304`` when the gateway already holds the version;
* the token check answers only for a session or a personal key.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import asyncpg
import pytest
from _auth import ADMIN_USERNAME
from _db import plain_dsn
from _ir import trivial_ir
from fastapi.testclient import TestClient
from theygent_ir.gateway import GatewayKeySet, GatewayTokenCheckResult

PASSWORD = "user-pass-1234"


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_user(client: TestClient, username: str, role: str = "editor") -> tuple[str, str]:
    """A new account and a session bearer for it."""
    created = client.post("/users", json={"username": username, "password": PASSWORD, "role": role})
    assert created.status_code == 201, created.text
    login = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    return created.json()["id"], login.json()["token"]


def mint(client: TestClient, as_user: str | None = None, name: str = "laptop") -> dict[str, Any]:
    headers = bearer(as_user) if as_user else {}
    minted = client.post("/gateway/keys", json={"name": name}, headers=headers)
    assert minted.status_code == 201, minted.text
    return minted.json()


@pytest.fixture
def service_key(client: TestClient) -> str:
    minted = client.post("/gateway/service-keys", json={"name": "gw-prod"})
    assert minted.status_code == 201, minted.text
    token = minted.json()["token"]
    assert token.startswith("tygs_")
    return token


def key_set(client: TestClient, service_key: str, since: int | None = None) -> GatewayKeySet:
    params = {"since": str(since)} if since is not None else None
    response = client.get("/gateway/keys/sync", params=params, headers=bearer(service_key))
    assert response.status_code == 200, response.text
    return GatewayKeySet.model_validate_json(response.content)


def version(pg_url: str) -> int:
    async def read() -> int:
        conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
        try:
            return await conn.fetchval("SELECT key_set_version FROM ai_gateway_state WHERE id = 1")
        finally:
            await conn.close()

    return asyncio.run(read())


def sql(pg_url: str, query: str, *args: Any) -> None:
    async def run() -> None:
        conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
        try:
            await conn.execute(query, *args)
        finally:
            await conn.close()

    asyncio.run(run())


# ── developers' keys ─────────────────────────────────────────────────────────


def test_a_developer_mints_lists_and_revokes_their_own_keys(client: TestClient) -> None:
    _, dev = make_user(client, "dev", "viewer")  # any role may use the gateway
    minted = mint(client, dev)
    token, key = minted["token"], minted["gateway_key"]
    assert token.startswith("tyg_") and key["token_prefix"] == token[:12]
    assert "role" not in key and "token" not in key
    assert [
        k["id"] for k in client.get("/gateway/keys", headers=bearer(dev)).json()["gateway_keys"]
    ] == [key["id"]]
    assert client.delete(f"/gateway/keys/{key['id']}", headers=bearer(dev)).status_code == 204
    [revoked] = client.get("/gateway/keys", headers=bearer(dev)).json()["gateway_keys"]
    assert revoked["revoked_at"] is not None


def test_admins_see_and_revoke_everyones_keys(client: TestClient) -> None:
    _, ada = make_user(client, "ada")
    _, bob = make_user(client, "bob")
    ada_key = mint(client, ada)["gateway_key"]
    mint(client, bob)
    assert len(client.get("/gateway/keys", headers=bearer(ada)).json()["gateway_keys"]) == 1
    forbidden = client.get("/gateway/keys", params={"all": "true"}, headers=bearer(ada))
    assert forbidden.status_code == 403
    assert client.delete(f"/gateway/keys/{ada_key['id']}", headers=bearer(bob)).status_code == 404
    assert len(client.get("/gateway/keys", params={"all": "true"}).json()["gateway_keys"]) == 2
    assert client.delete(f"/gateway/keys/{ada_key['id']}").status_code == 204  # the admin


def test_personal_key_endpoints_never_show_or_touch_gateway_keys(client: TestClient) -> None:
    gateway_key = mint(client)["gateway_key"]
    assert client.get("/auth/api-keys").json()["api_keys"] == []
    assert client.delete(f"/auth/api-keys/{gateway_key['id']}").status_code == 404
    personal = client.post("/auth/api-keys", json={"name": "ci"}).json()["api_key"]
    assert client.delete(f"/gateway/keys/{personal['id']}").status_code == 404
    assert client.delete(f"/gateway/service-keys/{personal['id']}").status_code == 404


def test_gateway_and_service_keys_never_open_the_management_api(
    client: TestClient, service_key: str
) -> None:
    gateway_token = mint(client)["token"]
    assert client.post("/agents", json={"ir": trivial_ir()}).status_code == 201
    for token in (gateway_token, service_key):
        assert client.get("/agents", headers=bearer(token)).status_code == 401
        assert client.get("/gateway/keys", headers=bearer(token)).status_code == 401
        invoke = client.post(
            "/agents/agt_01J9X8TRIVIAL/invoke", json={"input": "hi"}, headers=bearer(token)
        )
        assert invoke.status_code == 401
    # Nor does a developer's gateway key open the service surface.
    assert client.get("/gateway/keys/sync", headers=bearer(gateway_token)).status_code == 401


def test_service_keys_are_admin_only(client: TestClient) -> None:
    _, editor = make_user(client, "eve")
    refused = client.post("/gateway/service-keys", json={"name": "x"}, headers=bearer(editor))
    assert refused.status_code == 403
    assert client.get("/gateway/service-keys", headers=bearer(editor)).status_code == 403
    minted = client.post("/gateway/service-keys", json={"name": "gw"}).json()["service_key"]
    assert [k["id"] for k in client.get("/gateway/service-keys").json()["service_keys"]] == [
        minted["id"]
    ]
    assert client.delete(f"/gateway/service-keys/{minted['id']}").status_code == 204


@pytest.mark.parametrize("path", ["/gateway/keys", "/gateway/service-keys"])
def test_a_key_needs_a_name(client: TestClient, path: str) -> None:
    assert client.post(path, json={"name": "  "}).status_code == 400


# ── the key set ──────────────────────────────────────────────────────────────


def test_the_key_set_holds_live_keys_of_enabled_accounts_as_digests(
    client: TestClient, service_key: str
) -> None:
    dev_id, dev = make_user(client, "dev")
    off_id, off = make_user(client, "off")
    live = mint(client, dev)
    revoked = mint(client, dev, "old")["gateway_key"]
    client.delete(f"/gateway/keys/{revoked['id']}", headers=bearer(dev))
    mint(client, off)
    assert client.patch(f"/users/{off_id}", json={"disabled": True}).status_code == 200
    client.post("/auth/api-keys", json={"name": "personal"})  # not a gateway key

    keys = key_set(client, service_key)
    [key] = keys.keys
    assert key.key_id == live["gateway_key"]["id"]
    assert key.token_sha256 == hashlib.sha256(live["token"].encode()).hexdigest()
    assert (key.developer_id, key.credential_profile) == (dev_id, "passthrough")
    assert live["token"] not in keys.model_dump_json()


def test_an_unchanged_key_set_is_not_sent_again(client: TestClient, service_key: str) -> None:
    mint(client)
    current = key_set(client, service_key)
    unchanged = client.get(
        "/gateway/keys/sync", params={"since": str(current.version)}, headers=bearer(service_key)
    )
    assert unchanged.status_code == 304
    assert key_set(client, service_key, since=current.version - 1).version == current.version


def test_only_a_service_key_syncs(client: TestClient) -> None:
    personal = client.post("/auth/api-keys", json={"name": "ci"}).json()["token"]
    for headers in ({"Authorization": ""}, bearer(personal), {}):
        assert client.get("/gateway/keys/sync", headers=headers).status_code == 401


def test_the_version_moves_with_every_change_to_the_key_set(
    client: TestClient, pg_url: str
) -> None:
    dev_id, dev = make_user(client, "dev")

    def moves(change: Any) -> bool:
        before = version(pg_url)
        change()
        return version(pg_url) > before

    key = {}
    assert moves(lambda: key.update(mint(client, dev)["gateway_key"]))
    assert moves(lambda: client.patch(f"/users/{dev_id}", json={"disabled": True}))
    assert moves(lambda: client.patch(f"/users/{dev_id}", json={"disabled": False}))
    assert moves(
        lambda: client.put(f"/gateway/developers/{dev_id}", json={"credential_profile": "org_key"})
    )
    # Disabling the account ended the developer's session, so the admin revokes.
    assert moves(lambda: client.delete(f"/gateway/keys/{key['id']}"))
    dev = client.post("/auth/login", json={"username": "dev", "password": PASSWORD}).json()["token"]
    mint(client, dev)
    assert moves(lambda: client.delete(f"/users/{dev_id}"))  # its keys go with it
    # Hand-run SQL counts too: the trigger, not the endpoint, moves the version.
    _, other = make_user(client, "other")
    other_key = mint(client, other)["gateway_key"]
    assert moves(
        lambda: sql(pg_url, "UPDATE api_key SET revoked_at = now() WHERE id = $1", other_key["id"])
    )


def test_the_version_stays_put_when_the_key_set_does_not_change(
    client: TestClient, pg_url: str
) -> None:
    user_id, _ = make_user(client, "dev")
    personal = client.post("/auth/api-keys", json={"name": "ci"}).json()
    before = version(pg_url)
    client.get("/agents", headers=bearer(personal["token"]))  # a personal key's use is touched
    sql(pg_url, "UPDATE api_key SET last_used_at = now() - interval '1 hour'")
    client.patch(f"/users/{user_id}", json={"role": "viewer"})  # gateway keys carry no role
    client.post("/auth/api-keys", json={"name": "another"})
    client.post("/gateway/service-keys", json={"name": "another gateway"})
    assert version(pg_url) == before


# ── credential profiles ──────────────────────────────────────────────────────


def test_admins_set_how_a_developer_is_billed(client: TestClient, service_key: str) -> None:
    dev_id, dev = make_user(client, "dev")
    mint(client, dev)
    developers = {d["username"]: d for d in client.get("/gateway/developers").json()["developers"]}
    assert developers["dev"]["credential_profile"] == "passthrough"
    assert developers["dev"]["live_gateway_keys"] == 1
    assert developers[ADMIN_USERNAME]["live_gateway_keys"] == 0

    updated = client.put(f"/gateway/developers/{dev_id}", json={"credential_profile": "org_key"})
    assert updated.json() == {"user_id": dev_id, "credential_profile": "org_key"}
    [key] = key_set(client, service_key).keys
    assert key.credential_profile == "org_key"
    developers = {d["username"]: d for d in client.get("/gateway/developers").json()["developers"]}
    assert developers["dev"]["credential_profile"] == "org_key"
    assert developers["dev"]["updated_by"] is not None


def test_profile_changes_are_admin_only_and_checked(client: TestClient) -> None:
    dev_id, dev = make_user(client, "dev")
    body = {"credential_profile": "org_key"}
    assert (
        client.put(f"/gateway/developers/{dev_id}", json=body, headers=bearer(dev)).status_code
        == 403
    )
    assert client.get("/gateway/developers", headers=bearer(dev)).status_code == 403
    missing = client.put("/gateway/developers/usr_nobody", json=body)
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "user_not_found")
    assert (
        client.put(f"/gateway/developers/{dev_id}", json={"credential_profile": "free"}).status_code
        == 422
    )


# ── the gateway's token check ────────────────────────────────────────────────


def check(client: TestClient, service_key: str, token: str) -> GatewayTokenCheckResult:
    response = client.post(
        "/gateway/introspect", json={"token": token}, headers=bearer(service_key)
    )
    assert response.status_code == 200, response.text
    return GatewayTokenCheckResult.model_validate_json(response.content)


def test_the_token_check_answers_for_sessions_and_personal_keys_only(
    client: TestClient, service_key: str, caplog: pytest.LogCaptureFixture
) -> None:
    editor_id, editor = make_user(client, "eve")
    personal = client.post("/auth/api-keys", json={"name": "ci"}, headers=bearer(editor)).json()
    gateway_token = mint(client, editor)["token"]
    with caplog.at_level(logging.DEBUG):
        assert check(client, service_key, editor) == GatewayTokenCheckResult(
            active=True, user_id=editor_id, role="editor"
        )
        assert check(client, service_key, personal["token"]).role == "editor"
        for inactive in (gateway_token, service_key, "tys_forged", "anything"):
            assert check(client, service_key, inactive) == GatewayTokenCheckResult(active=False)
        client.delete(f"/auth/api-keys/{personal['api_key']['id']}", headers=bearer(editor))
        assert check(client, service_key, personal["token"]).active is False
        client.patch(f"/users/{editor_id}", json={"disabled": True})
        assert check(client, service_key, editor).active is False
    for secret in (editor, personal["token"], gateway_token):
        assert secret not in caplog.text


def test_only_a_service_key_may_check_tokens(client: TestClient) -> None:
    session = client.headers["Authorization"].removeprefix("Bearer ")
    response = client.post("/gateway/introspect", json={"token": session})
    assert response.status_code == 401
