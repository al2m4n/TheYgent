"""The gateway's HTTP application.

``create_app`` takes its seams as arguments so the fast suite runs the real app against a fake
upstream with no network: ``upstream_transport`` replaces the HTTP transport the upstream client
uses, ``observe`` receives an ``Observation`` for every forwarded exchange once it ends (alongside
the request log, which always does), and ``key_source``, ``config_source`` and ``event_sink``
replace the control plane for key sync, config and heartbeat, and record export. ``config`` is a
config in effect from the start (not persisted). The upstream client lives for the app's lifetime
(one connection pool, closed on shutdown).

The gateway's own surface (``/healthz``, ``/readyz``, and later ``/admin/*``) speaks camelCase,
like the inference plane's management surface. Every other path is forwarded to the upstream
unchanged (see ``proxy.py``), so the Anthropic-format paths keep Anthropic's shapes. FastAPI's
generated docs routes are off — the gateway sits on a coding agent's model path and serves
nothing it doesn't need to.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from theygent_ir.gateway import GatewayConfig, GatewayHeartbeat, GatewayMode

from theygent_ai_gateway.admin import (
    AdminCors,
    AdminRefused,
    CachedTokenCheck,
    ControlPlaneTokenCheck,
    TokenCheck,
    admin_error,
)
from theygent_ai_gateway.admin import build_router as build_admin_router
from theygent_ai_gateway.configsync import (
    CONFIG_FILE,
    ConfigSource,
    ConfigState,
    ControlPlaneConfigSource,
    UpstreamHealth,
    software_version,
    sync_config_forever,
)
from theygent_ai_gateway.conversations import ConversationTracker
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.credentials import OrgKey
from theygent_ai_gateway.identity import (
    ControlPlaneKeySource,
    KeyRing,
    KeySource,
    key_set_summary,
    sync_forever,
)
from theygent_ai_gateway.local import LocalRoute
from theygent_ai_gateway.observe import ObserveHook, fan_out
from theygent_ai_gateway.policy import Policy
from theygent_ai_gateway.proxy import forward
from theygent_ai_gateway.requestlog import (
    ControlPlaneEventSink,
    EventSink,
    Exporter,
    LogStats,
    RequestLog,
    Spool,
)
from theygent_ai_gateway.settings import GatewaySettings, load_settings

#: Streams can stay open through long thinking pauses (Claude Code itself waits five minutes of
#: silence before giving up), so reads have no timeout; connecting and sending stay bounded.
UPSTREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=120.0, pool=30.0)
#: Parallel subagents and many developers mean many concurrent streams. No connection cap: a
#: capped pool would silently queue requests in the gateway, where the upstream's own rate
#: limits are the right back-pressure.
UPSTREAM_LIMITS = httpx.Limits(max_connections=None, max_keepalive_connections=100)
#: Calls to the control plane are small and must not hang a sync.
CONTROL_PLANE_TIMEOUT = httpx.Timeout(10.0)
#: Every method a client may use; the gateway forwards them all.
FORWARDED_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

#: A client for the control plane, its URL, and the gateway's service key.
type ControlPlane = tuple[httpx.AsyncClient, str, str]


def create_app(
    settings: GatewaySettings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    observe: ObserveHook | None = None,
    key_source: KeySource | None = None,
    tracker: ConversationTracker | None = None,
    prices: PriceBook | None = None,
    config: GatewayConfig | None = None,
    request_log: RequestLog | None = None,
    event_sink: EventSink | None = None,
    config_source: ConfigSource | None = None,
    token_check: TokenCheck | None = None,
) -> FastAPI:
    settings = settings if settings is not None else load_settings(os.environ)
    keyring = KeyRing(settings.state_dir) if settings.auth_mode == "team" else None
    reaches_control_plane = (
        settings.control_plane_url is not None and settings.service_key is not None
    )
    if request_log is None:
        stats = LogStats()
        spool = Spool(
            settings.state_dir / "requests",
            stats=stats,
            exporting=event_sink is not None or reaches_control_plane,
        )
        request_log = RequestLog(spool, gateway_id=settings.gateway_id, stats=stats)
    records = request_log
    # Attached to a control plane, a gateway records nothing until an admin has configured it; a
    # standalone gateway has no one to configure it and observes from the start.
    unconfigured: GatewayMode = (
        "passthrough" if reaches_control_plane or config_source is not None else "observer"
    )
    config_state = ConfigState(
        prices if prices is not None else PriceBook.packaged(),
        unconfigured_mode=unconfigured,
        path=settings.state_dir / CONFIG_FILE,
    )
    if config is not None:
        config_state.apply(config, persist=False)
    health = UpstreamHealth()
    version = software_version()
    org_key = OrgKey(
        env_value=settings.org_api_key,
        state_dir=settings.state_dir,
        applicable=settings.auth_mode == "team",
        storable=settings.store_org_key,
    )
    org_key.load()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        upstream = httpx.AsyncClient(
            base_url=settings.upstream_url,
            transport=upstream_transport,
            timeout=UPSTREAM_TIMEOUT,
            limits=UPSTREAM_LIMITS,
            follow_redirects=False,
        )
        app.state.upstream = upstream
        started_at = datetime.now(UTC)
        app.state.started_at = started_at
        # The last known good config first, so a restart without the control plane keeps it.
        config_state.load_from_disk()
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(upstream.aclose)
            if settings.inference_plane_url is not None:
                local = LocalRoute(settings.inference_plane_url)
                app.state.local = local
                stack.push_async_callback(local.aclose)
            records.start()
            # Stopped after the exporter: what is still queued reaches the spool, and the next
            # start exports it.
            stack.push_async_callback(records.stop)
            control_plane: ControlPlane | None = None
            if settings.control_plane_url is not None and settings.service_key is not None:
                client = httpx.AsyncClient(timeout=CONTROL_PLANE_TIMEOUT)
                stack.push_async_callback(client.aclose)
                control_plane = (client, settings.control_plane_url, settings.service_key)
                if token_check is None:
                    app.state.admin_check = CachedTokenCheck(ControlPlaneTokenCheck(*control_plane))
            if keyring is not None:
                # The last known keys first, so a restart without the control plane still serves.
                keyring.load_from_disk()
                source = key_source
                if source is None:
                    if control_plane is None:
                        raise RuntimeError("team settings without a control plane or service key")
                    source = ControlPlaneKeySource(*control_plane)
                sync = asyncio.create_task(sync_forever(keyring, source, settings.key_sync_seconds))
                stack.push_async_callback(_cancel, sync)
            configs = config_source
            if configs is None and control_plane is not None:
                configs = ControlPlaneConfigSource(*control_plane)
            if configs is not None:

                async def beat() -> GatewayHeartbeat:
                    snapshot = config_state.current
                    return GatewayHeartbeat(
                        gateway_id=settings.gateway_id,
                        software_version=version,
                        mode=snapshot.mode,
                        config_version=snapshot.version,
                        key_set_version=keyring.version if keyring is not None else None,
                        started_at=started_at,
                        spool_backlog=await asyncio.to_thread(records.spool.backlog),
                        upstream_reachable=health.reachable,
                    )

                syncing = sync_config_forever(
                    config_state, configs, beat, settings.heartbeat_seconds
                )
                stack.push_async_callback(_cancel, asyncio.create_task(syncing))
            sink = event_sink
            if sink is None and control_plane is not None:
                sink = ControlPlaneEventSink(*control_plane)
            if sink is not None:
                exporter = Exporter(
                    records.spool, sink, stats=records.stats, appended=records.appended
                )
                stack.push_async_callback(_cancel, asyncio.create_task(exporter.run()))
            try:
                yield
            finally:
                app.state.upstream = None
                app.state.local = None
                if token_check is None:
                    app.state.admin_check = None

    app = FastAPI(
        title="TheYgent AI gateway",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.upstream = None
    app.state.local = None
    app.state.request_log = records
    hooks = (records.record, health.observe, *((observe,) if observe is not None else ()))
    app.state.observe = fan_out(*hooks)
    app.state.keyring = keyring
    app.state.tracker = tracker if tracker is not None else ConversationTracker()
    app.state.config_state = config_state
    app.state.policy = Policy()
    app.state.org_key = org_key
    app.state.started_at = None
    # Who may call /admin/*: callers the control plane vouches for as admins. A standalone
    # personal gateway has no control plane to ask, so its admin surface is open like the gateway
    # itself (loopback only, which personal-mode settings enforce). Anything else without a
    # checker refuses: the surface fails closed.
    app.state.admin_check = CachedTokenCheck(token_check) if token_check is not None else None
    app.state.admin_open = (
        settings.auth_mode == "personal" and not reaches_control_plane and token_check is None
    )
    app.add_middleware(AdminCors, origins=settings.admin_origins)
    app.add_exception_handler(AdminRefused, _admin_refused)

    async def admin_status(request: Request) -> dict[str, Any]:
        snapshot = config_state.current
        stats = records.stats
        started: datetime | None = app.state.started_at
        return {
            "gatewayId": settings.gateway_id,
            "softwareVersion": version,
            "authMode": settings.auth_mode,
            "mode": snapshot.mode,
            "configVersion": snapshot.version,
            "startedAt": started.isoformat() if started else None,
            "upstreamUrl": settings.upstream_url,
            "controlPlaneUrl": settings.control_plane_url,
            "upstreamReachable": health.reachable,
            "keySet": (
                {"loaded": keyring.loaded, "version": keyring.version}
                if keyring is not None
                else None
            ),
            "requestLog": {
                "recorded": stats.recorded,
                "dropped": stats.dropped,
                "unrecordable": stats.unrecordable,
                "unwritten": stats.unwritten,
                "exported": stats.exported,
                "unreadable": stats.unreadable,
                "prunedUnexported": stats.pruned_unexported,
                "lastExportError": stats.last_export_error,
                "backlog": await asyncio.to_thread(records.spool.backlog),
            },
        }

    app.include_router(build_admin_router(admin_status))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Team mode serves no one until it can tell developers apart.
        ready = app.state.upstream is not None and (keyring is None or keyring.loaded)
        snapshot = config_state.current
        return JSONResponse(
            {
                "ready": ready,
                "gatewayId": settings.gateway_id,
                "authMode": settings.auth_mode,
                "mode": snapshot.mode,
                "configVersion": snapshot.version,
                "upstreamUrl": settings.upstream_url,
                "controlPlaneUrl": settings.control_plane_url,
                **(key_set_summary(keyring) if keyring is not None else {}),
            },
            status_code=200 if ready else 503,
        )

    # Registered last: every path the gateway does not answer itself is forwarded upstream.
    app.add_api_route("/{path:path}", forward, methods=FORWARDED_METHODS, include_in_schema=False)
    return app


async def _cancel(task: asyncio.Task[None]) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def _admin_refused(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AdminRefused):  # registered for AdminRefused only
        raise exc
    return admin_error(exc)
