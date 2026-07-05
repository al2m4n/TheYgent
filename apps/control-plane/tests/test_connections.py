"""Tests for the connection/secret seam — the #1 hard-to-reverse decision in the
connection resource.

Two guards anchor everything that hangs on the connection resource:

* **The secret never reaches the wire or the connection row.** A connection is created with a
  write-only ``secret``; the response (and every read) exposes only ``hasSecret``, never the value
  or the ``secret_ref``. The ``secret`` table stores ciphertext, never plaintext (encrypted).
* **Rotating a secret keeps the SAME secret_ref.** So an IR that references the connection by id is
  untouched by a rotation — the contentHash-stability guarantee (proved end-to-end against a saved
  agent in the http-tool tests, once a node consumes the connection). Here we prove the store-level
  invariant: ``SecretStore.set`` re-encrypts in place under the same ref, and the connection row's
  ``secret_ref`` does not change across a PATCH rotation.
"""

from __future__ import annotations

import asyncio

import asyncpg
import pytest
from _db import plain_dsn
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from theygent_control_plane import db
from theygent_control_plane.app import create_app
from theygent_control_plane.secrets import SecretStore


@pytest.fixture
def client(fake_inference, pg_url: str) -> TestClient:  # type: ignore[no-untyped-def]
    # A fixed Fernet key so secrets stay decryptable across app instances in a single test (a
    # "restart" reuses the same key — the production posture; the ephemeral default is dev-only).
    key = Fernet.generate_key().decode()
    app = create_app(
        inference_base_url=fake_inference.v1_url, database_url=pg_url, secret_key=[key]
    )
    with TestClient(app) as test_client:
        yield test_client


async def _secret_rows(pg_url: str) -> list[dict[str, object]]:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        rows = await conn.fetch("SELECT id, ciphertext FROM secret")
        return [dict(r) for r in rows]
    finally:
        await conn.close()


async def _connection_secret_ref(pg_url: str, connection_id: str) -> str | None:
    conn = await asyncpg.connect(dsn=plain_dsn(pg_url))
    try:
        return await conn.fetchval("SELECT secret_ref FROM connection WHERE id = $1", connection_id)
    finally:
        await conn.close()


# ── connection CRUD + the secret-never-on-the-wire guard ─────────────────────────────────────────


def test_create_connection_returns_no_secret(client: TestClient, pg_url: str) -> None:
    resp = client.post(
        "/connections",
        json={
            "name": "CRM API",
            "kind": "http_auth",
            "config": {"baseUrl": "https://api.example.com", "auth": {"type": "bearer"}},
            "secret": "super-secret-token",
        },
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"].startswith("con_")
    assert body["kind"] == "http_auth"
    assert body["hasSecret"] is True
    # The secret value and the internal ref are NEVER on the wire.
    serialized = resp.text
    assert "super-secret-token" not in serialized
    assert "secret_ref" not in body
    assert "secretRef" not in serialized

    # The ``secret`` table stores ciphertext, never the plaintext (encrypted at rest).
    rows = asyncio.run(_secret_rows(pg_url))
    assert len(rows) == 1
    assert "super-secret-token" not in str(rows[0]["ciphertext"])


def test_connection_without_secret_has_no_secret(client: TestClient) -> None:
    resp = client.post(
        "/connections",
        json={"name": "public api", "kind": "http_auth", "config": {"baseUrl": "https://x"}},
    )
    assert resp.status_code == 201
    assert resp.json()["hasSecret"] is False


def test_list_and_get_connection_redact_secret(client: TestClient) -> None:
    cid = client.post(
        "/connections",
        json={"name": "c", "kind": "http_auth", "config": {}, "secret": "tok-XYZ"},
    ).json()["id"]

    listing = client.get("/connections")
    assert listing.status_code == 200
    assert "tok-XYZ" not in listing.text
    assert any(c["id"] == cid and c["hasSecret"] for c in listing.json()["connections"])

    one = client.get(f"/connections/{cid}")
    assert one.status_code == 200
    assert "tok-XYZ" not in one.text
    assert one.json()["hasSecret"] is True


def test_rotate_secret_keeps_same_secret_ref(client: TestClient, pg_url: str) -> None:
    cid = client.post(
        "/connections",
        json={"name": "c", "kind": "http_auth", "config": {}, "secret": "v1"},
    ).json()["id"]
    ref_before = asyncio.run(_connection_secret_ref(pg_url, cid))
    assert ref_before is not None

    # PATCH rotates the credential. The secret_ref MUST NOT change — that is what keeps an IR
    # referencing this connection (and therefore every agent's contentHash) stable across rotation.
    patched = client.patch(f"/connections/{cid}", json={"secret": "v2"})
    assert patched.status_code == 200
    assert patched.json()["hasSecret"] is True
    ref_after = asyncio.run(_connection_secret_ref(pg_url, cid))
    assert ref_after == ref_before

    # Still exactly one secret row (rotated in place, not duplicated); v1/v2 not in plaintext.
    rows = asyncio.run(_secret_rows(pg_url))
    assert len(rows) == 1
    assert "v1" not in str(rows[0]["ciphertext"])
    assert "v2" not in str(rows[0]["ciphertext"])


def test_patch_non_secret_config(client: TestClient) -> None:
    cid = client.post(
        "/connections", json={"name": "c", "kind": "http_auth", "config": {"baseUrl": "a"}}
    ).json()["id"]
    patched = client.patch(
        f"/connections/{cid}",
        json={"name": "renamed", "config": {"baseUrl": "b"}, "enabled": False},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["name"] == "renamed"
    assert body["config"]["baseUrl"] == "b"
    assert body["enabled"] is False


def test_delete_connection_removes_secret(client: TestClient, pg_url: str) -> None:
    cid = client.post(
        "/connections",
        json={"name": "c", "kind": "http_auth", "config": {}, "secret": "tok"},
    ).json()["id"]
    assert len(asyncio.run(_secret_rows(pg_url))) == 1

    assert client.delete(f"/connections/{cid}").status_code == 204
    assert client.get(f"/connections/{cid}").status_code == 404
    # The encrypted secret is deleted alongside the connection (no orphaned secret).
    assert asyncio.run(_secret_rows(pg_url)) == []


def test_unknown_connection_is_404(client: TestClient) -> None:
    assert client.get("/connections/con_nope").status_code == 404
    assert client.patch("/connections/con_nope", json={"enabled": False}).status_code == 404
    assert client.delete("/connections/con_nope").status_code == 404


# ── validation: secrets belong behind secret_ref; mcp_server config shape ─────────────────────────


def test_secret_in_config_is_rejected(client: TestClient) -> None:
    resp = client.post(
        "/connections",
        json={"name": "c", "kind": "http_auth", "config": {"apiKey": "leaked-in-config"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_connection"


def test_mcp_stdio_connection_requires_command(client: TestClient) -> None:
    resp = client.post(
        "/connections",
        json={"name": "m", "kind": "mcp_server", "config": {"transport": "stdio"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_connection"


def test_mcp_http_connection_requires_url(client: TestClient) -> None:
    resp = client.post(
        "/connections",
        json={"name": "m", "kind": "mcp_server", "config": {"transport": "http"}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_connection"


def test_mcp_stdio_connection_ok(client: TestClient) -> None:
    resp = client.post(
        "/connections",
        json={
            "name": "fs",
            "kind": "mcp_server",
            "config": {"transport": "stdio", "command": "python", "args": ["server.py"]},
        },
    )
    assert resp.status_code == 201
    assert resp.json()["kind"] == "mcp_server"


# ── the SecretStore at-rest round-trip + rotation (the encryption invariant) ─────────────────────


def test_secret_store_encrypts_and_rotates(pg_url: str) -> None:
    async def _run() -> None:
        key = Fernet.generate_key().decode()
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        store = SecretStore.from_keys([key])
        try:
            async with sm() as session, session.begin():
                ref = await store.create(session, "the-plaintext")
            # Stored ciphertext is NOT the plaintext.
            rows = await _secret_rows(pg_url)
            assert "the-plaintext" not in str(rows[0]["ciphertext"])
            # resolve round-trips to the original plaintext.
            async with sm() as session:
                assert await store.resolve(session, ref) == "the-plaintext"
            # Rotate in place under the same ref → new value, same ref.
            async with sm() as session, session.begin():
                await store.set(session, ref, "rotated")
            async with sm() as session:
                assert await store.resolve(session, ref) == "rotated"
            # An unknown / None ref resolves to None (an auth-less connection), never an error.
            async with sm() as session:
                assert await store.resolve(session, None) is None
                assert await store.resolve(session, "sec_missing") is None
        finally:
            await engine.dispose()

    asyncio.run(_run())


def test_secret_store_wrong_key_raises(pg_url: str) -> None:
    from theygent_control_plane.secrets import SecretDecryptError

    async def _run() -> None:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        try:
            store_a = SecretStore.from_keys([Fernet.generate_key().decode()])
            async with sm() as session, session.begin():
                ref = await store_a.create(session, "x")
            # A store with a different key cannot decrypt — fails honestly (→ tool err binding).
            store_b = SecretStore.from_keys([Fernet.generate_key().decode()])
            async with sm() as session:
                with pytest.raises(SecretDecryptError):
                    await store_b.resolve(session, ref)
        finally:
            await engine.dispose()

    asyncio.run(_run())
