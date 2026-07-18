"""Generated MCP servers (OpenAPI spec / GraphQL endpoint → in-process tools) + the app factory.

No Postgres, no Docker, no outside network: upstream APIs are ``httpx.MockTransport`` handlers
injected through the builders' test-only ``transport`` seam — the GraphQL one executes a REAL
schema via graphql-core, so introspection/validation/execution are exercised against genuine
responses, not canned strings. The one exception is the manager integration test, which serves
a real loopback upstream because the assembled factory deliberately has no injection seam.

Guards:
* OpenAPI tools are named from operationIds; calls hit the upstream WITH the configured auth
  headers and round-trip the response; include/exclude globs narrow the surface (start-anchored,
  so a glob never matches a path suffix); an invalid spec is a loud build error.
* GraphQL is the two-generic-tool shape: SDL introspection (cached), locally validated queries,
  mutations gated by ``allowMutations`` — invalid/blocked operations are tool errors the caller
  sees, and they never reach the upstream.
* ``GeneratedMcpClient`` keeps the actor guarantees: cached tool list, clean close (the actor
  task ends), calls after close raise ``McpConnectionError``.
* ``build_client_factory`` dispatches by transport and is loud about missing token machinery.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from graphql import build_schema, graphql_sync
from theygent_control_plane.mcp import (
    HttpMcpClient,
    McpConnectionError,
    McpManager,
    McpServerConfig,
    SseMcpClient,
    StdioMcpClient,
)
from theygent_control_plane.mcp.factory import build_client_factory
from theygent_control_plane.mcp.generated import (
    GeneratedMcpClient,
    GeneratedServerError,
    build_graphql_server,
    build_openapi_server,
    preview_tools,
)

# ── the OpenAPI upstream: a tiny 3.1 spec + a mock transport that implements it ───────────────


def _pets_spec() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "pets", "version": "1.0.0"},
        "paths": {
            "/pets": {
                "get": {
                    "operationId": "listPets",
                    "responses": {"200": {"description": "ok"}},
                },
                "post": {
                    "operationId": "createPet",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {"name": {"type": "string"}},
                                    "required": ["name"],
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "created"}},
                },
            },
            "/pets/{petId}": {
                "get": {
                    "operationId": "getPet",
                    "parameters": [
                        {
                            "name": "petId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"200": {"description": "ok"}},
                }
            },
            # Extra routes so the glob tests can prove narrowing (and start-anchoring:
            # "/internal/pets" must NOT be caught by an include glob of "/pets*").
            "/internal/pets": {
                "get": {
                    "operationId": "internalPets",
                    "responses": {"200": {"description": "ok"}},
                }
            },
            "/admin/reset": {
                "post": {
                    "operationId": "adminReset",
                    "responses": {"200": {"description": "ok"}},
                }
            },
        },
    }


_ALL_PET_TOOLS = {"listPets", "createPet", "getPet", "internalPets", "adminReset"}


def _pets_upstream(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path == "/pets":
            return httpx.Response(200, json=[{"id": "p1", "name": "rex"}])
        if request.method == "GET" and request.url.path.startswith("/pets/"):
            pet_id = request.url.path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"id": pet_id, "name": "rex"})
        if request.method == "POST" and request.url.path == "/pets":
            return httpx.Response(201, json={"id": "p2", **json.loads(request.content)})
        return httpx.Response(404, json={"error": "no such route"})

    return httpx.MockTransport(handler)


def _openapi_config(**options: Any) -> McpServerConfig:
    return McpServerConfig(
        transport="openapi",
        url="http://upstream.test",
        headers={"Authorization": "Bearer sekrit-token"},
        spec=_pets_spec(),
        options=options,
    )


# ── openapi: tool derivation, upstream calls, glob filtering, invalid specs ───────────────────


async def test_openapi_tools_are_named_from_operation_ids() -> None:
    tools = await preview_tools(_openapi_config())
    assert {t.name for t in tools} == _ALL_PET_TOOLS
    create = next(t for t in tools if t.name == "createPet")
    assert "name" in json.dumps(create.input_schema)  # requestBody surfaced as tool args


async def test_openapi_call_hits_upstream_with_auth_and_round_trips() -> None:
    seen: list[httpx.Request] = []
    client = GeneratedMcpClient(_openapi_config(), transport=_pets_upstream(seen))
    await client.connect()
    try:
        listed = await client.call_tool("listPets", {})
        assert listed.is_error is False
        assert json.loads(listed.value) == {"result": [{"id": "p1", "name": "rex"}]}
        assert seen[-1].headers["authorization"] == "Bearer sekrit-token"

        created = await client.call_tool("createPet", {"name": "momo"})
        assert created.is_error is False
        assert json.loads(created.value) == {"id": "p2", "name": "momo"}
        assert seen[-1].method == "POST"
        assert json.loads(seen[-1].content) == {"name": "momo"}

        got = await client.call_tool("getPet", {"petId": "p1"})
        assert json.loads(got.value) == {"id": "p1", "name": "rex"}
    finally:
        await client.close()


async def test_openapi_include_globs_keep_only_matching_routes() -> None:
    tools = await preview_tools(_openapi_config(include=["/pets*"]))
    # "/internal/pets" survives a suffix-matching glob translation — its absence proves the
    # patterns are start-anchored; "/admin/reset" simply matches no include glob.
    assert {t.name for t in tools} == {"listPets", "createPet", "getPet"}


async def test_openapi_exclude_globs_drop_matching_routes() -> None:
    tools = await preview_tools(_openapi_config(exclude=["/admin/*", "/internal/*"]))
    assert {t.name for t in tools} == {"listPets", "createPet", "getPet"}


def test_openapi_invalid_spec_is_a_loud_build_error() -> None:
    bad = McpServerConfig(
        transport="openapi",
        url="http://upstream.test",
        spec={"openapi": "3.1.0", "paths": "garbage"},
    )
    with pytest.raises(GeneratedServerError) as excinfo:
        build_openapi_server(bad)
    message = str(excinfo.value)
    assert "invalid OpenAPI spec" in message
    assert "paths" in message  # the underlying parse error is preserved, not swallowed

    with pytest.raises(GeneratedServerError, match="no parsed spec"):
        build_openapi_server(McpServerConfig(transport="openapi", url="http://upstream.test"))


async def test_builders_honor_timeout_seconds() -> None:
    _server, upstream = build_openapi_server(_openapi_config(timeoutSeconds=7.5))
    assert upstream.timeout == httpx.Timeout(7.5)
    await upstream.aclose()
    _server, upstream = build_graphql_server(
        McpServerConfig(transport="graphql", url="http://gql.test", options={"timeoutSeconds": 3})
    )
    assert upstream.timeout == httpx.Timeout(3.0)
    await upstream.aclose()


async def test_upstream_timeout_follows_the_injected_call_timeout() -> None:
    # With no options.timeoutSeconds the upstream httpx timeout must resolve from the SAME
    # call-timeout source the actor ceiling uses (the live-settings accessor), not the
    # env-only default — otherwise a raised call timeout never reaches the upstream client
    # and the actor waits on requests httpx already cancelled at the old ceiling.
    _server, upstream = build_openapi_server(_openapi_config(), call_timeout=lambda: 300.0)
    assert upstream.timeout == httpx.Timeout(300.0)
    await upstream.aclose()
    _server, upstream = build_graphql_server(
        McpServerConfig(transport="graphql", url="http://gql.test"), call_timeout=45.0
    )
    assert upstream.timeout == httpx.Timeout(45.0)
    await upstream.aclose()
    # An explicit options.timeoutSeconds still wins over the injected source.
    _server, upstream = build_openapi_server(
        _openapi_config(timeoutSeconds=7.5), call_timeout=lambda: 300.0
    )
    assert upstream.timeout == httpx.Timeout(7.5)
    await upstream.aclose()
    # And the client threads its own call-timeout source into the build, so a factory-built
    # generated server (which receives the accessor) resolves the upstream timeout from it.
    client = GeneratedMcpClient(_openapi_config(), call_timeout=lambda: 300.0)
    _server, upstream = client._build()
    assert upstream.timeout == httpx.Timeout(300.0)
    await upstream.aclose()


# ── the GraphQL upstream: a REAL schema executed by graphql-core inside the mock ──────────────

_GQL_SDL = """
type Query {
  hello(name: String): String
}

type Mutation {
  setGreeting(greeting: String!): String
}
"""


def _gql_upstream(seen: list[dict[str, Any]]) -> httpx.MockTransport:
    schema = build_schema(_GQL_SDL)
    root = {
        "hello": lambda info, name=None: f"hello {name or 'world'}",
        "setGreeting": lambda info, greeting: f"greeting is now {greeting}",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        result = graphql_sync(
            schema,
            payload["query"],
            root_value=root,
            variable_values=payload.get("variables"),
        )
        body: dict[str, Any] = {}
        if result.errors:
            body["errors"] = [error.formatted for error in result.errors]
        if result.data is not None:
            body["data"] = result.data
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _graphql_config(**options: Any) -> McpServerConfig:
    return McpServerConfig(
        transport="graphql",
        url="http://gql.test/graphql",
        headers={"Authorization": "Bearer gql-token"},
        options=options,
    )


async def _connect(
    config: McpServerConfig, transport: httpx.AsyncBaseTransport
) -> GeneratedMcpClient:
    client = GeneratedMcpClient(config, transport=transport)
    await client.connect()
    return client


async def test_graphql_exposes_exactly_the_two_generic_tools() -> None:
    seen: list[dict[str, Any]] = []
    client = await _connect(_graphql_config(), _gql_upstream(seen))
    try:
        assert {t.name for t in await client.list_tools()} == {"introspect_schema", "run_query"}
        assert seen == []  # listing needs no upstream round-trip
    finally:
        await client.close()


async def test_graphql_introspect_schema_returns_sdl_and_caches() -> None:
    seen: list[dict[str, Any]] = []
    client = await _connect(_graphql_config(), _gql_upstream(seen))
    try:
        result = await client.call_tool("introspect_schema", {})
        assert result.is_error is False
        assert "type Query" in result.value
        assert "hello(name: String): String" in result.value
        await client.call_tool("introspect_schema", {})
        assert len(seen) == 1  # the second call served from the cached schema
    finally:
        await client.close()


async def test_graphql_run_query_returns_data() -> None:
    seen: list[dict[str, Any]] = []
    client = await _connect(_graphql_config(), _gql_upstream(seen))
    try:
        result = await client.call_tool("run_query", {"query": '{ hello(name: "kit") }'})
        assert result.is_error is False
        assert json.loads(result.value) == {"hello": "hello kit"}

        result = await client.call_tool(
            "run_query",
            {"query": "query Q($n: String) { hello(name: $n) }", "variables": {"n": "ada"}},
        )
        assert json.loads(result.value) == {"hello": "hello ada"}
    finally:
        await client.close()


async def test_graphql_invalid_query_is_a_tool_error_and_never_goes_upstream() -> None:
    seen: list[dict[str, Any]] = []
    client = await _connect(_graphql_config(), _gql_upstream(seen))
    try:
        result = await client.call_tool("run_query", {"query": "{ nope }"})
        assert result.is_error is True
        assert "nope" in result.value  # the validation message names the bad field

        result = await client.call_tool("run_query", {"query": "query {{{"})
        assert result.is_error is True

        # Only the introspection request reached the upstream — never the bad queries.
        assert len(seen) == 1
    finally:
        await client.close()


async def test_graphql_mutation_is_blocked_by_default() -> None:
    seen: list[dict[str, Any]] = []
    client = await _connect(_graphql_config(), _gql_upstream(seen))
    try:
        result = await client.call_tool(
            "run_query", {"query": 'mutation { setGreeting(greeting: "hi") }'}
        )
        assert result.is_error is True
        assert "mutation" in result.value.lower()
        assert len(seen) == 1  # introspection only — the mutation never went upstream
    finally:
        await client.close()


async def test_graphql_mutation_executes_with_allow_mutations() -> None:
    seen: list[dict[str, Any]] = []
    client = await _connect(_graphql_config(allowMutations=True), _gql_upstream(seen))
    try:
        result = await client.call_tool(
            "run_query", {"query": 'mutation { setGreeting(greeting: "hi") }'}
        )
        assert result.is_error is False
        assert json.loads(result.value) == {"setGreeting": "greeting is now hi"}
    finally:
        await client.close()


# ── GeneratedMcpClient lifecycle (the actor guarantees) ───────────────────────────────────────


async def test_generated_client_lifecycle_and_close() -> None:
    seen: list[httpx.Request] = []
    client = GeneratedMcpClient(_openapi_config(), transport=_pets_upstream(seen))
    await client.connect()
    task = client._task
    assert task is not None and not task.done()

    first = await client.list_tools()
    second = await client.list_tools()
    assert [t.name for t in first] == [t.name for t in second]  # cached on connect
    assert seen == []  # listing never touched the upstream

    result = await client.call_tool("listPets", {})
    assert result.is_error is False

    await client.close()
    assert task.done()  # the actor task actually finished — nothing leaked
    assert client._task is None

    with pytest.raises(McpConnectionError):
        await client.call_tool("listPets", {})


async def test_preview_tools_leaves_no_running_tasks() -> None:
    before = {t for t in asyncio.all_tasks() if not t.done()}
    tools = await preview_tools(_openapi_config())
    assert {t.name for t in tools} == _ALL_PET_TOOLS
    lingering = {t for t in asyncio.all_tasks() if not t.done()} - before
    assert lingering == set()


# ── the app factory: dispatch + the oauth seam ────────────────────────────────────────────────


def test_factory_dispatches_by_transport() -> None:
    factory = build_client_factory()
    openapi = factory(McpServerConfig(transport="openapi", url="http://u.test", spec=_pets_spec()))
    assert isinstance(openapi, GeneratedMcpClient)
    graphql = factory(McpServerConfig(transport="graphql", url="http://u.test/graphql"))
    assert isinstance(graphql, GeneratedMcpClient)
    assert isinstance(
        factory(McpServerConfig(transport="http", url="http://u.test/mcp")), HttpMcpClient
    )
    assert isinstance(
        factory(McpServerConfig(transport="sse", url="http://u.test/sse")), SseMcpClient
    )
    assert isinstance(factory(McpServerConfig(transport="stdio", command="python")), StdioMcpClient)


def test_factory_oauth_connection_without_builder_is_loud() -> None:
    factory = build_client_factory()
    with pytest.raises(McpConnectionError, match="token machinery"):
        factory(
            McpServerConfig(transport="http", url="http://u.test/mcp", oauth_connection="con_1")
        )


def test_factory_oauth_builder_attaches_auth() -> None:
    class _StubAuth(httpx.Auth):
        pass

    stub = _StubAuth()
    minted: list[tuple[str, str]] = []

    def oauth(connection_id: str, server_url: str) -> httpx.Auth:
        minted.append((connection_id, server_url))
        return stub

    factory = build_client_factory(oauth=oauth)
    http = factory(
        McpServerConfig(transport="http", url="http://u.test/mcp", oauth_connection="con_9")
    )
    assert isinstance(http, HttpMcpClient)
    assert http._auth is stub
    sse = factory(
        McpServerConfig(transport="sse", url="http://u.test/sse", oauth_connection="con_9")
    )
    assert isinstance(sse, SseMcpClient)
    assert sse._auth is stub
    assert minted == [("con_9", "http://u.test/mcp"), ("con_9", "http://u.test/sse")]


# ── manager integration: a generated server behind the assembled factory ─────────────────────


@contextmanager
def _loopback_pets_upstream() -> Iterator[str]:
    """A real localhost REST upstream (stdlib, one thread). Needed only here: the assembled
    factory deliberately has no transport-injection seam, so the manager path exercises a real
    socket — loopback only, nothing external."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # the stdlib handler contract dictates the casing
            if self.path != "/pets":
                self.send_error(404)
                return
            body = json.dumps([{"id": "p1", "name": "rex"}]).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


async def test_manager_lists_and_calls_generated_openapi_tools() -> None:
    with _loopback_pets_upstream() as base_url:
        manager = McpManager(client_factory=build_client_factory())
        await manager.register(
            "pets-api", McpServerConfig(transport="openapi", url=base_url, spec=_pets_spec())
        )
        try:
            names = {t.name for t in await manager.list_tools("pets-api")}
            assert {"listPets", "createPet", "getPet"} <= names
            result = await manager.call_tool("pets-api", "listPets", {})
            assert result.is_error is False
            assert json.loads(result.value) == {"result": [{"id": "p1", "name": "rex"}]}
        finally:
            await manager.close_all()
