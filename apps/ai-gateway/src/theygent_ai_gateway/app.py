"""The gateway's HTTP application.

``create_app`` takes its seams as arguments so the fast suite runs the real app against a fake
upstream with no network: ``upstream_transport`` replaces the HTTP transport the upstream client
uses, and ``observe`` receives an ``Observation`` for every forwarded exchange once it ends. The
upstream client lives for the app's lifetime (one connection pool, closed on shutdown).

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

from theygent_ai_gateway.conversations import ConversationTracker
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.identity import (
    ControlPlaneKeySource,
    KeyRing,
    KeySource,
    key_set_summary,
    sync_forever,
)
from theygent_ai_gateway.observe import ObserveHook
from theygent_ai_gateway.proxy import forward
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


def create_app(
    settings: GatewaySettings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    observe: ObserveHook | None = None,
    key_source: KeySource | None = None,
    tracker: ConversationTracker | None = None,
    prices: PriceBook | None = None,
) -> FastAPI:
    settings = settings if settings is not None else load_settings(os.environ)
    keyring = KeyRing(settings.state_dir) if settings.auth_mode == "team" else None

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
            if keyring is not None:
                # The last known keys first, so a restart without the control plane still serves.
                keyring.load_from_disk()
                source = key_source
                if source is None:
                    control_plane = httpx.AsyncClient(timeout=CONTROL_PLANE_TIMEOUT)
                    stack.push_async_callback(control_plane.aclose)
                    if settings.control_plane_url is None or settings.service_key is None:
                        raise RuntimeError("team settings without a control plane or service key")
                    source = ControlPlaneKeySource(
                        control_plane, settings.control_plane_url, settings.service_key
                    )
                sync = asyncio.create_task(sync_forever(keyring, source, settings.key_sync_seconds))
                stack.push_async_callback(_cancel, sync)
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
    app.state.observe = observe
    app.state.keyring = keyring
    app.state.tracker = tracker if tracker is not None else ConversationTracker()
    app.state.prices = prices if prices is not None else PriceBook.packaged()

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
