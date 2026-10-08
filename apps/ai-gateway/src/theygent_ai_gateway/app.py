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

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

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
#: Every method a client may use; the gateway forwards them all.
FORWARDED_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def create_app(
    settings: GatewaySettings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
    observe: ObserveHook | None = None,
) -> FastAPI:
    settings = settings if settings is not None else load_settings(os.environ)

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
        try:
            yield
        finally:
            app.state.upstream = None
            await upstream.aclose()

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

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        ready = app.state.upstream is not None
        return JSONResponse(
            {
                "ready": ready,
                "gatewayId": settings.gateway_id,
                "authMode": settings.auth_mode,
                "upstreamUrl": settings.upstream_url,
                "controlPlaneUrl": settings.control_plane_url,
            },
            status_code=200 if ready else 503,
        )

    # Registered last: every path the gateway does not answer itself is forwarded upstream.
    app.add_api_route("/{path:path}", forward, methods=FORWARDED_METHODS, include_in_schema=False)
    return app
