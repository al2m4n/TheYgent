"""The MCP hub surface: connection auth schemes, the interactive-authorization broker + token
storage, and the catalog/install + connection-ops routes.

The registry half runs against fixture payloads captured from the real registries (no network);
the connection-ops half spawns the REAL fake stdio MCP server through a connection — the same
genuine-protocol posture as the rest of the MCP suite. Guards:

* Every declared auth scheme resolves into the transport config server-side — the secret shows
  up exactly where the scheme says (env / headers / Authorization), and never anywhere else.
* An authorize flow can only start from a user-opened session; anywhere else it fails fast with
  an actionable message instead of hanging a run on a browser that will never open.
* Token storage lives behind the connection's ONE secret_ref: authorizing, refreshing, and
  re-authorizing never create a second ref (the contentHash-stability rule).
* A hub install creates a connection whose config carries the launch shape + origin and whose
  secret-flagged inputs land in the encrypted store — never in the connection row.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from theygent_control_plane import db
from theygent_control_plane.app import create_app
from theygent_control_plane.mcp import McpConnectionError
from theygent_control_plane.mcp.oauth import DbTokenStorage, OAuthBroker
from theygent_control_plane.mcp.registry import McpRegistryClient, RegistryInfo
from theygent_control_plane.secrets import SecretStore
from theygent_control_plane.store import ConnectionStore
from theygent_control_plane.walker import ResolvedConnection, mcp_config_from_connection

_FAKE_SERVER = str(Path(__file__).parent / "_fake_mcp_server.py")
_FIXTURES = Path(__file__).parent / "fixtures" / "mcp_registry"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / f"{name}.json").read_text())


# ── mcp_config_from_connection: every auth scheme lands where its transport expects ───────────


def _conn(config: dict[str, Any], secret: str | None = None) -> ResolvedConnection:
    return ResolvedConnection(config=config, secret=secret, enabled=True)


async def test_http_secret_without_auth_block_stays_bearer() -> None:
    # The pre-auth-block default: a bare secret on an http connection is a bearer token.
    cfg = await mcp_config_from_connection(
        _conn({"transport": "http", "url": "http://s.test/mcp"}, secret="tok")
    )
    assert cfg.transport == "http"
    assert cfg.headers == {"Authorization": "Bearer tok"}


async def test_http_auth_schemes_resolve_to_headers() -> None:
    bearer = await mcp_config_from_connection(
        _conn({"transport": "http", "url": "u", "auth": {"type": "bearer"}}, secret="t")
    )
    assert bearer.headers == {"Authorization": "Bearer t"}

    api_key = await mcp_config_from_connection(
        _conn(
            {"transport": "http", "url": "u", "auth": {"type": "api_key", "header": "X-K"}},
            secret="k",
        )
    )
    assert api_key.headers == {"X-K": "k"}

    basic = await mcp_config_from_connection(
        _conn(
            {"transport": "http", "url": "u", "auth": {"type": "basic", "username": "u1"}},
            secret="p",
        )
    )
    assert basic.headers == {"Authorization": "Basic " + base64.b64encode(b"u1:p").decode("ascii")}

    header_map = await mcp_config_from_connection(
        _conn(
            {"transport": "http", "url": "u", "auth": {"type": "headers"}},
            secret=json.dumps({"X-A": "1", "X-B": "2"}),
        )
    )
    assert header_map.headers == {"X-A": "1", "X-B": "2"}


async def test_http_headers_map_malformed_secret_is_loud() -> None:
    with pytest.raises(ValueError, match="JSON object map"):
        await mcp_config_from_connection(
            _conn(
                {"transport": "http", "url": "u", "auth": {"type": "headers"}},
                secret="not-json",
            )
        )


async def test_stdio_secret_env_single_and_map() -> None:
    single = await mcp_config_from_connection(
        _conn(
            {"transport": "stdio", "command": "c", "secretEnv": "API_KEY", "env": {"A": "1"}},
            secret="shh",
        )
    )
    assert single.env == {"A": "1", "API_KEY": "shh"}

    mapped = await mcp_config_from_connection(
        _conn(
            {"transport": "stdio", "command": "c", "auth": {"type": "env"}, "env": {"A": "1"}},
            secret=json.dumps({"K1": "v1", "K2": "v2"}),
        )
    )
    assert mapped.env == {"A": "1", "K1": "v1", "K2": "v2"}


async def test_sse_and_generated_transports_pass_through() -> None:
    sse = await mcp_config_from_connection(
        _conn({"transport": "sse", "url": "http://s.test/sse", "auth": {"type": "bearer"}}, "t")
    )
    assert sse.transport == "sse" and sse.headers == {"Authorization": "Bearer t"}

    spec = {"openapi": "3.1.0", "info": {"title": "x", "version": "1"}, "paths": {}}
    openapi = await mcp_config_from_connection(
        _conn(
            {
                "transport": "openapi",
                "spec": spec,
                "url": "http://api.test",
                "include": ["/a*"],
                "auth": {"type": "api_key", "header": "X-K"},
            },
            secret="k",
        )
    )
    assert openapi.transport == "openapi"
    assert openapi.spec == spec
    assert openapi.url == "http://api.test"
    # Flat knobs normalize into options so the generated builders read ONE place.
    assert openapi.options["include"] == ["/a*"]
    assert openapi.headers == {"X-K": "k"}

    graphql = await mcp_config_from_connection(
        _conn({"transport": "graphql", "url": "http://g.test", "allowMutations": True})
    )
    assert graphql.transport == "graphql"
    assert graphql.options["allowMutations"] is True


async def test_oauth_scheme_stamps_connection_id_not_headers() -> None:
    cfg = await mcp_config_from_connection(
        _conn({"transport": "http", "url": "http://s.test/mcp", "auth": {"type": "oauth"}}),
        connection_id="con_42",
    )
    # The token never rides the config: the factory attaches the auth hook by connection id.
    assert cfg.oauth_connection == "con_42"
    assert not cfg.headers


# ── the authorize broker: interactive only inside a user-opened session ───────────────────────


async def test_redirect_outside_an_authorize_session_fails_fast() -> None:
    broker = OAuthBroker("http://localhost:8080/mcp/oauth/callback")
    redirect, _callback = broker.handlers("con_1")
    with pytest.raises(McpConnectionError, match="interactive authorization"):
        await redirect("https://as.example/authorize?state=abc")


async def test_start_surfaces_the_authorization_url_and_callback_completes_it() -> None:
    broker = OAuthBroker("http://localhost:8080/mcp/oauth/callback")
    seen: dict[str, Any] = {}

    async def connect() -> None:
        # What the SDK provider does on a 401: hand over the browser URL, await the code.
        redirect, callback = broker.handlers("con_1")
        await redirect("https://as.example/authorize?client_id=x&state=st-123")
        seen["code"] = await callback()

    result = await broker.start("con_1", connect)
    assert result["status"] == "pending"
    assert "state=st-123" in result["authorizationUrl"]
    assert broker.pending("con_1")

    assert broker.callback("st-123", "the-code", None) is True
    # The connect task finishes once the code arrives; the session is cleaned up.
    for _ in range(50):
        if not broker.pending("con_1"):
            break
        await _tick()
    assert seen["code"] == ("the-code", "st-123")
    assert not broker.pending("con_1")
    assert broker.last_error("con_1") is None


async def test_start_with_valid_tokens_reports_authorized_without_a_browser() -> None:
    broker = OAuthBroker("http://localhost:8080/mcp/oauth/callback")

    async def connect() -> None:  # stored tokens were good — no redirect happens
        return None

    assert await broker.start("con_1", connect) == {"status": "authorized"}


async def test_callback_with_unknown_state_is_rejected() -> None:
    broker = OAuthBroker("http://localhost:8080/mcp/oauth/callback")
    assert broker.callback("never-issued", "code", None) is False


async def _tick() -> None:
    import asyncio

    await asyncio.sleep(0.02)


# ── token storage: one secret_ref, write-through, tolerant reads ───────────────────────────────


async def test_token_storage_round_trips_behind_one_secret_ref(pg_url: str) -> None:
    engine = db.create_engine(pg_url)
    try:
        sessionmaker = db.create_sessionmaker(engine)
        connections = ConnectionStore()
        secrets = SecretStore.from_keys([Fernet.generate_key().decode()])
        async with sessionmaker() as session, session.begin():
            conn = await connections.create(
                session,
                name="oauth-server",
                kind="mcp_server",
                config={"transport": "http", "url": "http://s.test/mcp", "auth": {"type": "oauth"}},
                secret_ref=None,
                enabled=True,
            )
        storage = DbTokenStorage(sessionmaker, connections, secrets, conn.id)

        assert await storage.get_tokens() is None  # not yet authorized reads clean

        await storage.set_tokens(OAuthToken(access_token="a1", token_type="Bearer"))
        tokens = await storage.get_tokens()
        assert tokens is not None and tokens.access_token == "a1"
        async with sessionmaker() as session:
            ref_after_tokens = (await connections.get(session, conn.id)).secret_ref
        assert ref_after_tokens is not None

        # Client info merges into the SAME blob; a refresh rewrites tokens in place. Neither
        # creates a second ref — rotation must keep every referencing agent's hash stable.
        await storage.set_client_info(
            OAuthClientInformationFull(client_id="cid", redirect_uris=None)
        )
        await storage.set_tokens(OAuthToken(access_token="a2", token_type="Bearer"))
        async with sessionmaker() as session:
            assert (await connections.get(session, conn.id)).secret_ref == ref_after_tokens
        refreshed = await storage.get_tokens()
        assert refreshed is not None and refreshed.access_token == "a2"
        info = await storage.get_client_info()
        assert info is not None and info.client_id == "cid"
    finally:
        await engine.dispose()


# ── the hub routes: registries, catalog, install, preview, connection ops ─────────────────────


def _registry_transport() -> httpx.MockTransport:
    details = {
        "com.pulsemcp/remote-filesystem": _fixture("npm_filesystem"),
        "io.github.github/github-mcp-server": _fixture("github_mcp_server"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if path == "/v0.1/servers":
            return httpx.Response(200, json=_fixture("official_list"))
        for name, payload in details.items():
            if path == f"/v0.1/servers/{name}/versions/latest":
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"detail": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def hub_client(  # type: ignore[no-untyped-def]
    fake_inference, pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    # Pin the launcher-availability probe: the install route 400s when a stdio launch
    # command is missing on the HOST, and these tests must not depend on whether the
    # machine running them happens to carry node/uv. The dedicated launcher test
    # re-patches this to exercise the unavailable path.
    monkeypatch.setattr(
        "theygent_control_plane.app._stdio_launcher_available", lambda command: True
    )
    registry = McpRegistryClient(
        [RegistryInfo(id="official", label="Official MCP Registry", url="http://reg.test")],
        http_factory=lambda: httpx.AsyncClient(transport=_registry_transport()),
    )
    app = create_app(
        inference_base_url=fake_inference.v1_url,
        database_url=pg_url,
        secret_key=[Fernet.generate_key().decode()],
        mcp_registry_client=registry,
    )
    with TestClient(app) as client:
        yield client


def test_registries_endpoint_lists_configured_hubs(hub_client: TestClient) -> None:
    body = hub_client.get("/admin/mcp/registries").json()
    assert [r["id"] for r in body["registries"]] == ["official"]
    assert body["registries"][0]["url"] == "http://reg.test"


def test_catalog_lists_entries_with_cursor(hub_client: TestClient) -> None:
    body = hub_client.get("/admin/mcp/catalog", params={"registry": "official"}).json()
    assert body["entries"], "fixture page should map to entries"
    first = body["entries"][0]
    assert first["registry"] == "official"
    assert first["installed"] is False
    assert "nextCursor" in body


def test_install_stdio_candidate_routes_secrets_to_the_store(hub_client: TestClient) -> None:
    detail = hub_client.get(
        "/admin/mcp/catalog/entry",
        params={"registry": "official", "name": "com.pulsemcp/remote-filesystem"},
    ).json()
    stdio = next(c for c in detail["candidates"] if c["kind"] == "stdio")
    required = [i["name"] for i in stdio["inputs"] if i["required"]]
    secret_names = [i["name"] for i in stdio["inputs"] if i["secret"]]
    assert "GCS_BUCKET" in required
    assert "GCS_PRIVATE_KEY" in secret_names

    values = {"GCS_BUCKET": "my-bucket", "GCS_PRIVATE_KEY": "top-secret"}
    created = hub_client.post(
        "/admin/mcp/catalog/install",
        json={
            "registry": "official",
            "name": "com.pulsemcp/remote-filesystem",
            "version": "latest",
            "candidateId": stdio["id"],
            "connectionName": "gcs-files",
            "values": values,
        },
    )
    assert created.status_code == 201, created.text
    conn = created.json()
    assert conn["kind"] == "mcp_server"
    assert conn["hasSecret"] is True  # the private key went to the encrypted store
    cfg = conn["config"]
    assert cfg["transport"] == "stdio"
    assert cfg["env"].get("GCS_BUCKET") == "my-bucket"
    assert "GCS_PRIVATE_KEY" not in json.dumps(cfg)  # never in the row
    assert cfg["origin"]["name"] == "com.pulsemcp/remote-filesystem"

    # The catalog now marks it installed (origin cross-reference on the detail view; the
    # fixture list page is a different slice of the registry and need not contain this entry).
    detail_after = hub_client.get(
        "/admin/mcp/catalog/entry",
        params={"registry": "official", "name": "com.pulsemcp/remote-filesystem"},
    ).json()
    assert detail_after["entry"]["installed"] is True
    assert detail_after["entry"]["installedAs"] == "gcs-files"
    assert detail_after["entry"]["installedConnection"] == conn["id"]


def test_install_remote_with_oauth_skips_static_secrets(hub_client: TestClient) -> None:
    detail = hub_client.get(
        "/admin/mcp/catalog/entry",
        params={"registry": "official", "name": "io.github.github/github-mcp-server"},
    ).json()
    remote = next(c for c in detail["candidates"] if c["kind"] in ("http", "sse"))
    assert remote["supportsOauth"] is True
    created = hub_client.post(
        "/admin/mcp/catalog/install",
        json={
            "registry": "official",
            "name": "io.github.github/github-mcp-server",
            "candidateId": remote["id"],
            "connectionName": "gh-remote",
            "values": {},
            "useOauth": True,
        },
    )
    assert created.status_code == 201, created.text
    conn = created.json()
    assert conn["config"]["auth"] == {"type": "oauth"}
    assert conn["hasSecret"] is False  # tokens arrive later, via the authorize flow


def test_install_stdio_without_its_launcher_is_a_clear_400(
    hub_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stdio install whose launch command can't run HERE (a container image without
    docker/dnx, a host without node) must fail AT INSTALL with an actionable error — never
    201-then-err-on-every-run."""
    monkeypatch.setattr(
        "theygent_control_plane.app._stdio_launcher_available", lambda command: False
    )
    detail = hub_client.get(
        "/admin/mcp/catalog/entry",
        params={"registry": "official", "name": "com.pulsemcp/remote-filesystem"},
    ).json()
    stdio = next(c for c in detail["candidates"] if c["kind"] == "stdio")
    resp = hub_client.post(
        "/admin/mcp/catalog/install",
        json={
            "registry": "official",
            "name": "com.pulsemcp/remote-filesystem",
            "candidateId": stdio["id"],
            "connectionName": "gcs-files",
            "values": {"GCS_BUCKET": "my-bucket", "GCS_PRIVATE_KEY": "top-secret"},
        },
    )
    assert resp.status_code == 400, resp.text
    err = resp.json()["error"]
    assert err["code"] == "stdio_launcher_unavailable"
    # The message must name the missing command and the alternatives that always work.
    assert "npx" in err["message"]
    assert "http/sse" in err["message"] and "openapi/graphql" in err["message"]
    # …and nothing was persisted: the entry still shows as not installed.
    detail_after = hub_client.get(
        "/admin/mcp/catalog/entry",
        params={"registry": "official", "name": "com.pulsemcp/remote-filesystem"},
    ).json()
    assert detail_after["entry"]["installed"] is False


def test_install_missing_required_input_is_a_client_error(hub_client: TestClient) -> None:
    detail = hub_client.get(
        "/admin/mcp/catalog/entry",
        params={"registry": "official", "name": "com.pulsemcp/remote-filesystem"},
    ).json()
    stdio = next(c for c in detail["candidates"] if c["kind"] == "stdio")
    resp = hub_client.post(
        "/admin/mcp/catalog/install",
        json={
            "registry": "official",
            "name": "com.pulsemcp/remote-filesystem",
            "candidateId": stdio["id"],
            "connectionName": "gcs-files",
            "values": {},
        },
    )
    assert resp.status_code == 400
    assert "GCS_BUCKET" in resp.json()["error"]["message"]


def test_put_admin_server_rejects_generated_transports(hub_client: TestClient) -> None:
    resp = hub_client.put(
        "/admin/mcp/servers/gen",
        json={"transport": "openapi", "url": "http://api.test", "spec": {"openapi": "3.1.0"}},
    )
    assert resp.status_code == 400
    assert "connection" in resp.json()["error"]["message"]


def test_preview_openapi_inline_spec_lists_tools(hub_client: TestClient) -> None:
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "servers": [{"url": "http://api.test"}],
        "paths": {
            "/things": {
                "get": {"operationId": "listThings", "responses": {"200": {"description": "ok"}}}
            }
        },
    }
    resp = hub_client.post("/admin/mcp/generated:preview", json={"kind": "openapi", "spec": spec})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [t["name"] for t in body["tools"]] == ["listThings"]
    assert body["url"] == "http://api.test"  # derived from the spec's servers block


def test_preview_graphql_requires_a_url(hub_client: TestClient) -> None:
    resp = hub_client.post("/admin/mcp/generated:preview", json={"kind": "graphql"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_spec"


def test_connection_ops_probe_warm_close_against_a_real_server(hub_client: TestClient) -> None:
    created = hub_client.post(
        "/connections",
        json={
            "name": "fake-stdio",
            "kind": "mcp_server",
            "config": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [_FAKE_SERVER],
            },
        },
    )
    assert created.status_code == 201, created.text
    conn_id = created.json()["id"]

    tools = hub_client.get(f"/connections/{conn_id}/mcp/tools")
    assert tools.status_code == 200, tools.text
    names = {t["name"] for t in tools.json()["tools"]}
    assert {"echo", "boom", "pid"} <= names

    warmed = hub_client.post(f"/connections/{conn_id}/mcp:warm").json()
    assert warmed == {"id": conn_id, "connected": True}
    closed = hub_client.post(f"/connections/{conn_id}/mcp:close").json()
    assert closed == {"id": conn_id, "connected": False}

    missing = hub_client.get("/connections/con_nope/mcp/tools")
    assert missing.status_code == 404


def test_oauth_start_rejects_non_oauth_connections(hub_client: TestClient) -> None:
    created = hub_client.post(
        "/connections",
        json={
            "name": "plain-http",
            "kind": "mcp_server",
            "config": {"transport": "http", "url": "http://s.test/mcp"},
        },
    )
    conn_id = created.json()["id"]
    resp = hub_client.post(f"/connections/{conn_id}/mcp-oauth:start")
    assert resp.status_code == 400
    assert "oauth" in resp.json()["error"]["message"]


def test_oauth_status_and_unknown_callback_page(hub_client: TestClient) -> None:
    created = hub_client.post(
        "/connections",
        json={
            "name": "oauth-http",
            "kind": "mcp_server",
            "config": {
                "transport": "http",
                "url": "http://s.test/mcp",
                "auth": {"type": "oauth"},
            },
        },
    )
    conn_id = created.json()["id"]
    status = hub_client.get(f"/connections/{conn_id}/mcp-oauth").json()
    assert status["authorized"] is False
    assert status["pending"] is False

    page = hub_client.get("/mcp/oauth/callback", params={"state": "never-issued"})
    assert page.status_code == 400
    assert "Unknown or expired" in page.text
