"""The durable worker bootstrap.

Stand up the durable runtime against the shared Postgres, rehydrate the MCP registry (so
``mcp_tool`` activities can spawn their servers in the worker's trust domain), reconcile DBOS
schedules to the enabled schedule-triggers, then block while DBOS background threads consume the
durable queue and recover any workflows a crash left pending.

Topologies: in the **server / air-gapped** topology this runs as a separate
process against the shared PG; in the **desktop sidecar** the control-plane launches the same
runtime in-process and this binary is unused. One codebase, one runtime — only the deployable
differs.
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy.ext.asyncio import AsyncEngine
from theygent_control_plane import db
from theygent_control_plane.durable import DurableRuntime
from theygent_control_plane.mcp import McpManager
from theygent_control_plane.rag import RagRetriever
from theygent_control_plane.store import AgentStore, McpStore, RunStore, TriggerStore
from theygent_gateway_client import GatewayClient

logger = logging.getLogger("theygent.worker")


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL is required (postgresql+asyncpg://…)")
    return url


async def build_runtime(
    *,
    database_url: str,
    inference_base_url: str,
    mcp: McpManager | None = None,
    fast_polling: bool = False,
) -> tuple[DurableRuntime, McpManager, GatewayClient, AsyncEngine]:
    """Assemble (but do not launch) the worker's durable runtime + its dependencies. Returns the
    runtime, the MCP manager (registry rehydrated), the gateway (provider-retry OFF — DBOS owns
    retry), and the SQLAlchemy engine so the caller can dispose it. Factored out so a test can
    drive the worker's exact wiring without the blocking loop."""
    engine = db.create_engine(database_url)
    sessionmaker = db.create_sessionmaker(engine)
    mcp = mcp or McpManager()
    # Rehydrate the MCP registry so mcp_tool activities can connect (connections stay
    # lazy; only the registration is restored). Same posture as the control-plane lifespan.
    mcp_store = McpStore()
    async with sessionmaker() as session:
        for name, cfg in await mcp_store.list_servers(session):
            await mcp.register(name, cfg)
    gateway = GatewayClient(inference_base_url, max_retries=0)
    runtime = DurableRuntime(
        database_url=database_url,
        gateway=gateway,
        mcp=mcp,
        store=RunStore(),
        agents=AgentStore(),
        triggers=TriggerStore(),
        sessionmaker=sessionmaker,
        # Retrieval backend for rag activities — embeds over the same gateway (logical ids),
        # searches the shared Postgres. Same behavior as the control-plane's runtime.
        rag=RagRetriever(sessionmaker, gateway),
        fast_polling=fast_polling,
        # A distinct, stable executor identity per process role: launch-time recovery then claims
        # only workflows THIS role's crashed process left pending, never ones the (healthy) API
        # process is mid-step on.
        executor_id="worker",
    )
    return runtime, mcp, gateway, engine


async def run_worker() -> None:
    """Launch the durable runtime and block forever (until SIGINT/cancellation), consuming the
    queue and recovering pending workflows. Boot reconciliation aligns DBOS schedules to the
    enabled schedule-triggers (create missing, drop orphans) — the same reconcile the control-plane
    does, so whichever process boots first establishes the schedules."""
    database_url = _database_url()
    # The SAME env var the control-plane reads — one name configures the whole deployment. The
    # older worker-only *_BASE_URL name stays as a fallback so an existing setup keeps working.
    inference_base_url = (
        os.environ.get("THEYGENT_INFERENCE_PLANE_URL")
        or os.environ.get("THEYGENT_INFERENCE_PLANE_BASE_URL")
        or "http://127.0.0.1:8081/v1"
    )
    runtime, mcp, gateway, engine = await build_runtime(
        database_url=database_url, inference_base_url=inference_base_url
    )
    runtime.launch()
    triggers = TriggerStore()
    sessionmaker = db.create_sessionmaker(engine)
    async with sessionmaker() as session:
        enabled = await triggers.list_enabled_schedules(session)
    await runtime.reconcile_schedules(enabled)
    logger.info("worker.ready", extra={"queue": "theygent", "schedules": len(enabled)})
    try:
        await asyncio.Event().wait()  # block until cancelled (SIGINT) — DBOS threads do the work
    finally:
        runtime.shutdown()
        await gateway.aclose()
        await mcp.close_all()
        await engine.dispose()
