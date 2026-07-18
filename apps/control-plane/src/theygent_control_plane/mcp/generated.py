"""Generated MCP servers — an OpenAPI spec or a GraphQL endpoint minted into in-process tools.

A user who already has an API shouldn't have to write an MCP server to give agents tools. This
module turns an ``McpServerConfig`` with ``transport="openapi"`` or ``"graphql"`` into a live MCP
server hosted IN THIS PROCESS: no subprocess, no socket — the client reaches it over an in-memory
transport, and only the *upstream* API calls leave the process (through an httpx client that
carries the server-side-resolved auth headers; the secret never appears in the IR).

The two shapes are deliberately different:

- **OpenAPI** → one tool per operation (named by ``operationId``), derived mechanically from the
  parsed ``spec``. ``options.include`` / ``options.exclude`` are glob patterns over route paths
  that narrow the surface (an include list means everything else is excluded).
- **GraphQL** → exactly TWO generic tools (``introspect_schema`` + ``run_query``), not one tool
  per field: a per-field tool must synthesize selection sets for nested types, which is
  inherently lossy, so the model is given the schema and writes real queries instead. Queries
  are parsed + validated LOCALLY against the introspected schema before anything goes upstream;
  mutations are rejected unless ``options.allowMutations``, subscriptions always.

:class:`GeneratedMcpClient` is the ``McpClient`` over such a server. It inherits the actor
discipline of the transport clients (one background task owns every async context, calls served
serially with the shared per-call ceiling, drain-on-death, sentinel-then-cancel close) — an
in-process server earns no laxer lifecycle than a subprocess.
"""

from __future__ import annotations

import fnmatch
import json
from contextlib import AsyncExitStack
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from fastmcp.server.providers.openapi import MCPType, RouteMap
from graphql import (
    GraphQLSchema,
    GraphQLSyntaxError,
    OperationDefinitionNode,
    OperationType,
    build_client_schema,
    get_introspection_query,
    parse,
    print_schema,
    validate,
)

from theygent_control_plane.mcp.client import (
    McpConnectionError,
    McpServerConfig,
    McpToolDescriptor,
    TimeoutSource,
    _ActorMcpClient,
    _call_timeout_s,
    _resolve_timeout,
)


class GeneratedServerError(RuntimeError):
    """The generated-server definition is unusable: an invalid/unsupported OpenAPI spec, a
    missing endpoint url, or malformed options. Raised at build time with the underlying parse
    error's message preserved, so the user can fix the definition — distinct from a transport
    failure (:class:`McpConnectionError`) on a server that built fine."""


def _upstream_timeout(options: dict[str, Any], call_timeout: TimeoutSource = None) -> float:
    """The upstream httpx timeout: ``options.timeoutSeconds``, defaulting to the SAME resolved
    per-call ceiling the actor enforces (``call_timeout`` — the live-settings accessor when one
    is threaded in, else the env/default read). Resolving both from one source keeps the
    ordering options > setting > env/default in one place: a raised call-timeout setting must
    reach the upstream client too, or the actor would wait on an httpx client that already
    cancelled at the old ceiling."""
    raw = options.get("timeoutSeconds")
    if raw is None:
        return _resolve_timeout(call_timeout, _call_timeout_s)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise GeneratedServerError(f"invalid timeoutSeconds: {raw!r}") from exc


def _globs(options: dict[str, Any], key: str) -> list[str]:
    raw = options.get(key) or []
    if isinstance(raw, str) or not isinstance(raw, list | tuple):
        raise GeneratedServerError(f"options.{key} must be a list of glob patterns")
    return [str(glob) for glob in raw]


def _glob_regex(glob: str) -> str:
    # fnmatch.translate anchors only the END of the pattern, and route maps match with a regex
    # *search* — so anchor the start too, or "/pets/*" would also hit "/internal/pets/x".
    return r"\A" + fnmatch.translate(glob)


def _route_maps(options: dict[str, Any]) -> list[RouteMap] | None:
    """Translate ``include``/``exclude`` route-path globs into route maps (first match wins).
    Excludes are checked first; an include list present means everything not matching one of
    its globs is excluded (routes matching no map default to tools)."""
    include = _globs(options, "include")
    exclude = _globs(options, "exclude")
    maps: list[RouteMap] = [
        RouteMap(pattern=_glob_regex(glob), mcp_type=MCPType.EXCLUDE) for glob in exclude
    ]
    if include:
        maps.extend(RouteMap(pattern=_glob_regex(glob), mcp_type=MCPType.TOOL) for glob in include)
        maps.append(RouteMap(pattern=r".*", mcp_type=MCPType.EXCLUDE))
    return maps or None


def build_openapi_server(
    config: McpServerConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    call_timeout: TimeoutSource = None,
) -> tuple[FastMCP, httpx.AsyncClient]:
    """Build an MCP server from ``config.spec`` (a parsed OpenAPI 3.0/3.1 document): one tool
    per operation, calls forwarded to ``config.url`` with ``config.headers`` (the upstream auth,
    resolved server-side — opaque here). Returns the server AND the upstream httpx client; the
    CALLER owns closing the client (the server holds it but never closes it).

    ``transport`` is a TEST-ONLY seam: it substitutes the upstream httpx transport (e.g. a
    ``MockTransport``) so the suite exercises real request/response flow without a socket.
    Production callers never pass it. ``call_timeout`` is the per-call ceiling source the
    upstream timeout falls back to when the config sets no ``timeoutSeconds``."""
    if not isinstance(config.spec, dict) or not config.spec:
        raise GeneratedServerError("openapi config has no parsed spec document")
    client = httpx.AsyncClient(
        base_url=config.url or "",
        headers=config.headers,
        timeout=_upstream_timeout(config.options, call_timeout),
        transport=transport,
    )
    try:
        server = FastMCP.from_openapi(
            openapi_spec=config.spec,
            client=client,
            name="generated-openapi",
            route_maps=_route_maps(config.options),
        )
    except Exception as exc:
        raise GeneratedServerError(f"invalid OpenAPI spec: {exc}") from exc
    return server, client


def build_graphql_server(
    config: McpServerConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    call_timeout: TimeoutSource = None,
) -> tuple[FastMCP, httpx.AsyncClient]:
    """Build the two-generic-tool MCP server over the GraphQL endpoint at ``config.url``
    (``config.headers`` = upstream auth). The schema is introspected through the authed client
    on first use and cached for the server instance's lifetime. Returns the server AND the
    upstream httpx client; the CALLER owns closing the client.

    ``transport`` and ``call_timeout`` are the same seams as :func:`build_openapi_server`."""
    if not config.url:
        raise GeneratedServerError("graphql config has no endpoint url")
    endpoint = config.url
    allow_mutations = bool(config.options.get("allowMutations", False))
    client = httpx.AsyncClient(
        headers=config.headers,
        timeout=_upstream_timeout(config.options, call_timeout),
        transport=transport,
    )
    server = FastMCP("generated-graphql")
    schema_cache: list[GraphQLSchema] = []

    async def _execute(body: dict[str, Any]) -> Any:
        """POST one GraphQL request; transport/HTTP failures and a GraphQL ``errors`` array are
        tool errors (a structured outcome the caller sees), never raw exceptions."""
        try:
            response = await client.post(endpoint, json=body)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise ToolError(f"GraphQL endpoint request failed: {exc}") from exc
        if not isinstance(payload, dict):
            raise ToolError("GraphQL endpoint returned a non-object response")
        if payload.get("errors"):
            raise ToolError(f"GraphQL errors: {json.dumps(payload['errors'])}")
        return payload.get("data")

    async def _schema() -> GraphQLSchema:
        if schema_cache:
            return schema_cache[0]
        data = await _execute({"query": get_introspection_query()})
        try:
            schema = build_client_schema(data)
        except Exception as exc:
            raise ToolError(f"could not build a schema from introspection: {exc}") from exc
        schema_cache.append(schema)
        return schema

    @server.tool
    async def introspect_schema() -> str:
        """Return the endpoint's schema as SDL text — read this before writing a query."""
        return print_schema(await _schema())

    @server.tool
    async def run_query(query: str, variables: dict[str, Any] | None = None) -> Any:
        """Validate ``query`` against the schema locally, execute it upstream, return ``data``."""
        schema = await _schema()
        try:
            document = parse(query)
        except GraphQLSyntaxError as exc:
            raise ToolError(f"invalid GraphQL query: {exc.message}") from exc
        errors = validate(schema, document)
        if errors:
            messages = "; ".join(error.message for error in errors)
            raise ToolError(f"invalid GraphQL query: {messages}")
        for definition in document.definitions:
            if not isinstance(definition, OperationDefinitionNode):
                continue
            if definition.operation is OperationType.SUBSCRIPTION:
                raise ToolError("subscription operations are not supported")
            if definition.operation is OperationType.MUTATION and not allow_mutations:
                raise ToolError(
                    "mutation operations are disabled for this server "
                    "(enable with options.allowMutations)"
                )
        return await _execute({"query": query, "variables": variables or {}})

    return server, client


class GeneratedMcpClient(_ActorMcpClient):
    """An ``McpClient`` over a generated server, reached through the IN-MEMORY transport.

    Same guarantees as the subprocess/remote clients: the dedicated background task owns every
    async context (the in-memory session AND the upstream httpx client's closing), calls are
    served serially off the queue under the shared per-call ceiling, queued callers are failed
    when the task dies, and ``close()`` is sentinel + shielded wait + cancel fallback — all
    inherited. The base's ``_open_transport`` hook expects raw ``(read, write)`` streams, but
    the in-memory transport yields a ready ``ClientSession`` (it runs the server loop and the
    stream plumbing internally), so this subclass overrides :meth:`_run` instead; the
    connect/serve/call/close machinery is reused unchanged.

    ``transport`` is the builders' TEST-ONLY upstream-transport seam, forwarded verbatim."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        call_timeout: TimeoutSource = None,
        connect_timeout: TimeoutSource = None,
    ) -> None:
        super().__init__(call_timeout=call_timeout, connect_timeout=connect_timeout)
        self._config = config
        self._transport = transport

    def _build(self) -> tuple[FastMCP, httpx.AsyncClient]:
        # The client's own call-timeout source doubles as the upstream fallback, so the actor
        # ceiling and the upstream httpx timeout resolve from the same knob.
        if self._config.transport == "openapi":
            return build_openapi_server(
                self._config, transport=self._transport, call_timeout=self._call_timeout
            )
        if self._config.transport == "graphql":
            return build_graphql_server(
                self._config, transport=self._transport, call_timeout=self._call_timeout
            )
        raise GeneratedServerError(
            f"transport {self._config.transport!r} is not a generated-server transport"
        )

    async def _run(self) -> None:
        assert self._ready is not None
        try:
            async with AsyncExitStack() as stack:
                server, upstream = self._build()
                # The builder's caller owns closing the upstream client — here that caller is
                # this actor task, so the close rides the same stack as the session.
                stack.push_async_callback(upstream.aclose)
                session = await stack.enter_async_context(
                    FastMCPTransport(server).connect_session()
                )
                await session.initialize()
                listed = await session.list_tools()
                self._tools = [
                    McpToolDescriptor(t.name, t.description, dict(t.inputSchema or {}))
                    for t in listed.tools
                ]
                if not self._ready.done():  # a timed-out connect may have abandoned the wait
                    self._ready.set_result(None)
                await self._serve(session)
        except Exception as exc:
            # Mirrors the base task's contract: before ready the failure surfaces to connect()
            # (an invalid definition arrives as McpConnectionError with the builder's message);
            # after ready it ends the task and the next call_tool raises.
            if not self._ready.done():
                self._ready.set_exception(exc)
        finally:
            # Drain-on-death, same as the base: fail queued callers so nobody hangs.
            while not self._queue.empty():
                pending = self._queue.get_nowait()
                if pending is not None and not pending[2].done():
                    pending[2].set_exception(McpConnectionError("MCP connection closed"))


async def preview_tools(config: McpServerConfig) -> list[McpToolDescriptor]:
    """Build the generated server, connect in-memory, list its tools, tear everything down.
    Backs the preview endpoint: the user sees the would-be tools before creating anything.
    (Listing derives from the definition alone — no upstream request is made.)"""
    client = GeneratedMcpClient(config)
    await client.connect()
    try:
        return await client.list_tools()
    finally:
        await client.close()
