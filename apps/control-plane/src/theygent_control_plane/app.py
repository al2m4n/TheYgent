"""The control-plane API — theygent-native, owns one run end to end (M3 §4 / M4 §4-5).

Surfaces (§3.1: theygent-native, deliberately NOT OpenAI-shaped — the OpenAI-compat
surface lives on the inference plane):

  * POST /runs            create + execute a run; SSE stream when stream:true.
                          Optional ``thread_id`` opts the run into conversational memory.
  * GET  /runs/{run_id}   run status (now read from Postgres — survives a restart)
  * GET  /healthz         liveness
  * GET  /readyz          readiness — can it reach the inference plane AND Postgres?

The control-plane reaches inference **only** over HTTP via the gateway-client (§3.1, the
one hard-to-reverse rule). M4 adds Postgres as the control-plane store (§3): runs persist
and threads replay prior turns. Two session paths (§1.2): read handlers take a request-
scoped session via ``Depends``; run execution opens a transaction per logical operation
via the sessionmaker, because a streaming run writes its turns *after* returning the
response. ``create_app`` takes ``database_url`` + ``inference_base_url`` (or injected
``GatewayClient``) so tests point it at an ephemeral Postgres and a real threaded server.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from theygent_gateway_client import GatewayClient

from theygent_control_plane import db
from theygent_control_plane.run import Run
from theygent_control_plane.store import RunStore

logger = logging.getLogger("theygent.control_plane")

# §3.2: the control-plane forwards a LOGICAL model id, never an engine name. These are
# the managed-engine names from the binding enum (theygent-graph-schema.md §8.4); they
# must never appear as a `/runs` `model`. Kept as a local constant on purpose — M4 does
# not import `theygent_ir` (no graph execution yet, §2).
_ENGINE_NAMES = frozenset({"mlx", "vllm", "llamacpp"})

_RUN_ID_HEADER = "x-theygent-run-id"


class RunRequest(BaseModel):
    input: str
    model: str
    params: dict[str, Any] = {}
    stream: bool = True
    # Opt-in conversational memory (§4): None -> one-shot (M3 behavior, nothing persisted
    # to a thread); given (existing or new) -> prior turns replayed, this turn appended.
    thread_id: str | None = None


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
    database_url: str | None = None,
) -> FastAPI:
    gw = gateway or GatewayClient(inference_base_url)
    store = RunStore()
    # Resolved at startup, not import, so importing the factory has no side effects and a
    # missing DATABASE_URL fails loudly in the lifespan rather than silently.
    db_url = database_url or os.environ.get("DATABASE_URL")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if not db_url:
            raise RuntimeError("DATABASE_URL is required (postgresql+asyncpg://…) — §1.4")
        # Pooled async engine created once, disposed at shutdown (§1.2). Never per-request.
        engine = db.create_engine(db_url)
        app.state.engine = engine
        app.state.sessionmaker = db.create_sessionmaker(engine)
        try:
            yield
        finally:
            await gw.aclose()
            await engine.dispose()

    app = FastAPI(title="theygent control-plane", lifespan=lifespan)
    app.state.gateway = gw
    app.state.store = store

    # Auth placeholder so RBAC slots in later without reshaping handlers (§7) — a no-op
    # dependency today; build nothing now.
    async def require_auth() -> None:
        return None

    async def get_session() -> AsyncIterator[AsyncSession]:
        # Request-scoped session for read handlers (§1.2). Writes that outlive the handler
        # (the streaming run) use `tx()` off the sessionmaker instead.
        async with app.state.sessionmaker() as session:
            yield session

    @contextlib.asynccontextmanager
    async def tx() -> AsyncIterator[AsyncSession]:
        # One transaction per logical operation: commit on success, roll back on error.
        async with app.state.sessionmaker() as session, session.begin():
            yield session

    def _headers(run: Run) -> dict[str, str]:
        # Request identity, not OTel (§5): forward an opaque run id so the inference call
        # is correlatable. No OTel SDK — just a header.
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

        # Create the run (and ensure its thread) in one transaction so it persists
        # immediately — GET /runs/{id} works during streaming and across a restart.
        async with tx() as session:
            if req.thread_id:
                await store.ensure_thread(session, req.thread_id)
            run = await store.create_run(
                session, model=req.model, thread_id=req.thread_id, params=req.params
            )

        # Naive full replay (§4): prior turns verbatim + the new input. No summarization,
        # no truncation — that's a deliberate later milestone.
        messages: list[dict[str, Any]] = []
        if req.thread_id:
            async with tx() as session:
                messages = list(await store.load_thread_messages(session, req.thread_id))
        messages.append({"role": "user", "content": req.input})
        logger.info(
            "run.created",
            extra={"run_id": run.id, "model": run.model, "thread_id": run.thread_id},
        )

        if req.stream:
            return await _stream_run(run, req.input, messages, req.params)
        return await _complete_run(run, req.input, messages, req.params)

    async def _stream_run(
        run: Run, user_input: str, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> Any:
        # Open the upstream stream BEFORE committing to a 200 SSE response, so a pre-stream
        # error (503/404) surfaces as a clean status — never a 200 followed by a broken
        # stream (mirrors inference `_serve_managed`).
        try:
            upstream = await gw.open_stream(
                model=run.model, messages=messages, params=params, extra_headers=_headers(run)
            )
        except APIStatusError as exc:
            status, code, message = _map_inference_error(exc)
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=f"{code}: {message}")
            logger.warning("run.failed", extra={"run_id": run.id, "code": code, "status": status})
            return _error(message, status=status, code=code, run_id=run.id)
        except APIConnectionError as exc:
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            return _error(
                "inference plane unreachable",
                status=503,
                code="inference_unreachable",
                run_id=run.id,
            )

        async def gen() -> AsyncIterator[str]:
            async with tx() as s:
                await store.set_status(s, run.id, "streaming")
            yield _sse("run", {"runId": run.id, "status": "streaming", "model": run.model})
            output = ""
            try:
                async for chunk in upstream:
                    delta = chunk.choices[0].delta if chunk.choices else None
                    content = getattr(delta, "content", None) if delta else None
                    if content:
                        output += content
                        yield _sse("delta", {"runId": run.id, "delta": content})
            except Exception as exc:  # inference died mid-stream (§4): fail cleanly.
                # A failed run contributes no turns — write nothing to the thread, so it
                # never holds a dangling user turn with no answer. The run row records it.
                async with tx() as s:
                    await store.set_status(s, run.id, "failed", error=str(exc))
                logger.warning("run.failed_midstream", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "failed", "error": str(exc)})
                yield "data: [DONE]\n\n"
                return
            # Success only: mark completed AND append the turn pair in ONE transaction (§4)
            # — the user+assistant messages and the run state land together or not at all.
            async with tx() as s:
                await store.set_status(s, run.id, "completed")
                if run.thread_id:
                    await store.append_turn(
                        s,
                        thread_id=run.thread_id,
                        run_id=run.id,
                        user_content=user_input,
                        assistant_content=output,
                    )
            logger.info("run.completed", extra={"run_id": run.id})
            yield _sse("run", {"runId": run.id, "status": "completed"})
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _complete_run(
        run: Run, user_input: str, messages: list[dict[str, Any]], params: dict[str, Any]
    ) -> Any:
        try:
            completion = await gw.complete(
                model=run.model, messages=messages, params=params, extra_headers=_headers(run)
            )
        except APIStatusError as exc:
            status, code, message = _map_inference_error(exc)
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=f"{code}: {message}")
            return _error(message, status=status, code=code, run_id=run.id)
        except APIConnectionError as exc:
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            return _error(
                "inference plane unreachable",
                status=503,
                code="inference_unreachable",
                run_id=run.id,
            )

        output = completion.choices[0].message.content if completion.choices else ""
        async with tx() as s:
            await store.set_status(s, run.id, "completed")
            if run.thread_id:
                await store.append_turn(
                    s,
                    thread_id=run.thread_id,
                    run_id=run.id,
                    user_content=user_input,
                    assistant_content=output or "",
                )
        logger.info("run.completed", extra={"run_id": run.id})
        return {"runId": run.id, "status": "completed", "output": output}

    @app.get("/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        run = await store.get_run(session, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        return run.model_dump(mode="json")

    # ── liveness / readiness ─────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        # Honest readiness (§1.4 / §4): not-ready if EITHER dependency is down, so an
        # unreachable inference plane OR Postgres is a distinguishable not-ready — never a
        # green light that 500s on first request.
        try:
            await db.ping(app.state.engine)
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "not-ready", "reason": f"database unreachable: {exc}"},
            )
        try:
            await gw.models()
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={"status": "not-ready", "reason": f"inference plane unreachable: {exc}"},
            )
        return JSONResponse({"status": "ready"})

    return app


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"
