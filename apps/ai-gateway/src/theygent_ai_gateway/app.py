"""The gateway's HTTP application.

``create_app`` takes its seams as arguments so the fast suite runs the real app against a fake
upstream with no network: ``upstream_transport`` replaces the HTTP transport the upstream client
uses, ``observe`` receives an ``Observation`` for every forwarded exchange once it ends (alongside
the request log, which always does), and ``event_sink`` replaces the control plane the request log
exports to. The upstream client lives for the app's lifetime (one connection pool, closed on
shutdown).

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

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from theygent_ir.gateway import GatewayConfig

from theygent_ai_gateway.conversations import ConversationTracker
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.identity import (
    ControlPlaneKeySource,
    KeyRing,
    KeySource,
    key_set_summary,
    sync_forever,
)
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
#: Calls to the control plane are small and must not hang the key sync.
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
        async with contextlib.AsyncExitStack() as stack:
            stack.push_async_callback(upstream.aclose)
            records.start()
            # Stopped after the exporter: what is still queued reaches the spool, and the next
            # start exports it.
            stack.push_async_callback(records.stop)
            control_plane: ControlPlane | None = None
            if settings.control_plane_url is not None and settings.service_key is not None:
                client = httpx.AsyncClient(timeout=CONTROL_PLANE_TIMEOUT)
                stack.push_async_callback(client.aclose)
                control_plane = (client, settings.control_plane_url, settings.service_key)
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

    app = FastAPI(
        title="TheYgent AI gateway",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.upstream = None
    app.state.request_log = records
    app.state.observe = fan_out(records.record, observe) if observe is not None else records.record
    app.state.keyring = keyring
    app.state.tracker = tracker if tracker is not None else ConversationTracker()
    book = prices if prices is not None else PriceBook.packaged()
    app.state.prices = book.with_overrides(config.pricing_overrides) if config else book
    app.state.config = config
    app.state.policy = Policy()

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Team mode serves no one until it can tell developers apart.
        ready = app.state.upstream is not None and (keyring is None or keyring.loaded)
        return JSONResponse(
            {
                "ready": ready,
                "gatewayId": settings.gateway_id,
                "authMode": settings.auth_mode,
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
