"""The control-plane API — theygent-native, owns one run end to end (M3 §4).

Surfaces (§3.1: theygent-native, deliberately NOT OpenAI-shaped — the OpenAI-compat
surface lives on the inference plane):

  * POST /runs            create + execute a run; SSE stream when stream:true
  * GET  /runs/{run_id}   run status
  * GET  /healthz         liveness
  * GET  /readyz          readiness — can it reach the inference plane?

The control-plane reaches inference **only** over HTTP via the gateway-client (§3.1,
the one hard-to-reverse rule). ``create_app`` takes ``inference_base_url`` (or an
injected ``GatewayClient``) so the fast suite points it at a real threaded HTTP server
— real transport, only the model is fake.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel
from theygent_gateway_client import GatewayClient

from theygent_control_plane.run import Run, RunRegistry

logger = logging.getLogger("theygent.control_plane")

# §3.2: the control-plane forwards a LOGICAL model id, never an engine name. These are
# the managed-engine names from the binding enum (theygent-graph-schema.md §8.4); they
# must never appear as a `/runs` `model`. Kept as a local constant on purpose — M3 does
# not import `theygent_ir` (no graph execution yet, §2).
_ENGINE_NAMES = frozenset({"mlx", "vllm", "llamacpp"})

_RUN_ID_HEADER = "x-theygent-run-id"


class RunRequest(BaseModel):
    input: str
    model: str
    params: dict[str, Any] = {}
    stream: bool = True


def _error(message: str, *, status: int, code: str, run_id: str | None = None) -> JSONResponse:
    error: dict[str, Any] = {"message": message, "code": code}
    body: dict[str, Any] = {"error": error}
    # Run-execution failures carry the runId so the client can correlate the failed
    # request with GET /runs/{id} (the §5 request-identity payoff, on the error path too).
    if run_id is not None:
        body["runId"] = run_id
    return JSONResponse(status_code=status, content=body)


def _map_inference_error(exc: APIStatusError) -> tuple[int, str, str]:
    """Map an inference-plane error to a clean control-plane (status, code, message).

    The inference plane returns honest OpenAI-style errors (§4 error mapping); we relay
    them faithfully rather than swallowing or leaking a stacktrace.
    """
    code = "inference_error"
    message = str(exc)
    with contextlib.suppress(Exception):
        body = exc.response.json()
        err = body.get("error", body)
        code = err.get("code", code)
        message = err.get("message", message)
    # 503 engine_unavailable -> 503; 404 model_not_found -> 404; otherwise pass through.
    status = exc.status_code if exc.status_code in (404, 503) else 502
    return status, code, message


def create_app(
    *,
    inference_base_url: str,
    gateway: GatewayClient | None = None,
) -> FastAPI:
    registry = RunRegistry()
    gw = gateway or GatewayClient(inference_base_url)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await gw.aclose()

    app = FastAPI(title="theygent control-plane", lifespan=lifespan)
    app.state.registry = registry
    app.state.gateway = gw

    # Auth placeholder so RBAC slots in later without reshaping handlers (§7) — a no-op
    # dependency today; build nothing now.
    async def require_auth() -> None:
        return None

    def _headers(run: Run) -> dict[str, str]:
        # Request identity, not OTel (§5): forward an opaque run id so the inference
        # call is correlatable. No OTel SDK — just a header.
        return {_RUN_ID_HEADER: run.id}

    # ── theygent-native API: /runs ───────────────────────────────────────

    @app.post("/runs", dependencies=[Depends(require_auth)])
    async def create_run(req: RunRequest) -> Any:
        # §3.2: an engine name is not a logical id. Reject before anything reaches the
        # wire — we never rewrite a logical id into an engine name either.
        if req.model in _ENGINE_NAMES:
            return _error(
                f"{req.model!r} is an engine name, not a logical model id; "
                "the `model` field must be a logical id",
                status=400,
                code="engine_name_not_allowed",
            )

        run = registry.create(model=req.model)
        messages = [{"role": "user", "content": req.input}]
        logger.info("run.created", extra={"run_id": run.id, "model": run.model})

        if req.stream:
            return await _stream_run(run, messages, req.params)
        return await _complete_run(run, messages, req.params)

    async def _stream_run(run: Run, messages: list[dict[str, Any]], params: dict[str, Any]) -> Any:
        # Open the upstream stream BEFORE committing to a 200 SSE response, so a
        # pre-stream error (503/404) surfaces as a clean status — never a 200 followed
        # by a broken stream (mirrors inference `_serve_managed`).
        try:
            upstream = await gw.open_stream(
                model=run.model, messages=messages, params=params, extra_headers=_headers(run)
            )
        except APIStatusError as exc:
            status, code, message = _map_inference_error(exc)
            registry.set_status(run.id, "failed", error=f"{code}: {message}")
            logger.warning("run.failed", extra={"run_id": run.id, "code": code, "status": status})
            return _error(message, status=status, code=code, run_id=run.id)
        except APIConnectionError as exc:
            registry.set_status(run.id, "failed", error=str(exc))
            return _error(
                "inference plane unreachable",
                status=503,
                code="inference_unreachable",
                run_id=run.id,
            )

        async def gen() -> AsyncIterator[str]:
            registry.set_status(run.id, "streaming")
            yield _sse("run", {"runId": run.id, "status": "streaming", "model": run.model})
            try:
                async for chunk in upstream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    content = getattr(delta, "content", None) if delta else None
                    if content:
                        yield _sse("delta", {"runId": run.id, "delta": content})
            except Exception as exc:  # inference died mid-stream (§4): fail cleanly.
                registry.set_status(run.id, "failed", error=str(exc))
                logger.warning("run.failed_midstream", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "failed", "error": str(exc)})
                yield "data: [DONE]\n\n"
                return
            registry.set_status(run.id, "completed")
            logger.info("run.completed", extra={"run_id": run.id})
            yield _sse("run", {"runId": run.id, "status": "completed"})
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _complete_run(
        run: Run, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> Any:
        try:
            completion = await gw.complete(
                model=run.model, messages=messages, params=params, extra_headers=_headers(run)
            )
        except APIStatusError as exc:
            status, code, message = _map_inference_error(exc)
            registry.set_status(run.id, "failed", error=f"{code}: {message}")
            return _error(message, status=status, code=code, run_id=run.id)
        except APIConnectionError as exc:
            registry.set_status(run.id, "failed", error=str(exc))
            return _error(
                "inference plane unreachable",
                status=503,
                code="inference_unreachable",
                run_id=run.id,
            )

        output = completion.choices[0].message.content if completion.choices else ""
        registry.set_status(run.id, "completed")
        logger.info("run.completed", extra={"run_id": run.id})
        return {"runId": run.id, "status": "completed", "output": output}

    @app.get("/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def get_run(run_id: str) -> Any:
        run = registry.get(run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        return run.model_dump(mode="json")

    # ── liveness / readiness ─────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Honest readiness (§4): "control-plane up but inference unreachable" must be a
        # distinguishable not-ready, not a green light that 500s on first request.
        try:
            await gw.models()
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "status": "not-ready",
                    "reason": f"inference plane unreachable: {exc}",
                },
            )
        return JSONResponse({"status": "ready"})

    return app


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
