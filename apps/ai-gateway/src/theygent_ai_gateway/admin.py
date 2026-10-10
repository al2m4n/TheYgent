"""The gateway's admin surface (``/admin/*``, camelCase): what the interface shows and sets.

* ``GET /admin/status`` — identity, mode, config and key-set versions, the request log and
  export, upstream health;
* ``GET /admin/credentials`` — whether each credential is set and where it came from, never a
  value;
* ``PUT``/``DELETE /admin/credentials/org-api-key`` — a single-instance install's org key, kept
  by the gateway (never by the control plane).

Callers present their TheYgent bearer (a session or a personal API key). The gateway asks the
control plane whether it is live and an admin's (``POST /gateway/introspect`` with its service
key) and caches the answer briefly — allowed for ``ALLOW_SECONDS``, refused for
``DENY_SECONDS`` — keyed by the bearer's digest, never the bearer. If the control plane cannot
answer, the surface refuses (503): it fails closed. A standalone personal gateway has no control
plane to ask; its admin surface is open like the gateway itself, which then listens on loopback
only.

Browsers reach only this surface: CORS is answered for ``/admin/*`` alone, for the interface's
origins, and never for the proxied paths.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send
from theygent_ir.gateway import GatewayTokenCheck, GatewayTokenCheckResult

from theygent_ai_gateway.credentials import OrgKey, OrgKeyError, OrgKeyStatus
from theygent_ai_gateway.identity import token_digest

log = logging.getLogger(__name__)

ADMIN_PREFIX = "/admin"
ALLOW_SECONDS = 30.0
DENY_SECONDS = 5.0
MAX_CACHED = 1024


class AdminRefused(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def admin_error(exc: AdminRefused) -> JSONResponse:
    headers = {"retry-after": "5"} if exc.status == 503 else None
    return JSONResponse(
        {"error": {"message": exc.message, "code": exc.code}},
        status_code=exc.status,
        headers=headers,
    )


# ── who may administer ───────────────────────────────────────────────────────


class TokenCheck(Protocol):
    async def check(self, token: str) -> GatewayTokenCheckResult: ...


class ControlPlaneTokenCheck:
    """Asks the control plane's ``POST /gateway/introspect``, authenticated with the gateway's
    service key."""

    def __init__(self, client: httpx.AsyncClient, control_plane_url: str, service_key: str) -> None:
        self._client = client
        self._url = f"{control_plane_url}/gateway/introspect"
        self._auth = {"authorization": f"Bearer {service_key}"}

    async def check(self, token: str) -> GatewayTokenCheckResult:
        response = await self._client.post(
            self._url,
            content=GatewayTokenCheck(token=token).model_dump_json(),
            headers={**self._auth, "content-type": "application/json"},
        )
        response.raise_for_status()
        return GatewayTokenCheckResult.model_validate_json(response.content)


class CachedTokenCheck:
    """Remembers answers briefly, keyed by the bearer's digest: a page of admin calls costs one
    round trip, and a revoked session stops working within ``ALLOW_SECONDS``."""

    def __init__(
        self,
        inner: TokenCheck,
        *,
        allow_seconds: float = ALLOW_SECONDS,
        deny_seconds: float = DENY_SECONDS,
        max_entries: int = MAX_CACHED,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._inner = inner
        self._allow = allow_seconds
        self._deny = deny_seconds
        self._max = max_entries
        self._clock = clock
        self._answers: OrderedDict[str, tuple[float, GatewayTokenCheckResult]] = OrderedDict()

    async def check(self, token: str) -> GatewayTokenCheckResult:
        key = token_digest(token)
        cached = self._answers.get(key)
        now = self._clock()
        if cached is not None and cached[0] > now:
            return cached[1]
        result = await self._inner.check(token)
        allowed = result.active and result.role == "admin"
        self._answers[key] = (now + (self._allow if allowed else self._deny), result)
        self._answers.move_to_end(key)
        while len(self._answers) > self._max:
            self._answers.popitem(last=False)
        return result


def _bearer(request: Request) -> str | None:
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    return (token.strip() or None) if scheme.lower() == "bearer" else None


async def require_admin(request: Request) -> GatewayTokenCheckResult | None:
    """The caller, if this gateway checks callers; ``None`` for an open standalone gateway."""
    checker: TokenCheck | None = getattr(request.app.state, "admin_check", None)
    if checker is None:
        if getattr(request.app.state, "admin_open", False):
            return None
        raise AdminRefused(
            503, "control_plane_unavailable", "the control plane cannot check credentials now"
        )
    token = _bearer(request)
    if token is None:
        raise AdminRefused(
            401,
            "admin_auth_required",
            "sign in to TheYgent: Authorization: Bearer <session or key>",
        )
    try:
        result = await checker.check(token)
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        log.warning("the control plane could not check an admin token: %s", type(exc).__name__)
        raise AdminRefused(
            503, "control_plane_unavailable", "the control plane cannot check credentials now"
        ) from None
    if not result.active:
        raise AdminRefused(401, "admin_auth_invalid", "invalid or expired credentials")
    if result.role != "admin":
        raise AdminRefused(
            403, "admin_required", "the gateway's admin surface needs the admin role"
        )
    return result


# ── what admins see and set ──────────────────────────────────────────────────


class OrgKeyWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str

    def __repr__(self) -> str:
        return "OrgKeyWrite(api_key=<hidden>)"


def _when(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _org_key_view(status: OrgKeyStatus) -> dict[str, Any]:
    return {
        "set": status.set,
        "source": status.source,
        "updatedAt": _when(status.updated_at),
        "writable": status.writable,
    }


def credentials_view(request: Request) -> dict[str, Any]:
    org_key: OrgKey = request.app.state.org_key
    settings = request.app.state.settings
    return {
        "orgApiKey": _org_key_view(org_key.status()),
        "serviceKey": {"set": settings.service_key is not None},
    }


StatusSource = Callable[[Request], Awaitable[dict[str, Any]]]


def build_router(status: StatusSource) -> APIRouter:
    router = APIRouter(prefix=ADMIN_PREFIX, dependencies=[Depends(require_admin)])

    @router.get("/status")
    async def admin_status(request: Request) -> JSONResponse:
        return JSONResponse({**await status(request), "credentials": credentials_view(request)})

    @router.get("/credentials")
    async def admin_credentials(request: Request) -> JSONResponse:
        return JSONResponse(credentials_view(request))

    @router.put("/credentials/org-api-key")
    async def set_org_key(
        request: Request,
        caller: GatewayTokenCheckResult | None = Depends(require_admin),
    ) -> JSONResponse:
        try:
            write = OrgKeyWrite.model_validate_json(await request.body())
        except ValidationError:
            # Never echo the body: it carries a secret.
            raise AdminRefused(422, "invalid_api_key", 'send {"api_key": "<key>"}') from None
        try:
            await asyncio.to_thread(request.app.state.org_key.set, write.api_key)
        except OrgKeyError as exc:
            raise AdminRefused(
                422 if exc.code == "invalid_api_key" else 409, exc.code, exc.message
            ) from None
        log.info("org key stored", extra={"by": caller.user_id if caller else None})
        return JSONResponse(credentials_view(request))

    @router.delete("/credentials/org-api-key")
    async def clear_org_key(
        request: Request,
        caller: GatewayTokenCheckResult | None = Depends(require_admin),
    ) -> JSONResponse:
        try:
            await asyncio.to_thread(request.app.state.org_key.clear)
        except OrgKeyError as exc:
            raise AdminRefused(409, exc.code, exc.message) from None
        log.info("org key cleared", extra={"by": caller.user_id if caller else None})
        return JSONResponse(credentials_view(request))

    return router


class AdminCors:
    """CORS for ``/admin/*`` only. Proxied paths pass through untouched: a browser preflight to
    them is forwarded like any other request, never answered here."""

    def __init__(self, app: ASGIApp, *, origins: tuple[str, ...]) -> None:
        self._app = app
        self._cors = CORSMiddleware(
            app,
            allow_origins=list(origins),
            allow_methods=["GET", "PUT", "DELETE"],
            allow_headers=["authorization", "content-type"],
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if path == ADMIN_PREFIX or path.startswith(ADMIN_PREFIX + "/"):
            await self._cors(scope, receive, send)
        else:
            await self._app(scope, receive, send)
