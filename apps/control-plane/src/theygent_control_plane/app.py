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
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from theygent_gateway_client import GatewayClient
from theygent_ir import (
    GraphValidationError,
    content_hash,
    parse_document,
    validate_graph,
)

from theygent_control_plane import db
from theygent_control_plane.run import Run
from theygent_control_plane.store import RunStore
from theygent_control_plane.tools import DEFAULT_REGISTRY
from theygent_control_plane.walker import (
    EngineNameNotAllowed,
    RouterError,
    WalkContext,
    WalkResult,
    llm_models,
    tool_nodes,
    walk,
)

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


class GraphRunRequest(BaseModel):
    # The §8.2 envelope, inline for M5 (no registry yet — M5 §7). Kept as a raw dict so the
    # control-plane owns the 400 shape when it fails IR validation, rather than FastAPI's
    # generic 422. ``input`` binds to the graph's input boundary node; ``thread_id`` reuses M4
    # thread memory through the graph path unchanged. Snake_case request fields, matching /runs.
    ir: dict[str, Any]
    input: str
    stream: bool = True
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

    # ── theygent-native API: /graphs/runs (M5 — the IR walker) ───────────
    # The first consumer of the IR seam: validate an IRDocument, walk it node by node, and
    # produce a Run exactly as /runs does — but IR-driven (m5.md §1). A graph run is still a
    # Run, so GET /runs/{id} is unchanged. /runs itself is untouched (§7).

    @app.post("/graphs/runs", dependencies=[Depends(require_auth)])
    async def create_graph_run(req: GraphRunRequest) -> Any:
        # 1. Validate the IR — Pydantic envelope (§8.2) then semantic graph checks (§5). Either
        #    failure is a 400 with a precise message; NO Run is created (§3.1/§6).
        try:
            ir = parse_document(req.ir)
            validate_graph(ir)
        except (ValidationError, GraphValidationError) as exc:
            return _error(str(exc), status=400, code="invalid_ir")

        # 2. Resolve every llm node's model up front and reject an engine-name binding before
        #    anything is created or sent — the §8.4/§3.2 logical-id invariant on the graph path.
        try:
            llms = llm_models(ir)
        except EngineNameNotAllowed as exc:
            return _error(str(exc), status=400, code="engine_name_not_allowed")

        # 2b. Resolve every tool node's name against the registry up front (m6.md §5): an unknown
        #     tool is a 400 here, before a Run exists — never a runtime surprise inside the walker.
        for node, tool_name in tool_nodes(ir):
            if tool_name not in DEFAULT_REGISTRY:
                return _error(
                    f"node {node.id!r}: unknown tool {tool_name!r}",
                    status=400,
                    code="tool_not_found",
                )

        # The trivial M5 graph has exactly one llm node; record its resolved logical id as the
        # Run's model so GET /runs/{id} stays meaningful (no llm node -> empty, an inert graph).
        run_model = llms[0][1] if llms else ""
        chash = content_hash(ir)

        # 3. Create + persist the Run (M4) with the graph's identity recorded (§4).
        async with tx() as session:
            if req.thread_id:
                await store.ensure_thread(session, req.thread_id)
            run = await store.create_run(
                session,
                model=run_model,
                thread_id=req.thread_id,
                params=None,
                graph_id=ir.id,
                graph_version=ir.version,
                content_hash=chash,
            )

        # 4. Thread memory (M4) is unchanged through the graph path: prior turns replay into the
        #    llm node's messages (the walker prepends ctx.prior_messages).
        prior: list[dict[str, Any]] = []
        if req.thread_id:
            async with tx() as session:
                prior = list(await store.load_thread_messages(session, req.thread_id))
        logger.info(
            "graph_run.created",
            extra={
                "run_id": run.id,
                "graph_id": run.graph_id,
                "graph_version": run.graph_version,
                "content_hash": run.content_hash,
                "thread_id": run.thread_id,
            },
        )

        ctx = WalkContext(
            gateway=gw,
            run_id=run.id,
            prior_messages=prior,
            extra_headers=_headers(run),
        )
        if req.stream:
            return await _stream_graph(ir, run, req.input, ctx)
        return await _complete_graph(ir, run, req.input, ctx)

    async def _stream_graph(ir: Any, run: Run, user_input: str, ctx: WalkContext) -> Any:
        # Prime the walker before committing to a 200 SSE response: a pre-stream error (a 503/404
        # from the llm node's open_stream, or a RouterError from a router that runs before any
        # llm) surfaces as a clean status, never a 200-then-broken-stream (mirrors /runs).
        result = WalkResult()
        walker = walk(ir, user_input, ctx, result)
        try:
            primed = [await anext(walker)]
        except StopAsyncIteration:
            primed = []
        except APIStatusError as exc:
            status, code, message = _map_inference_error(exc)
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=f"{code}: {message}")
            logger.warning("graph_run.failed", extra={"run_id": run.id, "code": code})
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
        except RouterError as exc:  # router failed before any stream committed: clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            return _error(str(exc), status=422, code="router_error", run_id=run.id)

        async def gen() -> AsyncIterator[str]:
            async with tx() as s:
                await store.set_status(s, run.id, "streaming")
            yield _sse("run", {"runId": run.id, "status": "streaming", "model": run.model})
            try:
                for delta in primed:
                    yield _sse("delta", {"runId": run.id, "delta": delta.content})
                async for delta in walker:
                    yield _sse("delta", {"runId": run.id, "delta": delta.content})
            except Exception as exc:  # inference died / router failed mid-walk: fail cleanly.
                async with tx() as s:
                    await store.set_status(s, run.id, "failed", error=str(exc))
                logger.warning("graph_run.failed_midstream", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "failed", "error": str(exc)})
                yield "data: [DONE]\n\n"
                return
            # Success only: complete the run AND append the turn pair in ONE transaction (§4). The
            # assistant turn is the run's canonical output (the output node's value — m6.md §4),
            # which equals the streamed llm text for a trivial graph but may be a tool result.
            output = _coerce_output(result.output)
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
            logger.info("graph_run.completed", extra={"run_id": run.id})
            yield _sse("run", {"runId": run.id, "status": "completed"})
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _complete_graph(ir: Any, run: Run, user_input: str, ctx: WalkContext) -> Any:
        result = WalkResult()
        try:
            async for _delta in walk(ir, user_input, ctx, result):
                pass  # non-stream: drain the deltas; the canonical output is result.output.
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
        except RouterError as exc:  # router named a missing handle / no handle field (§3.2).
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            return _error(str(exc), status=422, code="router_error", run_id=run.id)
        except Exception as exc:  # mid-walk drop on the non-stream path: fail cleanly, no turn.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            return _error(str(exc), status=502, code="inference_error", run_id=run.id)

        output = _coerce_output(result.output)
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
        logger.info("graph_run.completed", extra={"run_id": run.id})
        return {"runId": run.id, "status": "completed", "output": output}

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


def _coerce_output(value: Any) -> str:
    """The run's output as a string for the wire + thread storage (M6). A graph's output node may
    bind a non-string value (e.g. ``http_fetch``'s ``{status, body, headers}``); serialize it so
    the ``output`` field and the ``assistant`` turn stay strings. A trivial M5 graph binds the llm
    text, which passes through unchanged — backward-compatible."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)
