"""Interactive authorization for remote MCP servers (the connection ``auth.type == "oauth"``).

The MCP authorization spec makes an HTTP MCP server an OAuth 2.1 resource server, and the
official SDK ships the entire client side — protected-resource discovery, authorization-server
metadata, PKCE, dynamic client registration, token refresh, scope step-up — as an httpx auth
hook (``OAuthClientProvider``). Hosts in the wild converge on the same UX: OAuth is never
*configured*, it is discovered from the server's 401 and driven interactively once, with the
minted tokens cached outside the config. This module supplies the two pieces the SDK leaves to
the host:

1. :class:`DbTokenStorage` — the SDK's ``TokenStorage`` protocol over the connection's encrypted
   secret. Tokens AND the dynamically-registered client live as ONE JSON blob behind the
   connection's ``secret_ref``, so authorizing (or re-authorizing) a server never moves any
   referencing agent's ``contentHash``, and every refresh writes through — OAuth 2.1 rotates
   refresh tokens, so losing the newest one strands the connection.

2. :class:`OAuthBroker` — the interactive half. A browser flow may start ONLY inside an
   authorize session the user explicitly opened (the ``:start`` endpoint); anywhere else — an
   unattended trigger fire, a scheduled run, the worker — the redirect handler fails fast so the
   node binds ``err`` ("authorize this connection in the MCP page") instead of parking a run on
   a browser tab that will never open. Non-interactive refresh keeps working everywhere.

Sovereignty: tokens are minted by the user's browser against the server's own authorization
server and stored encrypted at rest in the user's control-plane database. They never transit
anything of theygent's and are never logged or returned over the wire.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

import httpx
from mcp.client.auth import OAuthClientProvider
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from pydantic import AnyUrl
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theygent_control_plane.mcp.client import McpConnectionError

if TYPE_CHECKING:  # runtime-lazy: store.py imports the mcp package, never this module
    from theygent_control_plane.secrets import SecretStore
    from theygent_control_plane.store import ConnectionStore

logger = logging.getLogger("theygent.control_plane.mcp.oauth")

#: How long an authorize session may sit waiting for the browser round-trip. Mirrors the SDK
#: provider's own callback timeout so the two never disagree about who gave up first.
AUTHORIZE_TIMEOUT_S = 300.0

_NOT_INTERACTIVE = (
    "this MCP connection requires interactive authorization — open the MCP page and connect it"
)


class DbTokenStorage:
    """The SDK ``TokenStorage`` protocol over a connection's encrypted secret.

    The blob is ``{"tokens": {…}, "clientInfo": {…}}`` under the connection's ``secret_ref``
    (created on first write when the connection has none). Reads tolerate an absent/foreign
    blob — a connection whose secret was hand-set to something else simply reads as
    "not yet authorized" rather than crashing the connect."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        connections: ConnectionStore,
        secrets: SecretStore,
        connection_id: str,
    ) -> None:
        self._sm = sessionmaker
        self._connections = connections
        self._secrets = secrets
        self._connection_id = connection_id

    async def _load(self) -> dict:
        async with self._sm() as session:
            conn = await self._connections.get(session, self._connection_id)
            if conn is None or not conn.secret_ref:
                return {}
            raw = await self._secrets.resolve(session, conn.secret_ref)
        if not raw:
            return {}
        try:
            blob = json.loads(raw)
        except ValueError:
            return {}
        return blob if isinstance(blob, dict) else {}

    async def _save(self, key: str, value: dict) -> None:
        # Read-merge-write under one transaction; rotation keeps the SAME secret_ref so no
        # referencing agent's contentHash moves (the connection/secret discipline).
        async with self._sm() as session, session.begin():
            conn = await self._connections.get(session, self._connection_id)
            if conn is None:
                raise McpConnectionError(
                    f"mcp connection {self._connection_id!r} disappeared during authorization"
                )
            blob: dict = {}
            if conn.secret_ref:
                raw = await self._secrets.resolve(session, conn.secret_ref)
                with contextlib.suppress(ValueError, TypeError):
                    parsed = json.loads(raw or "")
                    if isinstance(parsed, dict):
                        blob = parsed
            blob[key] = value
            payload = json.dumps(blob)
            if conn.secret_ref:
                await self._secrets.set(session, conn.secret_ref, payload)
            else:
                ref = await self._secrets.create(session, payload)
                await self._connections.update(
                    session, self._connection_id, secret_ref=ref, set_secret_ref=True
                )

    # ── the four TokenStorage methods ─────────────────────────────────────────

    async def get_tokens(self) -> OAuthToken | None:
        tokens = (await self._load()).get("tokens")
        if not isinstance(tokens, dict):
            return None
        with contextlib.suppress(Exception):
            return OAuthToken.model_validate(tokens)
        return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        # Full dumps (no exclude_none): a required-but-null field must round-trip or the
        # re-validation on read rejects the blob and the connection reads as unauthorized.
        await self._save("tokens", tokens.model_dump(mode="json"))

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        info = (await self._load()).get("clientInfo")
        if not isinstance(info, dict):
            return None
        with contextlib.suppress(Exception):
            return OAuthClientInformationFull.model_validate(info)
        return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        await self._save("clientInfo", client_info.model_dump(mode="json"))


@dataclass
class _AuthorizeSession:
    """One user-initiated authorize round-trip: the connect task drives the SDK flow, the
    redirect handler parks the authorization URL here, and the callback route resolves the
    code future when the browser lands back. Constructed on the event loop (the endpoint),
    so the futures bind to the running loop."""

    connection_id: str
    auth_url: asyncio.Future[str] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    code: asyncio.Future[tuple[str, str | None]] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )
    state: str | None = None
    task: asyncio.Task[None] | None = None
    started_at: float = field(default_factory=time.monotonic)


class OAuthBroker:
    """Pending interactive flows, keyed by connection id (one active per connection) and by the
    OAuth ``state`` parameter (how the browser callback finds its flow)."""

    def __init__(self, redirect_url: str | Callable[[], str]) -> None:
        # A plain string OR a zero-arg accessor (the live-settings seam): the redirect URL is
        # read per authorize flow — at ``:start`` time, when the provider is built — so a
        # changed deployment URL applies to the next flow without a restart.
        self._redirect_url = redirect_url
        self._active: dict[str, _AuthorizeSession] = {}
        self._by_state: dict[str, _AuthorizeSession] = {}
        self._last_error: dict[str, str] = {}

    @property
    def redirect_url(self) -> str:
        return self._redirect_url() if callable(self._redirect_url) else self._redirect_url

    # ── the two SDK handlers, scoped to a connection ──────────────────────────

    async def _on_redirect(self, connection_id: str, url: str) -> None:
        sess = self._active.get(connection_id)
        if sess is None:
            # No user-opened session → unattended context. Fail fast: the run binds err
            # instead of waiting five minutes for a browser that will never open.
            raise McpConnectionError(_NOT_INTERACTIVE)
        state = parse_qs(urlparse(url).query).get("state", [None])[0]
        sess.state = state
        if state:
            self._by_state[state] = sess
        if not sess.auth_url.done():
            sess.auth_url.set_result(url)

    async def _await_code(self, connection_id: str) -> tuple[str, str | None]:
        sess = self._active.get(connection_id)
        if sess is None:
            raise McpConnectionError(_NOT_INTERACTIVE)
        return await asyncio.wait_for(sess.code, timeout=AUTHORIZE_TIMEOUT_S)

    def handlers(
        self, connection_id: str
    ) -> tuple[Callable[[str], Awaitable[None]], Callable[[], Awaitable[tuple[str, str | None]]]]:
        async def redirect(url: str) -> None:
            await self._on_redirect(connection_id, url)

        async def callback() -> tuple[str, str | None]:
            return await self._await_code(connection_id)

        return redirect, callback

    # ── the authorize session lifecycle (the ``:start`` endpoint) ─────────────

    async def start(
        self, connection_id: str, connect: Callable[[], Awaitable[None]]
    ) -> dict[str, str]:
        """Open an authorize session and kick ``connect`` (a real server connect, which drives
        the SDK's discovery → registration → authorization flow on its first 401). Returns
        ``{"status": "authorized"}`` when stored tokens were already good,
        ``{"status": "pending", "authorizationUrl": …}`` when the browser must visit the
        authorization server, or ``{"status": "error", "error": …}``."""
        self._cancel(connection_id)
        sess = _AuthorizeSession(connection_id=connection_id)
        self._active[connection_id] = sess
        sess.task = asyncio.create_task(self._drive(connection_id, connect))
        url_fut: asyncio.Future = asyncio.ensure_future(_await_future(sess.auth_url))
        try:
            await asyncio.wait(
                {url_fut, sess.task}, timeout=30.0, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            if not url_fut.done():
                url_fut.cancel()
        if sess.auth_url.done() and not sess.auth_url.cancelled():
            return {"status": "pending", "authorizationUrl": sess.auth_url.result()}
        if sess.task.done():
            exc = sess.task.exception()
            if exc is not None:
                return {"status": "error", "error": str(exc)}
            return {"status": "authorized"}
        return {"status": "error", "error": "authorization flow did not start in time"}

    async def _drive(self, connection_id: str, connect: Callable[[], Awaitable[None]]) -> None:
        try:
            await connect()
            self._last_error.pop(connection_id, None)
        except Exception as exc:
            self._last_error[connection_id] = str(exc)
            logger.warning("mcp.oauth.authorize_failed", extra={"connection_id": connection_id})
            raise
        finally:
            self._finish(connection_id)

    def callback(self, state: str, code: str | None, error: str | None) -> bool:
        """The browser landed on the redirect URL. Resolve the matching flow; unknown/expired
        ``state`` returns False (the route renders an honest failure page — never a 500)."""
        sess = self._by_state.pop(state, None)
        if sess is None:
            return False
        if not sess.code.done():
            if error:
                sess.code.set_exception(McpConnectionError(f"authorization was refused: {error}"))
            else:
                sess.code.set_result((code or "", state))
        return True

    def last_error(self, connection_id: str) -> str | None:
        return self._last_error.get(connection_id)

    def pending(self, connection_id: str) -> bool:
        return connection_id in self._active

    def _cancel(self, connection_id: str) -> None:
        sess = self._active.pop(connection_id, None)
        if sess is None:
            return
        if sess.state:
            self._by_state.pop(sess.state, None)
        for fut in (sess.auth_url, sess.code):
            if not fut.done():
                fut.cancel()
        if sess.task is not None and not sess.task.done():
            sess.task.cancel()

    def _finish(self, connection_id: str) -> None:
        sess = self._active.pop(connection_id, None)
        if sess is not None and sess.state:
            self._by_state.pop(sess.state, None)


async def _await_future(fut: asyncio.Future) -> None:
    # asyncio.wait refuses bare futures alongside tasks on some versions; wrap uniformly.
    await asyncio.shield(fut)


def build_oauth_provider(
    *,
    server_url: str,
    storage: DbTokenStorage,
    redirect_url: str,
    redirect_handler: Callable[[str], Awaitable[None]],
    callback_handler: Callable[[], Awaitable[tuple[str, str | None]]],
    scope: str | None = None,
) -> httpx.Auth:
    """The httpx auth hook for one connection: the SDK provider with our storage + handlers.
    Registered as a public client (PKCE, no static secret) — the dominant deployed shape;
    pre-registered credentials can be seeded into the stored blob's ``clientInfo``."""
    metadata = OAuthClientMetadata(
        client_name="TheYgent",
        redirect_uris=[AnyUrl(redirect_url)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        scope=scope,
    )
    return OAuthClientProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
        timeout=AUTHORIZE_TIMEOUT_S,
    )


def non_interactive_handlers() -> tuple[
    Callable[[str], Awaitable[None]], Callable[[], Awaitable[tuple[str, str | None]]]
]:
    """Handlers for processes with no user in the loop (the worker): stored tokens and silent
    refresh work; anything needing a browser fails fast with the actionable message."""

    async def redirect(_url: str) -> None:
        raise McpConnectionError(_NOT_INTERACTIVE)

    async def callback() -> tuple[str, str | None]:
        raise McpConnectionError(_NOT_INTERACTIVE)

    return redirect, callback


#: What the client factory calls to attach token auth to a server config that carries an
#: ``oauth_connection``: (connection id, server url) → the httpx auth hook.
OAuthBuilder = Callable[[str, str], httpx.Auth]


def broker_oauth_builder(
    broker: OAuthBroker,
    sessionmaker: async_sessionmaker[AsyncSession],
    connections: ConnectionStore,
    secrets: SecretStore,
) -> OAuthBuilder:
    """The API process's builder: interactive flows go through the broker (so a user-opened
    authorize session can complete them), refresh runs silently off stored tokens."""

    def build(connection_id: str, server_url: str) -> httpx.Auth:
        redirect, callback = broker.handlers(connection_id)
        return build_oauth_provider(
            server_url=server_url,
            storage=DbTokenStorage(sessionmaker, connections, secrets, connection_id),
            redirect_url=broker.redirect_url,
            redirect_handler=redirect,
            callback_handler=callback,
        )

    return build


def worker_oauth_builder(
    sessionmaker: async_sessionmaker[AsyncSession],
    connections: ConnectionStore,
    secrets: SecretStore,
    *,
    redirect_url: str = "http://localhost/unused-no-interactive-flow",
) -> OAuthBuilder:
    """The worker's builder: same storage (stored tokens + silent refresh work), but any flow
    that needs a browser fails fast — no user sits at the worker."""

    def build(connection_id: str, server_url: str) -> httpx.Auth:
        redirect, callback = non_interactive_handlers()
        return build_oauth_provider(
            server_url=server_url,
            storage=DbTokenStorage(sessionmaker, connections, secrets, connection_id),
            redirect_url=redirect_url,
            redirect_handler=redirect,
            callback_handler=callback,
        )

    return build
