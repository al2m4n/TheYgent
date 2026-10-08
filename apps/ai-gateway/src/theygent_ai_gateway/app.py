"""The gateway's HTTP application.

``create_app`` takes its seams as arguments so the fast suite runs the real app against a fake
upstream with no network: ``upstream_transport`` replaces the HTTP transport the upstream client
uses. The upstream client lives for the app's lifetime (one connection pool, closed on shutdown).

The gateway's own surface (``/healthz``, ``/readyz``, and later ``/admin/*``) speaks camelCase,
like the inference plane's management surface; the Anthropic-format paths keep Anthropic's
shapes. FastAPI's generated docs routes are off — the gateway sits on a coding agent's model path
and serves nothing it doesn't need to.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from theygent_ai_gateway.settings import GatewaySettings, load_settings

#: Streams can stay open through long thinking pauses (Claude Code itself waits five minutes of
#: silence before giving up), so reads have no timeout; connecting and sending stay bounded.
UPSTREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=None, write=120.0, pool=30.0)


def create_app(
    settings: GatewaySettings | None = None,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = settings if settings is not None else load_settings(os.environ)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        upstream = httpx.AsyncClient(
            base_url=settings.upstream_url,
            transport=upstream_transport,
            timeout=UPSTREAM_TIMEOUT,
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

    return app
