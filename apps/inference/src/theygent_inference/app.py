"""The two HTTP surfaces (theygent-stack-9.1.md §9.1.0), never conflated.

  * /admin/* — management plane (theygent-native): registry, lifecycle, caps, health
  * /v1/*    — data plane (OpenAI-compatible): the `model` field is a LOGICAL id

``create_app`` takes injectable seams (launcher, clock, probe, policy) so the fast
suite runs everything-real-except-the-weights via a FakeUpstreamLauncher.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, ValidationError
from theygent_ir import Capabilities, ManagedBinding, parse_registration

from theygent_inference.clock import Clock
from theygent_inference.credentials import CredentialResolutionError, resolve_credential
from theygent_inference.eviction import EvictionPolicy, ResourceProbe
from theygent_inference.gateway import Gateway, merge_params
from theygent_inference.launcher import (
    EngineLauncher,
    EngineUnavailableError,
    LlamaCppLauncher,
    ManagedLauncherSet,
    MlxLauncher,
)
from theygent_inference.manager import EngineManager, NoCapacityError, NotManagedError, Upstream
from theygent_inference.registry import Registry, UnknownLogicalId
from theygent_inference.vllm_engine import VllmLauncher

_REAP_INTERVAL_SEC = 30.0


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]]
    stream: bool = False


def _openai_error(message: str, *, status: int, type_: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": type_, "code": code}},
    )


def _engine_unavailable(exc: Exception) -> JSONResponse:
    # The model is registered but its engine isn't installed/ready on this host
    # (e.g. an `mlx` model on a box without mlx-lm, or `vllm` without CUDA).
    return _openai_error(str(exc), status=503, type_="server_error", code="engine_unavailable")


def _cockpit_cors_origins() -> list[str]:
    # The M8 cockpit SPA calls the management plane (/admin/models, /admin/engines) DIRECTLY
    # from the browser — the inference plane is user-controlled and reachable, not proxied
    # through the control-plane (the two-plane split). So the browser needs CORS for the dev
    # origin here too, symmetric with the control-plane's. Narrow dev-origin only (never `*`);
    # override via THEYGENT_CORS_ORIGINS (comma-separated). Empty list disables it entirely.
    raw = os.environ.get("THEYGENT_CORS_ORIGINS")
    if raw is not None:
        return [o.strip() for o in raw.split(",") if o.strip()]
    # The cockpit (:5173) and the M15 visual interface (:5174) both call /admin/models directly.
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]


def create_app(
    *,
    launcher: EngineLauncher | None = None,
    clock: Clock | None = None,
    probe: ResourceProbe | None = None,
    policy: EvictionPolicy | None = None,
    max_resident: int = 2,
    enable_reaper: bool = True,
    cors_origins: list[str] | None = None,
    state_path: Path | None = None,
) -> FastAPI:
    # M9 §2.3: persist the logical-model registry LOCALLY to the inference plane (never the
    # control-plane's Postgres — the plane boundary). `state_path=None` keeps it in-memory (the
    # fast suite never touches disk); the real entrypoint passes a path under the plane's state dir.
    registry = Registry(state_path)
    # One launcher per managed engine, behind a single dispatcher so the manager stays
    # engine-agnostic (MLX/vLLM added with zero EngineManager changes). Tests inject a
    # single fake launcher that serves every binding.
    engine_launcher = launcher or ManagedLauncherSet(
        {
            "llamacpp": LlamaCppLauncher(),
            "mlx": MlxLauncher(),
            "vllm": VllmLauncher(),
        }
    )
    manager = EngineManager(
        registry,
        engine_launcher,
        clock=clock,
        probe=probe,
        policy=policy,
        max_resident=max_resident,
    )
    gateway = Gateway()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        reaper: asyncio.Task[None] | None = None
        if enable_reaper:

            async def _reap_loop() -> None:
                while True:
                    await asyncio.sleep(_REAP_INTERVAL_SEC)
                    with contextlib.suppress(Exception):
                        await manager.reap()

            reaper = asyncio.create_task(_reap_loop())
        try:
            yield
        finally:
            if reaper is not None:
                reaper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reaper
            await manager.shutdown()

    app = FastAPI(title="theygent inference plane", lifespan=lifespan)
    _origins = cors_origins if cors_origins is not None else _cockpit_cors_origins()
    if _origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.state.registry = registry
    app.state.manager = manager
    app.state.gateway = gateway
    app.state.launcher = engine_launcher

    # ── management plane: /admin/* ──────────────────────────────────────

    def _model_view(logical_id: str) -> dict[str, Any]:
        binding = registry.require(logical_id)
        return {
            "logicalId": logical_id,
            "binding": binding.model_dump(by_alias=True),
            "state": manager.state(logical_id),
        }

    @app.put("/admin/models/{logical_id}")
    async def put_model(logical_id: str, request: Request) -> JSONResponse:
        raw = await request.json()
        try:
            binding = parse_registration(raw)
        except ValidationError as exc:
            return _openai_error(
                f"invalid registration payload: {exc.errors()}",
                status=422,
                type_="invalid_request_error",
                code="invalid_binding",
            )
        registry.put(logical_id, binding)
        return JSONResponse(_model_view(logical_id))

    @app.get("/admin/models")
    async def list_models() -> dict[str, Any]:
        return {"models": [_model_view(lid) for lid in registry.ids()]}

    @app.get("/admin/engines")
    async def list_engines() -> dict[str, Any]:
        # Running managed engines + resident state (§9.1.2). Count-based arbitration,
        # so no RAM/VRAM bytes yet — maxResident is the ceiling the policy enforces.
        return {"maxResident": manager.max_resident, "resident": manager.resident_engines()}

    @app.get("/admin/models/{logical_id}")
    async def get_model(logical_id: str) -> Response:
        if registry.get(logical_id) is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        return JSONResponse(_model_view(logical_id))

    @app.delete("/admin/models/{logical_id}", status_code=204)
    async def delete_model(logical_id: str) -> Response:
        if registry.get(logical_id) is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        await manager.evict(logical_id)
        registry.delete(logical_id)
        return Response(status_code=204)

    @app.get("/admin/models/{logical_id}/capabilities")
    async def get_capabilities(logical_id: str) -> Response:
        binding = registry.get(logical_id)
        if binding is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        if isinstance(binding, ManagedBinding):
            try:
                caps = await manager.capabilities(logical_id)
            except EngineUnavailableError as exc:
                return _engine_unavailable(exc)
        else:
            # Reachable upstreams aren't probed locally in M1; advertise defaults.
            caps = Capabilities()
        return JSONResponse(caps.model_dump(by_alias=True))

    @app.post("/admin/models/{logical_id}:warm")
    async def warm_model(logical_id: str) -> Response:
        binding = registry.get(logical_id)
        if binding is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        if isinstance(binding, ManagedBinding):
            try:
                await manager.warm(logical_id)
            except EngineUnavailableError as exc:
                return _engine_unavailable(exc)
            except NoCapacityError as exc:
                return _openai_error(str(exc), status=503, type_="server_error", code="no_capacity")
        return JSONResponse(_model_view(logical_id))

    @app.post("/admin/models/{logical_id}:evict")
    async def evict_model(logical_id: str) -> Response:
        if registry.get(logical_id) is None:
            return _openai_error(
                f"unknown logical id {logical_id!r}",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )
        await manager.evict(logical_id)
        return JSONResponse(_model_view(logical_id))

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> Response:
        body: dict[str, Any] = {}
        # Per-engine breakdown when the launcher is the dispatcher (the real app):
        # one engine missing (e.g. vLLM on a Mac) must not make the service not-ready.
        if isinstance(engine_launcher, ManagedLauncherSet):
            body["engines"] = {
                name: {"ready": r.ready, "reason": r.reason}
                for name, r in engine_launcher.readiness().items()
            }
        if engine_launcher.ready:
            body["status"] = "ready"
            return JSONResponse(body)
        body["status"] = "not-ready"
        body["reason"] = engine_launcher.not_ready_reason
        return JSONResponse(status_code=503, content=body)

    # ── data plane: /v1/* (OpenAI-compatible) ───────────────────────────

    @app.get("/v1/models")
    async def openai_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {"id": lid, "object": "model", "owned_by": "theygent"} for lid in registry.ids()
            ],
        }

    @app.post("/v1/chat/completions")
    async def chat_completions(req: ChatRequest) -> Response:
        # The `model` field is a LOGICAL id. An engine name (e.g. "llamacpp") is
        # simply not a registered id -> model_not_found. Engine names never reach here.
        try:
            binding = registry.require(req.model)
        except UnknownLogicalId:
            return _openai_error(
                f"unknown logical id {req.model!r} (the model field is a logical id, "
                "not an engine name)",
                status=404,
                type_="invalid_request_error",
                code="model_not_found",
            )

        params = merge_params(binding.params, req.model_dump())

        if isinstance(binding, ManagedBinding):
            return await _serve_managed(req, params)
        return await _serve_reachable(binding, req, params)

    async def _serve_managed(req: ChatRequest, params: dict[str, Any]) -> Response:
        # Spawn + resolve capacity BEFORE committing to a response. For a stream,
        # the 200/text-event-stream status flushes before the body generator runs,
        # so a spawn/capacity failure must surface here as a clean error, not as a
        # 200 followed by a broken stream.
        try:
            await manager.warm(req.model)
        except EngineUnavailableError as exc:
            return _engine_unavailable(exc)
        except NoCapacityError as exc:
            return _openai_error(str(exc), status=503, type_="server_error", code="no_capacity")
        except NotManagedError:  # pragma: no cover - guarded by isinstance above
            return _openai_error(
                "binding is not managed", status=500, type_="server_error", code="not_managed"
            )

        if req.stream:

            async def gen():
                # The lease finds the warmed engine resident (no re-spawn) and holds
                # the in-flight slot for the whole stream; on exit a draining engine
                # is torn down. An engine that dies mid-stream propagates the error,
                # but the lease still releases (inflight -> 0) so the slot never leaks.
                async with manager.lease(req.model) as upstream:
                    async for line in gateway.stream(upstream, req.messages, params):
                        yield line

            return StreamingResponse(gen(), media_type="text/event-stream")

        async with manager.lease(req.model) as upstream:
            result = await gateway.complete(upstream, req.messages, params)
        return JSONResponse(result)

    async def _serve_reachable(binding: Any, req: ChatRequest, params: dict[str, Any]) -> Response:
        try:
            api_key = resolve_credential(binding.credential_ref) or "sk-noauth"
        except CredentialResolutionError as exc:
            return _openai_error(
                str(exc), status=502, type_="server_error", code="credential_error"
            )
        upstream = Upstream(api_base=binding.base_url, model=binding.model, api_key=api_key)
        if req.stream:

            async def gen():
                async for line in gateway.stream(upstream, req.messages, params):
                    yield line

            return StreamingResponse(gen(), media_type="text/event-stream")
        result = await gateway.complete(upstream, req.messages, params)
        return JSONResponse(result)

    return app
