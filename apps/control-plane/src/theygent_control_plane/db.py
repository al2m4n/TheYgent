"""DB engine + session lifecycle (M4 §1.2) — the convention every later table inherits.

The control-plane is near-stateless and scales horizontally (stack §8), so nothing here
may assume a single instance: a pooled async engine created once at startup and disposed
at shutdown, and a fresh ``AsyncSession`` per logical operation — never a global/module
session, never a process-local cache. Postgres is the *shared* state.

Two ways to get a session, both off the one ``async_sessionmaker``:
  * a request-scoped ``AsyncSession`` for read handlers (the ``get_session`` FastAPI
    ``Depends`` defined in ``app.py``); opened and closed within the request.
  * the sessionmaker directly, for writes that outlive the handler return — a streaming
    run commits its turns *after* the response object is returned, so it cannot borrow a
    request-scoped session (§1.2 "per logical operation").
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(database_url: str) -> AsyncEngine:
    """A pooled async engine for ``postgresql+asyncpg://…``. Created once (lifespan)."""
    # pool_pre_ping: a recycled/dead pooled connection surfaces as a clean reconnect
    # rather than a first-query error — matters when instances outlive PG failovers.
    return create_async_engine(database_url, pool_pre_ping=True)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: we read attributes off rows after commit (to build the
    # detached domain entity) without a surprise lazy reload.
    return async_sessionmaker(engine, expire_on_commit=False)


async def ping(engine: AsyncEngine) -> None:
    """Raise if Postgres is unreachable — drives /readyz honesty (§1.4)."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
