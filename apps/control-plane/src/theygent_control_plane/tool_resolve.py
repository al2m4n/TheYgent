"""The DB-backed connection resolver — the server-side half of the M19 §1.1 tool/MCP auth seam.

The walker/durable steps resolve a connection's auth through a ``ConnectionResolver`` callable so
they never open a DB session themselves (the M5 walker seam; the same indirection M17 telemetry
uses). This module is the one concrete impl: it reads the (non-secret) ``connection`` row +
decrypts its ``secret`` (``SecretStore``) AT STEP TIME, and hands back a ``ResolvedConnection``
(config + the plaintext secret). The plaintext exists only for the call; it is never journaled,
spanned, or returned over the wire. A disabled/unknown connection resolves to ``None``.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theygent_control_plane.secrets import SecretStore
from theygent_control_plane.store import ConnectionStore
from theygent_control_plane.walker import ResolvedConnection


class DbConnectionResolver:
    """Resolves a connection id → ``ResolvedConnection`` over Postgres (M19 §1.1). Constructed once
    at app/runtime startup with the session factory + the connection/secret stores; the resolver
    instance is the ``ConnectionResolver`` callable injected into ``WalkContext.tool_auth`` and the
    durable resources. ``__call__`` makes it a plain async callable (the walker's seam type)."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        connections: ConnectionStore,
        secrets: SecretStore,
    ) -> None:
        self._sm = sessionmaker
        self._connections = connections
        self._secrets = secrets

    async def __call__(self, connection_id: str) -> ResolvedConnection | None:
        async with self._sm() as session:
            conn = await self._connections.get(session, connection_id)
            if conn is None or not conn.enabled:
                return None  # unknown / disabled → honest err at the call site
            secret = await self._secrets.resolve(session, conn.secret_ref)
        return ResolvedConnection(config=conn.config, secret=secret, enabled=conn.enabled)
