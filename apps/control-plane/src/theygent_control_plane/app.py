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

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from openai import APIConnectionError, APIStatusError
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from theygent_gateway_client import GatewayClient
from theygent_ir import (
    GraphValidationError,
    IRDocument,
    content_hash,
    parse_document,
    validate_graph,
)

from theygent_control_plane import db
from theygent_control_plane.mcp import McpConnectionError, McpManager, McpServerConfig
from theygent_control_plane.run import Run
from theygent_control_plane.store import AgentStore, McpStore, RunStore, VersionConflict
from theygent_control_plane.tools import DEFAULT_REGISTRY
from theygent_control_plane.walker import (
    EngineNameNotAllowed,
    RouterError,
    TemplateError,
    WalkContext,
    WalkResult,
    llm_models,
    mcp_tool_nodes,
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
    #
    # **M10 deliberate contract extension (graph path only):** ``input`` is ``Any``, not ``str``.
    # A multi-input agent's run input is naturally an OBJECT (e.g. ``{"path": ..., "question":
    # ...}``) that downstream nodes select from with ``$in.in.<field>`` — typing it ``str`` would
    # 422 exactly the multi-input agents M10 exists to enable. The walker already binds any value
    # to the input boundary node; only this request type blocked it. ``/runs`` (a single prompt =
    # a chat message = a string) is intentionally NOT widened.
    ir: dict[str, Any]
    input: Any = None
    stream: bool = True
    thread_id: str | None = None


class CreateAgentRequest(BaseModel):
    # M11 §3: create an agent + its first version from an IR. The agent's §8.2 id and the first
    # version's semver come FROM the IR document (§1.1: the IR carries its identity, the registry
    # persists it under that key — so a stored agent and the Run it produces agree on graph_id /
    # graph_version / contentHash). ``name`` is an optional human label; it falls back to the IR's
    # own name. The IR is a raw dict so the control-plane owns the 400 on a validation failure.
    ir: dict[str, Any]
    name: str | None = None


class AddVersionRequest(BaseModel):
    # M11 §3: add a new immutable version from an IR. The IR's id must match the URL agent id
    # (the version belongs to that agent); its ``version`` is the new coordinate.
    ir: dict[str, Any]


class AgentRunRequest(BaseModel):
    # M11 §3: invoke a saved agent by reference. ``input`` binds to the agent's input boundary node
    # (Any, like /graphs/runs — a multi-input agent's input is naturally an object). The version is
    # resolved as: pinned ``content_hash`` (immutable, content-addressed) > pinned ``version`` >
    # latest published (default). ``thread_id`` reuses M4 thread memory; ``stream`` mirrors the
    # other run surfaces. M11 ships input-only invocation (§4); typed params are deferred.
    input: Any = None
    version: str | None = None
    content_hash: str | None = None
    thread_id: str | None = None
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


def _default_cors_origins() -> list[str]:
    # M8 §3.3 / §6: the cockpit SPA is served by Vite on its own port (never bundled into
    # this app), so the browser needs CORS for the dev origin. Single-user localhost only —
    # not a public CORS posture. Override via THEYGENT_CORS_ORIGINS (comma-separated).
    raw = os.environ.get("THEYGENT_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]


def create_app(
    *,
    inference_base_url: str,
    gateway: GatewayClient | None = None,
    database_url: str | None = None,
    mcp_manager: McpManager | None = None,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    gw = gateway or GatewayClient(inference_base_url)
    store = RunStore()
    mcp_store = McpStore()
    agents = AgentStore()
    # The MCP server registry/connections (m7.md §3.2). App-scoped: created once, connections are
    # lazy (no I/O at construction), torn down at shutdown. `mcp_manager` lets tests inject a
    # manager with a fake client factory.
    mcp = mcp_manager or McpManager()
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
        # M9 §2.1 (F5.2): sweep runs left non-terminal by a crash to `failed` before serving, so a
        # zombie can never lie about being `streaming` forever. Cheap honest mitigation, not resume.
        async with app.state.sessionmaker() as session, session.begin():
            swept = await store.reconcile_orphaned_runs(session)
        if swept:
            logger.warning("run.reconciled_orphans", extra={"count": swept})
        # M9 §2.3 (F6.1): rehydrate persisted MCP registrations into the in-memory manager so they
        # survive a restart. Registration only — connections stay lazy (re-established on next use).
        async with app.state.sessionmaker() as session:
            for name, cfg in await mcp_store.list_servers(session):
                await mcp.register(name, cfg)
        try:
            yield
        finally:
            await mcp.close_all()  # tear down every live MCP connection (m7.md §3.2)
            await gw.aclose()
            await engine.dispose()

    app = FastAPI(title="theygent control-plane", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else _default_cors_origins(),
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.gateway = gw
    app.state.store = store
    app.state.mcp = mcp

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

    async def _terminalize_interrupted(run_id: str) -> None:
        # A streaming response was cancelled (client disconnected) before reaching a terminal state.
        # Fail the run if it's still active so it never lingers as a `streaming` zombie. Status-
        # guarded (fail_if_active) so a late cancel can't overwrite a real completed/failed outcome.
        async with tx() as s:
            changed = await store.fail_if_active(
                s, run_id, "interrupted: client disconnected mid-stream"
            )
        if changed:
            logger.warning("run.interrupted", extra={"run_id": run_id})

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
            terminal = False
            try:
                async with tx() as s:
                    await store.set_status(s, run.id, "streaming")
                yield _sse("run", {"runId": run.id, "status": "streaming", "model": run.model})
                output = ""
                finish_reason: str | None = None
                async for chunk in upstream:
                    if not chunk.choices:
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    delta = choice.delta
                    if delta is None:
                        continue
                    # A reasoning model streams `reasoning_content` (its thinking) before `content`
                    # (its answer). Forward the thinking as visible progress so it doesn't look
                    # frozen, but NEVER into `output` — only `content` is the answer.
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield _sse("reasoning", {"runId": run.id, "reasoning": reasoning})
                    content = getattr(delta, "content", None)
                    if content:
                        output += content
                        yield _sse("delta", {"runId": run.id, "delta": content})
                # Success: mark completed AND append the turn pair in ONE transaction (§4). M9 §2.2
                # persists the output; an empty answer (model hit its token cap before producing
                # content) is surfaced honestly and contributes no thread turn (no blank assistant).
                empty_reason = _empty_output_reason(output, finish_reason)
                async with tx() as s:
                    await store.set_status(
                        s, run.id, "completed", output=output, error=empty_reason
                    )
                    if run.thread_id and empty_reason is None:
                        await store.append_turn(
                            s,
                            thread_id=run.thread_id,
                            run_id=run.id,
                            user_content=user_input,
                            assistant_content=output,
                        )
                terminal = True
                logger.info("run.completed", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "completed"})
                yield "data: [DONE]\n\n"
            except Exception as exc:  # inference died mid-stream (§4): fail cleanly.
                # A failed run contributes no turns — write nothing to the thread, so it
                # never holds a dangling user turn with no answer. The run row records it.
                async with tx() as s:
                    await store.set_status(s, run.id, "failed", error=str(exc))
                terminal = True
                logger.warning("run.failed_midstream", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "failed", "error": str(exc)})
                yield "data: [DONE]\n\n"
            finally:
                # The client disconnected/aborted mid-stream: the generator is cancelled and neither
                # branch above ran, so terminalize the run instead of leaving a `streaming` zombie
                # until the next restart's reconcile sweep. Shielded so the DB write completes
                # despite the cancellation; status-guarded so it can't clobber a real outcome.
                if not terminal:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(_terminalize_interrupted(run.id))

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

        choice = completion.choices[0] if completion.choices else None
        output = (choice.message.content if choice else "") or ""
        finish_reason = choice.finish_reason if choice else None
        # An empty answer (model hit its token cap before producing content) is surfaced honestly
        # and contributes no thread turn — same posture as the streaming path (m9.md §2.4 spirit).
        empty_reason = _empty_output_reason(output, finish_reason)
        async with tx() as s:
            await store.set_status(s, run.id, "completed", output=output, error=empty_reason)
            if run.thread_id and empty_reason is None:
                await store.append_turn(
                    s,
                    thread_id=run.thread_id,
                    run_id=run.id,
                    user_content=user_input,
                    assistant_content=output,
                )
        logger.info("run.completed", extra={"run_id": run.id})
        return {"runId": run.id, "status": "completed", "output": output}

    @app.get("/runs", dependencies=[Depends(require_auth)])
    async def list_runs(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        # M8 §2: the one sanctioned cockpit-driven backend addition — a thin, read-only,
        # paginated list over already-persisted runs (newest first). Additive, not a reshape;
        # /runs/{id} and POST /runs are untouched. NOT an aggregation/summary endpoint (§6).
        runs = await store.list_runs(session, limit=limit, before=before)
        return {"runs": [r.model_dump(mode="json") for r in runs]}

    @app.get("/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        run = await store.get_run(session, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        return run.model_dump(mode="json")

    @app.get("/threads", dependencies=[Depends(require_auth)])
    async def list_threads(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        # M8 §2: read-only paginated thread list (newest activity first). Same additive shape
        # as GET /runs. No new memory write surface — M4's §7 "no standalone memory resource"
        # still holds for writes; this only *reads* what runs already persisted.
        threads = await store.list_threads(session, limit=limit, before=before)
        return {"threads": [t.model_dump(mode="json") for t in threads]}

    @app.get("/threads/{thread_id}", dependencies=[Depends(require_auth)])
    async def get_thread(thread_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        thread = await store.get_thread(session, thread_id)
        if thread is None:
            return _error(f"unknown thread {thread_id!r}", status=404, code="thread_not_found")
        return thread.model_dump(mode="json")

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
        # 2. Execute on the shared IR-run path — the SAME path M11's /agents/{id}/runs reuses; the
        #    only difference between the two surfaces is where the IR came from (inline body here,
        #    the registry there). Everything below — engine/tool/MCP up-front checks, the Run row,
        #    SSE relay, thread memory — is identical (m11.md §3).
        return await _execute_ir_run(
            ir, input_value=req.input, thread_id=req.thread_id, stream=req.stream
        )

    async def _execute_ir_run(
        ir: IRDocument, *, input_value: Any, thread_id: str | None, stream: bool
    ) -> Any:
        """Run a *validated* IRDocument through the M5 walker — the one execution path both
        /graphs/runs (inline IR) and /agents/{id}/runs (saved IR) use (m11.md §3/§5). Assumes the IR
        is already shape/graph-valid; performs the up-front engine/tool/MCP membership checks (which
        depend on live control-plane state, so they run per-invocation, not at store time), creates
        the Run with the graph's recorded identity (§4), and streams/completes. No new execution
        code — M11 only changes where the IR is sourced."""

        # Resolve every llm node's model up front and reject an engine-name binding before anything
        # is created or sent — the §8.4/§3.2 logical-id invariant on the graph path.
        try:
            llms = llm_models(ir)
        except EngineNameNotAllowed as exc:
            return _error(str(exc), status=400, code="engine_name_not_allowed")

        # Resolve every tool node's name against the registry up front (m6.md §5): an unknown tool
        # is a 400 here, before a Run exists — never a runtime surprise inside the walker.
        for node, tool_name in tool_nodes(ir):
            if tool_name not in DEFAULT_REGISTRY:
                return _error(
                    f"node {node.id!r}: unknown tool {tool_name!r}",
                    status=400,
                    code="tool_not_found",
                )

        # mcp_tool nodes (m7.md §4): the server must be registered (400, no Run). The tool's checked
        # against the server's cached capability list ONLY if it's already connected; otherwise the
        # IR is accepted and the runtime handler binds err on a miss (lazy).
        for node, server_name, mcp_tool_name in mcp_tool_nodes(ir):
            if server_name not in mcp:
                return _error(
                    f"node {node.id!r}: unknown MCP server {server_name!r}",
                    status=400,
                    code="mcp_server_not_found",
                )
            cached = mcp.cached_tools(server_name)
            if cached is not None and mcp_tool_name not in {t.name for t in cached}:
                return _error(
                    f"node {node.id!r}: MCP server {server_name!r} does not expose tool "
                    f"{mcp_tool_name!r}",
                    status=400,
                    code="mcp_tool_not_found",
                )

        # The trivial M5 graph has exactly one llm node; record its resolved logical id as the
        # Run's model so GET /runs/{id} stays meaningful (no llm node -> empty, an inert graph).
        run_model = llms[0][1] if llms else ""
        chash = content_hash(ir)

        # Create + persist the Run (M4) with the graph's identity recorded (§4). For a saved-agent
        # invoke this is the agent's coordinate (ir.id == agent id, ir.version == the resolved
        # version, chash == the stored content hash — §1.1), so the Run row carries it (m11.md §6).
        async with tx() as session:
            if thread_id:
                await store.ensure_thread(session, thread_id)
            run = await store.create_run(
                session,
                model=run_model,
                thread_id=thread_id,
                params=None,
                graph_id=ir.id,
                graph_version=ir.version,
                content_hash=chash,
            )

        # Thread memory (M4) is unchanged through the graph path: prior turns replay into the llm
        # node's messages (the walker prepends ctx.prior_messages).
        prior: list[dict[str, Any]] = []
        if thread_id:
            async with tx() as session:
                prior = list(await store.load_thread_messages(session, thread_id))
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
            mcp=mcp,
        )
        if stream:
            return await _stream_graph(ir, run, input_value, ctx)
        return await _complete_graph(ir, run, input_value, ctx)

    async def _stream_graph(ir: Any, run: Run, user_input: Any, ctx: WalkContext) -> Any:
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
        except TemplateError as exc:  # a bad $in token in an llm node (m9.md §1.2): clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            return _error(str(exc), status=422, code="template_error", run_id=run.id)

        async def gen() -> AsyncIterator[str]:
            terminal = False
            try:
                async with tx() as s:
                    await store.set_status(s, run.id, "streaming")
                yield _sse("run", {"runId": run.id, "status": "streaming", "model": run.model})
                for delta in primed:
                    yield _graph_delta_sse(run.id, delta)
                async for delta in walker:
                    yield _graph_delta_sse(run.id, delta)
                # Success: complete the run AND append the turn pair in ONE transaction (§4). The
                # assistant turn is the run's canonical output (the output node's value — m6.md §4),
                # which equals the streamed llm text for a trivial graph but may be a tool result.
                # M9 §2.2 persists the output; §2.4 surfaces an honest empty-output reason.
                output = _coerce_output(result.output)
                async with tx() as s:
                    await store.set_status(
                        s, run.id, "completed", output=output, error=result.empty_reason
                    )
                    # No turn for an empty output (§2.4) — never store a blank assistant turn.
                    if run.thread_id and result.empty_reason is None:
                        await store.append_turn(
                            s,
                            thread_id=run.thread_id,
                            run_id=run.id,
                            user_content=_coerce_output(user_input),
                            assistant_content=output,
                        )
                terminal = True
                logger.info("graph_run.completed", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "completed"})
                yield "data: [DONE]\n\n"
            except Exception as exc:  # inference died / router failed mid-walk: fail cleanly.
                async with tx() as s:
                    await store.set_status(s, run.id, "failed", error=str(exc))
                terminal = True
                logger.warning("graph_run.failed_midstream", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "failed", "error": str(exc)})
                yield "data: [DONE]\n\n"
            finally:
                # Client disconnected mid-stream: terminalize so the run isn't a `streaming` zombie
                # (mirrors /runs; shielded + status-guarded). The walker is in-process, so a cancel
                # also stops the underlying inference call when the generator is closed.
                if not terminal:
                    with contextlib.suppress(Exception):
                        await asyncio.shield(_terminalize_interrupted(run.id))

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _complete_graph(ir: Any, run: Run, user_input: Any, ctx: WalkContext) -> Any:
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
        except TemplateError as exc:  # a bad $in token in an llm node (m9.md §1.2): clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            return _error(str(exc), status=422, code="template_error", run_id=run.id)
        except Exception as exc:  # mid-walk drop on the non-stream path: fail cleanly, no turn.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            return _error(str(exc), status=502, code="inference_error", run_id=run.id)

        output = _coerce_output(result.output)
        async with tx() as s:
            # M9 §2.2: persist the output; §2.4: an honest reason when an upstream error left the
            # output empty (no turn appended for an empty output).
            await store.set_status(s, run.id, "completed", output=output, error=result.empty_reason)
            if run.thread_id and result.empty_reason is None:
                await store.append_turn(
                    s,
                    thread_id=run.thread_id,
                    run_id=run.id,
                    user_content=_coerce_output(user_input),
                    assistant_content=output,
                )
        logger.info("graph_run.completed", extra={"run_id": run.id})
        return {"runId": run.id, "status": "completed", "output": output}

    # ── theygent-native API: /agents/* (M11 — the agent registry) ────────
    # The lifecycle fork: a saved agent has a stable §8.2 id, immutable versioned content addressed
    # by contentHash, and is invoked BY REFERENCE — not by pasting a graph (m11.md §0). Purely
    # additive (§3/§7): /runs and /graphs/runs are untouched; inline IR stays the authoring loop,
    # /agents/* is the saved loop. The registry stores the canonical §8.2 IR — it invents no agent
    # format — and the hash it stores IS the walker's hash (§1.1), so a stored agent and the Run it
    # produces agree byte-for-byte. Single-user localhost: no auth/RBAC/sharing built (§1.3/§7).

    @app.post("/agents", status_code=201, dependencies=[Depends(require_auth)])
    async def create_agent(req: CreateAgentRequest) -> Any:
        # Validate the IR (M5 shape + graph checks); the agent id + first version come FROM the IR
        # (§1.1). View is stripped before hashing (§1.2). A NEW agent only — re-using an existing
        # id is a 409 directing the caller to POST a version (identity is immutable, not content).
        try:
            ir = parse_document(req.ir)
            validate_graph(ir)
        except (ValidationError, GraphValidationError) as exc:
            return _error(str(exc), status=400, code="invalid_ir")
        doc, view, chash = _canonical_ir_and_view(ir)
        async with tx() as session:
            if await agents.get_agent(session, ir.id) is not None:
                return _error(
                    f"agent {ir.id!r} already exists; add a version via "
                    f"POST /agents/{ir.id}/versions",
                    status=409,
                    code="agent_exists",
                )
            await agents.create_agent(session, agent_id=ir.id, name=req.name or ir.name)
            await agents.add_version(
                session,
                agent_id=ir.id,
                version=ir.version,
                content_hash=chash,
                ir=doc,
                view=view,
            )
            detail = await agents.get_agent_detail(session, ir.id)
        assert detail is not None
        logger.info(
            "agent.created",
            extra={"agent_id": ir.id, "version": ir.version, "content_hash": chash},
        )
        return detail.model_dump(mode="json")

    @app.get("/agents", dependencies=[Depends(require_auth)])
    async def list_agents(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        # Paginated, newest-first list (M8 §2 list shape); each row carries the latest version +
        # count, composed client-side-friendly in one query (no aggregating endpoint — M11 §5).
        rows = await agents.list_agents(session, limit=limit, before=before)
        return {"agents": [a.model_dump(mode="json") for a in rows]}

    @app.get("/agents/{agent_id}", dependencies=[Depends(require_auth)])
    async def get_agent(agent_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        detail = await agents.get_agent_detail(session, agent_id)
        if detail is None:
            return _error(f"unknown agent {agent_id!r}", status=404, code="agent_not_found")
        return detail.model_dump(mode="json")

    @app.post("/agents/{agent_id}/versions", dependencies=[Depends(require_auth)])
    async def add_agent_version(agent_id: str, req: AddVersionRequest) -> Any:
        try:
            ir = parse_document(req.ir)
            validate_graph(ir)
        except (ValidationError, GraphValidationError) as exc:
            return _error(str(exc), status=400, code="invalid_ir")
        if ir.id != agent_id:
            return _error(
                f"IR id {ir.id!r} does not match agent {agent_id!r} in the URL; a version's IR "
                "must carry the same §8.2 id",
                status=400,
                code="agent_id_mismatch",
            )
        doc, view, chash = _canonical_ir_and_view(ir)
        async with tx() as session:
            if await agents.get_agent(session, agent_id) is None:
                return _error(f"unknown agent {agent_id!r}", status=404, code="agent_not_found")
            try:
                _meta, created = await agents.add_version(
                    session,
                    agent_id=agent_id,
                    version=ir.version,
                    content_hash=chash,
                    ir=doc,
                    view=view,
                )
            except VersionConflict as exc:
                # Immutability guard (§1.2): different content under an existing (id, version).
                return _error(str(exc), status=409, code="version_conflict")
            detail = await agents.get_agent_detail(session, agent_id)
        assert detail is not None
        logger.info(
            "agent.versioned",
            extra={
                "agent_id": agent_id,
                "version": ir.version,
                "content_hash": chash,
                "created": created,
            },
        )
        # 201 for a genuinely new version; 200 for an idempotent re-publish of identical content.
        return JSONResponse(detail.model_dump(mode="json"), status_code=201 if created else 200)

    @app.get("/agents/{agent_id}/versions/{version}", dependencies=[Depends(require_auth)])
    async def get_agent_version(
        agent_id: str, version: str, session: AsyncSession = Depends(get_session)
    ) -> Any:
        # The stored IR (+ view) for that version — the §8.2 document, view-stripped for hashing but
        # returned with its view alongside (§3). The IR is authored/edited as JSON (M8), so this is
        # what the editor reloads.
        sv = await agents.get_version(session, agent_id, version)
        if sv is None:
            return _error(
                f"agent {agent_id!r} has no version {version!r}",
                status=404,
                code="agent_version_not_found",
            )
        return sv.model_dump(mode="json")

    @app.post("/agents/{agent_id}/runs", dependencies=[Depends(require_auth)])
    async def run_agent(agent_id: str, req: AgentRunRequest) -> Any:
        # Invoke-by-reference (m11.md §3/§5): resolve the agent's IR (pinned contentHash > pinned
        # version > latest), then run it through the EXISTING walker path (_execute_ir_run) — same
        # SSE relay, same Run row (recording the agent's graph_id/graph_version/contentHash), same
        # thread memory. The only new thing is *where the IR came from*: the registry, not the body.
        async with tx() as session:
            agent_exists = await agents.get_agent(session, agent_id) is not None
            if req.content_hash:
                sv = await agents.get_version_by_hash(session, agent_id, req.content_hash)
            elif req.version:
                sv = await agents.get_version(session, agent_id, req.version)
            else:
                sv = await agents.latest_version(session, agent_id)
        if sv is None:
            if not agent_exists:
                return _error(f"unknown agent {agent_id!r}", status=404, code="agent_not_found")
            pinned = req.content_hash or req.version
            detail = f"version {pinned!r}" if pinned else "any published version"
            return _error(
                f"agent {agent_id!r} has no {detail}",
                status=404,
                code="agent_version_not_found",
            )
        # The stored IR was validated at store time; re-parse (and validate) so the walker always
        # gets a validated IRDocument (the same precondition /graphs/runs guarantees).
        try:
            ir = parse_document(sv.ir)
            validate_graph(ir)
        except (ValidationError, GraphValidationError) as exc:  # pragma: no cover - stored valid
            return _error(str(exc), status=400, code="invalid_ir")
        logger.info(
            "agent.invoked",
            extra={"agent_id": agent_id, "version": sv.version, "content_hash": sv.content_hash},
        )
        return await _execute_ir_run(
            ir, input_value=req.input, thread_id=req.thread_id, stream=req.stream
        )

    # ── management plane: /admin/mcp/* (M7 — MCP server registry) ─────────
    # theygent-native management surface (like the inference plane's /admin/*). Servers connect
    # lazily; registration is pure metadata. `env` stays in the user's trust domain (§10) — it is
    # passed into the spawned subprocess, never logged with values, never resolved in the cloud.

    def _mcp_view(name: str) -> dict[str, Any]:
        cfg = mcp.config(name)
        assert cfg is not None
        # `env` keys are listed but VALUES are never echoed back (sovereignty §10).
        return {
            "name": name,
            "transport": cfg.transport,
            "command": cfg.command,
            "args": cfg.args,
            "envKeys": sorted(cfg.env or {}),
            "connected": mcp.is_connected(name),
        }

    @app.put("/admin/mcp/servers/{name}", dependencies=[Depends(require_auth)])
    async def put_mcp_server(name: str, request: Request) -> Any:
        raw = await request.json()
        try:
            cfg = McpServerConfig.model_validate(raw)
        except ValidationError as exc:
            return _error(
                f"invalid MCP server config: {exc}", status=422, code="invalid_mcp_config"
            )
        await mcp.register(name, cfg)
        # M9 §2.3: persist the registration so it survives a restart (the manager is in-memory).
        async with tx() as s:
            await mcp_store.upsert_server(s, name, cfg)
        return JSONResponse(_mcp_view(name))

    @app.get("/admin/mcp/servers", dependencies=[Depends(require_auth)])
    async def list_mcp_servers() -> Any:
        return {"servers": mcp.states()}

    @app.get("/admin/mcp/servers/{name}", dependencies=[Depends(require_auth)])
    async def get_mcp_server(name: str) -> Any:
        if name not in mcp:
            return _error(f"unknown MCP server {name!r}", status=404, code="mcp_server_not_found")
        return JSONResponse(_mcp_view(name))

    @app.delete("/admin/mcp/servers/{name}", status_code=204, dependencies=[Depends(require_auth)])
    async def delete_mcp_server(name: str) -> Response:
        if name not in mcp:
            return _error(f"unknown MCP server {name!r}", status=404, code="mcp_server_not_found")
        await mcp.remove(name)
        # M9 §2.3: drop the persisted registration too, so a delete also survives a restart.
        async with tx() as s:
            await mcp_store.delete_server(s, name)
        return Response(status_code=204)

    @app.get("/admin/mcp/servers/{name}/tools", dependencies=[Depends(require_auth)])
    async def get_mcp_tools(name: str) -> Any:
        # Capability probe: connects if needed (lazy), returns the cached tool list. A server that
        # won't spawn is a clean 503, never a 500 (mirrors engine_unavailable).
        if name not in mcp:
            return _error(f"unknown MCP server {name!r}", status=404, code="mcp_server_not_found")
        try:
            tools = await mcp.list_tools(name)
        except McpConnectionError as exc:
            return _error(str(exc), status=503, code="mcp_unavailable")
        return {
            "tools": [
                {"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in tools
            ]
        }

    @app.post("/admin/mcp/servers/{name}:warm", dependencies=[Depends(require_auth)])
    async def warm_mcp_server(name: str) -> Any:
        if name not in mcp:
            return _error(f"unknown MCP server {name!r}", status=404, code="mcp_server_not_found")
        try:
            await mcp.warm(name)
        except McpConnectionError as exc:
            return _error(str(exc), status=503, code="mcp_unavailable")
        return JSONResponse(_mcp_view(name))

    @app.post("/admin/mcp/servers/{name}:close", dependencies=[Depends(require_auth)])
    async def close_mcp_server(name: str) -> Any:
        if name not in mcp:
            return _error(f"unknown MCP server {name!r}", status=404, code="mcp_server_not_found")
        await mcp.close(name)
        return JSONResponse(_mcp_view(name))

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
        # MCP is informational only (m7.md §7): a registered-but-unconnected server does NOT make
        # the control-plane not-ready — connections are lazy, so "ready" means "can start", not
        # "every external dependency is up". An unspawnable server surfaces as a clean error at the
        # node invocation, never here.
        return JSONResponse({"status": "ready", "mcp": {"servers": mcp.states()}})

    return app


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _graph_delta_sse(run_id: str, delta: Any) -> str:
    """Relay one walker ``Delta`` to SSE, routing a model's *thinking* to ``event: reasoning`` and
    its *answer* to ``event: delta`` — the same split the prompt-run path makes (so a reasoning node
    in a graph streams visible progress and the cockpit never looks frozen)."""
    if getattr(delta, "kind", "content") == "reasoning":
        return _sse("reasoning", {"runId": run_id, "reasoning": delta.content})
    return _sse("delta", {"runId": run_id, "delta": delta.content})


def _empty_output_reason(output: str, finish_reason: str | None) -> str | None:
    """An honest reason when a run completed but produced no answer, else ``None`` (m9.md §2.4
    spirit, extended to the inference layer). A blank ``content`` with ``finish_reason == 'length'``
    means the model hit its token cap before answering — classically a reasoning model that spent
    the whole budget on its hidden ``reasoning_content``. Surfacing this stops an empty completion
    from masquerading as a clean success (and tells the user the concrete fix: raise maxTokens)."""
    if output.strip():
        return None
    if finish_reason == "length":
        return (
            "output empty: the model hit the token limit (finish_reason=length) before producing "
            "any content — raise maxTokens (a reasoning model can spend the whole budget thinking)"
        )
    return f"output empty: the model produced no content (finish_reason={finish_reason})"


def _canonical_ir_and_view(ir: IRDocument) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
    """Split a validated IR into (canonical view-stripped document, view block, contentHash) for the
    registry (M11 §1.2). The hash is the walker's ``content_hash`` (§1.1 — the one function, so a
    stored agent and a walked graph never disagree). The stored ``ir`` is the default-filled model
    dump (decision D2) with ``view`` removed and ``contentHash`` stamped to the computed value, so
    the stored document is self-describing and re-parses to the identical model + identical hash.
    The ``view`` (React-Flow layout) is stored alongside but NEVER hashed — dragging a node must not
    mint a new version (§8.0)."""

    chash = content_hash(ir)
    doc = ir.model_dump(mode="json", by_alias=True, exclude_none=False)
    view = doc.pop("view", None)
    doc["contentHash"] = chash
    return doc, view, chash


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
