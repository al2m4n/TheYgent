"""``McpManager`` — the named-MCP-server registry + connection lifecycle.

Same *shape* as the ``EngineManager`` (named registry, lazy-connect on first use, reuse the
connection, capability cache on connect), deliberately scaled down: MCP server processes are cheap
(npm / small Python), engines are heavy (multi-GB / GPU). So **no idle-timeout teardown, no
priority arbitration, no memory-pressure eviction** — the ``EvictionPolicy`` seam is NOT applied
here (different domain, different cost model). Connections live for the control-plane process
lifetime; the only failure handling is **one reconnect-retry on a transport failure**, then bind
``err``.

This is NOT unified with the ``ToolRegistry``: in-process callables and external subprocesses
have genuinely different lifecycle concerns. They only converge at the walker's ``tool`` /
``mcp_tool`` handlers (both call something with args and bind ok/err).

Sovereignty: servers run in the user's trust domain; the manager spawns them locally and the
registration's ``env`` (secrets/paths) is passed straight into the subprocess — never logged with
values, never resolved in theygent cloud.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

from theygent_control_plane.mcp.client import (
    HttpMcpClient,
    McpClient,
    McpConnectionError,
    McpResult,
    McpServerConfig,
    McpToolDescriptor,
    McpToolNotFound,
    SseMcpClient,
    StdioMcpClient,
)

logger = logging.getLogger("theygent.control_plane.mcp")

#: Builds an ``McpClient`` from a registration. Injectable so a test can substitute a fake; the
#: default opens a real stdio subprocess or http session by ``transport`` (the fast suite uses both
#: against a real fake server).
ClientFactory = Callable[[McpServerConfig], McpClient]


def _default_factory(config: McpServerConfig) -> McpClient:
    """Build the transport-appropriate client: ``http``/``sse`` → the remote clients (url + the
    server-side-built auth headers), ``stdio`` → the local subprocess. Generated transports
    (``openapi``/``graphql``) and user-authorized token auth need collaborators (spec builders,
    token storage) this bare factory doesn't have — the app wires those via
    ``build_client_factory``; reaching one here is a configuration error surfaced as a
    connection failure, not a crash."""
    if config.transport in ("openapi", "graphql") or config.oauth_connection:
        raise McpConnectionError(
            f"MCP server config (transport {config.transport!r}) requires the app-configured "
            "client factory; this process was started without one"
        )
    if config.transport == "http":
        return HttpMcpClient(url=config.url or "", headers=config.headers)
    if config.transport == "sse":
        return SseMcpClient(url=config.url or "", headers=config.headers)
    return StdioMcpClient(
        command=config.command or "", args=config.args, env=config.env, cwd=config.cwd
    )


#: The public name for the bare factory — what late-binding callers fall back to before the
#: app's full factory (generated servers, token auth) is constructed.
default_client_factory = _default_factory


class McpServerNotFound(KeyError):
    """A graph referenced an MCP server name that isn't registered. The control-plane maps this to
    400 ``mcp_server_not_found`` at IR validation — no Run created."""


class McpManager:
    """Per-control-plane-instance registry of named MCP servers + their lazy connections."""

    def __init__(self, *, client_factory: ClientFactory = _default_factory) -> None:
        self._factory = client_factory
        self._configs: dict[str, McpServerConfig] = {}
        self._clients: dict[str, McpClient] = {}
        self._tools: dict[str, list[McpToolDescriptor]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ── registration (no I/O — connection is lazy) ────────────────────────────

    async def register(self, name: str, config: McpServerConfig) -> None:
        """Register/replace a server. Does NOT connect (lazy). If a connection for
        this name already exists, it's dropped so the next call reconnects with the new config.
        Serialized on the per-name lock so a replace never interleaves with an in-flight lazy
        connect (which would otherwise cache a client built from the OLD config)."""
        async with self._locks.setdefault(name, asyncio.Lock()):
            await self._drop(name)
            self._configs[name] = config

    async def remove(self, name: str) -> None:
        # The lock object itself is deliberately kept: popping it while a straggler coroutine
        # still holds a reference would leave two locks alive for one name. One idle Lock per
        # removed name is negligible, and a re-register reuses it.
        async with self._locks.setdefault(name, asyncio.Lock()):
            await self._drop(name)
            self._configs.pop(name, None)

    def __contains__(self, name: object) -> bool:
        return name in self._configs

    def config(self, name: str) -> McpServerConfig | None:
        return self._configs.get(name)

    def is_connected(self, name: str) -> bool:
        return name in self._clients

    def cached_tools(self, name: str) -> list[McpToolDescriptor] | None:
        """The capability list cached at connect, or ``None`` if the server isn't connected yet.
        IR validation checks ``config.tool`` against this only when it's available."""
        return self._tools.get(name)

    def states(self) -> list[dict[str, Any]]:
        """Registered servers + live connection state (for ``GET /admin/mcp/servers`` and the
        informational ``/readyz`` ``mcp`` field — registered-but-unconnected is NOT not-ready)."""
        return [
            {"name": name, "transport": cfg.transport, "connected": self.is_connected(name)}
            for name, cfg in self._configs.items()
        ]

    # ── lazy connect + reuse + one reconnect-retry ────────────────────────────

    async def _ensure_connected(self, name: str) -> McpClient:
        if name not in self._configs:
            raise McpServerNotFound(name)
        existing = self._clients.get(name)
        if existing is not None:
            return existing
        # Single-flight: concurrent first-calls for the same server connect once, not N times.
        async with self._locks.setdefault(name, asyncio.Lock()):
            existing = self._clients.get(name)
            if existing is not None:
                return existing
            client = self._factory(self._configs[name])
            await client.connect()  # raises McpConnectionError on spawn/handshake failure
            self._clients[name] = client
            self._tools[name] = await client.list_tools()
            logger.info("mcp.connected", extra={"server": name})
            return client

    async def _drop(self, name: str, *, only: McpClient | None = None) -> None:
        """Evict + close the cached connection. ``only`` scopes the drop to ONE known-bad client:
        two callers failing on the same dead connection both try to drop it, and without the
        identity guard the second drop would close the healthy REPLACEMENT the first caller's
        retry just connected."""
        client = self._clients.get(name)
        if only is not None and client is not only:
            return
        self._clients.pop(name, None)
        self._tools.pop(name, None)
        if client is not None:
            try:
                await client.close()
            except Exception:
                logger.warning("mcp.close_failed", extra={"server": name})

    async def warm(self, name: str) -> None:
        """Eager-connect now instead of on first call (``:warm``)."""
        await self._ensure_connected(name)

    async def close(self, name: str) -> None:
        """Tear down one server's connection (``:close``); it stays registered and reconnects
        lazily on the next call."""
        await self._drop(name)

    async def list_tools(self, name: str) -> list[McpToolDescriptor]:
        """Connect if needed and return the server's tools (the ``/tools`` capability probe)."""
        await self._ensure_connected(name)
        return list(self._tools.get(name, []))

    async def call_tool(self, name: str, tool: str, args: dict[str, Any]) -> McpResult:
        """Invoke ``tool`` on server ``name`` with one reconnect-retry on transport failure.
        A tool-level error comes back as ``McpResult(is_error=True)`` (not a raise);
        an unknown tool raises :class:`McpToolNotFound`; a dead/unreachable connection raises
        :class:`McpConnectionError` after the retry — the walker binds ``err`` in every case."""
        last: McpConnectionError | None = None
        for attempt in (1, 2):
            client = await self._ensure_connected(name)
            if tool not in {t.name for t in self._tools.get(name, [])}:
                raise McpToolNotFound(f"server {name!r} does not expose tool {tool!r}")
            try:
                return await client.call_tool(tool, args)
            except McpConnectionError as exc:
                last = exc
                logger.warning(
                    "mcp.transport_failure",
                    extra={"server": name, "tool": tool, "attempt": attempt},
                )
                # Drop only the client WE failed on — a concurrent caller's retry may already
                # have cached a healthy replacement this must not close.
                await self._drop(name, only=client)
        raise last if last is not None else McpConnectionError("MCP call failed")

    async def list_connection_tools(
        self, name: str, config: McpServerConfig
    ) -> list[McpToolDescriptor]:
        """The tool list of a CONNECTION-BACKED server (keyed by connection id), registering the
        server-side-built config only when it changed — same reuse contract as
        :meth:`call_connection_tool`. Used to build model tool schemas for connection-backed
        bindings."""
        if self._configs.get(name) != config:
            await self.register(name, config)
        return await self.list_tools(name)

    async def call_connection_tool(
        self, name: str, config: McpServerConfig, tool: str, args: dict[str, Any]
    ) -> McpResult:
        """Invoke ``tool`` on a CONNECTION-BACKED MCP server, keyed by ``name`` (the
        connection id) using the server-side-built ``config`` (transport + auth already resolved —
        the secret never reached the IR). Registers the config only when it CHANGED, so a live
        connection is reused across calls (a rotated secret / edited url re-registers + reconnects);
        then the normal ``call_tool`` path (one reconnect-retry, ok/err contract)."""
        if self._configs.get(name) != config:
            await self.register(name, config)
        return await self.call_tool(name, tool, args)

    async def close_all(self) -> None:
        """Tear down every live connection (control-plane shutdown)."""
        for name in list(self._clients):
            await self._drop(name)
