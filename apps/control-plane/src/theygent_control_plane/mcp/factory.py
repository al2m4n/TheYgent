"""The app-assembled ``ClientFactory`` — every transport, wired with its collaborators.

``McpManager``'s built-in default factory can only build the self-contained transports (a stdio
subprocess, a plain remote http/sse server): it has no spec builders for generated servers and
no token storage for user-authorized ones, so it refuses those configs with a loud connection
error. This module is where the app closes that gap. Generated transports (``openapi`` /
``graphql``) get the in-process server builders via :class:`GeneratedMcpClient`; a config whose
``oauth_connection`` is set gets an httpx auth hook minted per connection id by the ``oauth``
collaborator (the token machinery — storage, refresh, the interactive flow — lives entirely
behind that callable, never in this closure)."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from theygent_control_plane.mcp.client import (
    HttpMcpClient,
    McpClient,
    McpConnectionError,
    McpServerConfig,
    SseMcpClient,
    StdioMcpClient,
    TimeoutSource,
)
from theygent_control_plane.mcp.generated import GeneratedMcpClient
from theygent_control_plane.mcp.manager import ClientFactory

#: (connection id, server url) -> httpx auth hook for a user-authorized (token-flow) remote
#: server. The server url is the OAuth resource the tokens are bound to (the audience-binding
#: rule), so the builder needs it alongside the id. The hook injects/refreshes the token per
#: request; the token itself never rides a config or this seam.
OAuthBuilder = Callable[[str, str], httpx.Auth]


def build_client_factory(
    *,
    oauth: OAuthBuilder | None = None,
    call_timeout: TimeoutSource = None,
    connect_timeout: TimeoutSource = None,
) -> ClientFactory:
    """Build the full transport-dispatching factory for ``McpManager(client_factory=...)``.

    A closure over the collaborators rather than a class: the factory's only job is dispatch,
    and the collaborators are fixed for the process lifetime. ``call_timeout``/
    ``connect_timeout`` (a float or a live-settings accessor) reach EVERY client built here —
    resolved at connect/serve time, so a changed setting applies to the next connection without
    rebuilding this factory."""

    def factory(config: McpServerConfig) -> McpClient:
        timeouts: dict[str, TimeoutSource] = {
            "call_timeout": call_timeout,
            "connect_timeout": connect_timeout,
        }
        if config.transport in ("openapi", "graphql"):
            # Upstream auth is already resolved into ``headers`` server-side; the generated
            # server needs no token hook of its own.
            return GeneratedMcpClient(config, **timeouts)
        auth: httpx.Auth | None = None
        if config.oauth_connection is not None:
            if oauth is None:
                raise McpConnectionError(
                    f"MCP server config names oauth connection {config.oauth_connection!r} "
                    "but this process has no token machinery configured — build the factory "
                    "with an oauth builder (build_client_factory(oauth=...))"
                )
            auth = oauth(config.oauth_connection, config.url or "")
        if config.transport == "http":
            return HttpMcpClient(
                url=config.url or "", headers=config.headers, auth=auth, **timeouts
            )
        if config.transport == "sse":
            return SseMcpClient(url=config.url or "", headers=config.headers, auth=auth, **timeouts)
        return StdioMcpClient(
            command=config.command or "",
            args=config.args,
            env=config.env,
            cwd=config.cwd,
            **timeouts,
        )

    return factory
