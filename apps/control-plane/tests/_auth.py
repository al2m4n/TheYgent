"""Auth bootstrap for tests — every client authenticates through the REAL endpoints.

The API is closed until an admin exists (the identity layer's whole point), so the suite's
clients must sign in like any other caller: ``POST /auth/setup`` on a fresh DB, ``POST
/auth/login`` afterwards. ``attach`` does that and installs the bearer as the client's
default header; conftest hooks it into ``TestClient.__enter__`` so the ~90 existing
client-construction sites keep working unchanged — nothing is bypassed, the tests simply
authenticate (as admin, the superset role; role-specific tests mint their own users).

The token is cached per test (scrypt is deliberately slow — re-hashing on every client
would tax the whole suite); conftest's truncate fixture resets the cache alongside the DB.
"""

from __future__ import annotations

from typing import Any

ADMIN_USERNAME = "suite-admin"
ADMIN_PASSWORD = "suite-admin-pw-1"

# One cached bearer per truncate cycle (all clients in a test share the same Postgres).
_cached_token: str | None = None


def reset_cache() -> None:
    global _cached_token
    _cached_token = None


def bootstrap_token(client: Any) -> str:
    """Setup-or-login as the suite admin, via the real endpoints. Returns the bearer."""
    global _cached_token
    if _cached_token is not None:
        return _cached_token
    resp = client.post(
        "/auth/setup",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "display_name": "Suite"},
    )
    if resp.status_code != 201:
        resp = client.post(
            "/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
        )
        assert resp.status_code == 200, f"suite login failed: {resp.status_code} {resp.text}"
    _cached_token = resp.json()["token"]
    return _cached_token


def attach(client: Any) -> Any:
    """Authenticate ``client`` and install the bearer as its default Authorization header."""
    token = bootstrap_token(client)
    client.headers["Authorization"] = f"Bearer {token}"
    return client


async def attach_async(client: Any) -> Any:
    """The httpx.AsyncClient variant (dispatcher/durable tests drive the ASGI app async)."""
    global _cached_token
    if _cached_token is None:
        resp = await client.post(
            "/auth/setup",
            json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD, "display_name": "Suite"},
        )
        if resp.status_code != 201:
            resp = await client.post(
                "/auth/login", json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD}
            )
            assert resp.status_code == 200, f"suite login failed: {resp.status_code} {resp.text}"
        _cached_token = resp.json()["token"]
    client.headers["Authorization"] = f"Bearer {_cached_token}"
    return client
