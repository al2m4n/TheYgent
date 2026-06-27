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
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast

from fastapi import Depends, FastAPI, Header, Query, Request
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
from theygent_control_plane.artifacts import LocalArtifactStore
from theygent_control_plane.dispatcher import ScheduleDispatcher, cron_is_valid
from theygent_control_plane.gates import GateBackend
from theygent_control_plane.governance import LOCAL_PRINCIPAL, authorize
from theygent_control_plane.mcp import McpConnectionError, McpManager, McpServerConfig
from theygent_control_plane.observability import (
    INPROC_EXECUTOR,
    CaptureLevel,
    SpanBus,
    Telemetry,
    build_otlp_sink,
    now_ns,
)
from theygent_control_plane.run import _SECRETISH_CONFIG_KEYS as _SECRETISH_CONFIG_KEYS
from theygent_control_plane.run import (
    BenchCase,
    BenchPreset,
    BenchRun,
    BenchSuite,
    BenchTargetKind,
    ConnectionKind,
    Run,
    Trigger,
    TriggerKind,
)
from theygent_control_plane.secrets import SECRET_KEY_ENV, SecretStore
from theygent_control_plane.store import (
    AgentStore,
    BenchStore,
    ConnectionStore,
    McpStore,
    RunStore,
    TriggerStore,
    VersionConflict,
    output_digest,
    params_digest,
)
from theygent_control_plane.tool_resolve import DbConnectionResolver
from theygent_control_plane.tools import DEFAULT_REGISTRY
from theygent_control_plane.walker import (
    EngineNameNotAllowed,
    RouterError,
    TemplateError,
    TransformError,
    WalkContext,
    WalkResult,
    audio_model_nodes,
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

# M14 node types that lower ONLY onto the durable runtime (DBOS recv/child-workflows/queue). The
# interactive M5 walker (the `/graphs/runs`, `/agents/{id}/runs`, `/agents/{id}/invoke`, and
# non-durable `fire()` path) cannot run them — a `human` can't durably wait in-process, etc. Caught
# up front with a CLEAR ``durable_required`` error (before a Run), rather than surfacing as the
# walker's raw ``NotImplementedError`` mis-mapped to a 502 inference_error (the confusing
# "not implemented yet" a builder hit when running a human/approval graph interactively).
_DURABLE_ONLY_TYPES = frozenset({"human", "subgraph", "loop", "map"})

_RUN_ID_HEADER = "x-theygent-run-id"

# M17: the synthetic single node a plain ``/runs`` prompt run is traced as. ``/runs`` has no
# IR/walker (it is the M3 one-shot chat call straight to the gateway), but a prompt run is
# conceptually a one-node run — so the waterfall shows a run-root span + one ``llm`` bar (the
# generation, with ttft), the prompt as input and the answer as output. ``node_id == "llm"``.
_PROMPT_NODE = SimpleNamespace(id="llm", type="llm", kind="activity")


class RunRequest(BaseModel):
    input: str
    model: str
    params: dict[str, Any] = {}
    stream: bool = True
    # Opt-in conversational memory (§4): None -> one-shot (M3 behavior, nothing persisted
    # to a thread); given (existing or new) -> prior turns replayed, this turn appended.
    thread_id: str | None = None


class ResumeRunRequest(BaseModel):
    # M14 §1.1: deliver the awaited input to a `waiting` run paused at a `human` node. `input` is
    # the human's response (Any — like a graph run input, it may be an object the agent drills).
    # Durable mode only: the resume maps to DBOS.send to the checkpointed workflow.
    input: Any = None


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


class InvokeRequest(BaseModel):
    # M12 §3.1: the token-authed, cockpit-free sibling of AgentRunRequest. Same resolution (pinned
    # content_hash > version > latest) and the same shared run path; the only differences are the
    # auth gate (§1.3) and the default — ``stream`` defaults to ``False`` because the caller is a
    # program awaiting a result or a ``run_id`` to poll, not a browser holding an SSE session open.
    input: Any = None
    version: str | None = None
    content_hash: str | None = None
    thread_id: str | None = None
    stream: bool = False


class CreateTriggerRequest(BaseModel):
    # M12 §1.2/§3: register a non-interactive entry point for a saved agent. A trigger ALWAYS pins
    # (§1.1) — exactly one of ``version`` / ``content_hash`` — so an unattended deploy runs an
    # immutable artifact. ``config`` carries the kind-specific knobs (a cron expression for
    # ``schedule``, a signing ``secret`` for ``webhook``, an optional ``input`` template). NO inline
    # IR is ever accepted (§1.1): a trigger references a saved agent by id, never a pasted graph.
    agent_id: str
    kind: TriggerKind
    version: str | None = None
    content_hash: str | None = None
    config: dict[str, Any] = {}
    enabled: bool = True


class UpdateTriggerRequest(BaseModel):
    # M12 §3 PATCH: enable/disable or edit config only. The pin + kind are immutable (changing them
    # would change *which immutable artifact* runs — a re-pin is a new trigger, §1.1 discipline).
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class IoPolicyRequest(BaseModel):
    # M17 §1.8/§5: set an agent's I/O capture policy. ``io_capture`` is the persist decision
    # (off|metadata|full); the effective behavior is the AND with the deployment ceiling + topology
    # default, surfaced in the response so the UI shows the real behavior, not a lie. Keyed to the
    # STABLE agent id — editing it never changes the agent's contentHash (M11 immutability).
    io_capture: CaptureLevel
    io_retention_seconds: int | None = None
    redact_rules: dict[str, Any] | None = None


# ── M18 bench request models ─────────────────────────────────────────────────────────────────────


class BenchCaseInput(BaseModel):
    # One golden case in a suite (§2.5). ``input``/``expected`` are the AUTHORED test spec.
    input: Any = None
    expected: Any = None
    assertion: str = "contains"  # exact|contains|regex|json-path-equals|llm-judge
    assertion_config: dict[str, Any] = {}


class CreateSuiteRequest(BaseModel):
    # §2.5: save a suite of golden cases pinned to a target. A model suite sets logical_id/binding;
    # an agent suite sets agent_id + (version|content_hash).
    name: str
    target_kind: BenchTargetKind
    modality: str | None = None
    logical_id: str | None = None
    binding: str | None = None
    agent_id: str | None = None
    version: str | None = None
    content_hash: str | None = None
    cases: list[BenchCaseInput] = []


class RecordBenchRunRequest(BaseModel):
    # §1.6/§2.4: record one result captured AT THE BENCH. The server computes ``params_digest`` from
    # ``params`` and ``output_digest`` from ``output`` — and stores the raw ``output`` ONLY when
    # ``capture`` is true (opt-in, local — §1.6 / §10). Metrics + digests are always stored.
    target_kind: BenchTargetKind
    modality: str
    logical_id: str | None = None
    model_ref: str | None = None
    binding: str | None = None
    params: dict[str, Any] | None = None
    agent_id: str | None = None
    version: str | None = None
    content_hash: str | None = None
    metrics: dict[str, Any] = {}
    output: str | None = None  # used to compute output_digest; only persisted when capture=True
    capture: bool = False  # opt-in raw capture (stored as a LOCAL reference, never a blob)
    suite_id: str | None = None
    case_id: str | None = None
    assertion: str | None = None
    assertion_passed: bool | None = None
    run_id: str | None = None
    label: str | None = None


class CreatePresetRequest(BaseModel):
    # §1.7: save a named, modality-scoped, LITERAL param set. Values only — never a run/agent link.
    name: str
    modality: str
    logical_id: str | None = None
    params: dict[str, Any] = {}


# ── M19 connection request models (§1.1) ─────────────────────────────────────────────────────────


class CreateConnectionRequest(BaseModel):
    # M19 §1.1: create a tool/MCP auth binding. ``config`` is NON-SECRET only
    # (url/headers/transport/
    # scopes); ``secret`` is the credential material — encrypted into the secret store and NEVER
    # stored in the connection row or returned over the wire. ``kind`` ∈ http_auth | mcp_server.
    name: str
    kind: ConnectionKind
    config: dict[str, Any] = {}
    secret: str | None = None  # write-only; goes to the encrypted secret store, never echoed back
    enabled: bool = True


class UpdateConnectionRequest(BaseModel):
    # M19 §1.1 PATCH: edit name / non-secret config / enabled, and ROTATE the secret. A rotation
    # keeps the SAME secret_ref (so no agent's contentHash moves — §1.1). ``secret=""``/None leaves
    # the existing secret untouched; a non-empty ``secret`` rotates it. ``kind`` is immutable.
    name: str | None = None
    config: dict[str, Any] | None = None
    secret: str | None = None  # write-only; non-empty rotates the credential in place
    enabled: bool | None = None


def _secret_keys_from_env() -> list[str] | None:
    """Resolve the Fernet key(s) for the secret store from ``THEYGENT_SECRET_KEY`` (comma-separated
    for rotation — first encrypts, all decrypt). ``None`` when unset (SecretStore then warns + uses
    an ephemeral key — dev-only, see secrets.py)."""
    raw = os.environ.get(SECRET_KEY_ENV)
    if not raw:
        return None
    return [k.strip() for k in raw.split(",") if k.strip()]


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


class AuthError(Exception):
    """An unattended-surface auth failure (M12 §1.3). Carries the house error fields so the
    registered handler maps it to ``{error:{message,code}}`` — kept distinct from FastAPI's own
    ``HTTPException`` so it never collides with framework 4xx handling."""

    def __init__(self, message: str, *, code: str = "unauthorized", status: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` header, or ``None`` if absent /
    malformed / a non-bearer scheme. The presence check is separate from the constant-time compare
    so a missing header and a wrong token are indistinguishable to the caller (both → 401)."""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def _verify_signature(secret: str, raw: bytes, provided: str | None) -> bool:
    """Verify an inbound webhook signature (M12 §3.3): HMAC-SHA256 of the RAW request body keyed by
    the per-webhook secret, constant-time compared. Accepts ``sha256=<hex>`` (the GitHub-style
    convention real senders emit) or a bare hex digest — shaped to what real callers send, not one
    SDK's idea of it. Any absence/mismatch is a clean ``False`` → 401, never an exception."""
    if not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    candidate = provided.split("=", 1)[1] if provided.startswith("sha256=") else provided
    return hmac.compare_digest(candidate.strip(), expected)


def _default_cors_origins() -> list[str]:
    # M8 §3.3 / §6: the dev SPAs are served by Vite on their own ports (never bundled into this
    # app), so the browser needs CORS for each dev origin. Single-user localhost only — not a
    # public CORS posture. Override via THEYGENT_CORS_ORIGINS (comma-separated). Two SPAs talk to
    # the control-plane: the cockpit (:5173, M8) and the M15 visual interface (:5174).
    raw = os.environ.get("THEYGENT_CORS_ORIGINS")
    if raw:
        return [o.strip() for o in raw.split(",") if o.strip()]
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ]


def create_app(
    *,
    inference_base_url: str,
    gateway: GatewayClient | None = None,
    database_url: str | None = None,
    mcp_manager: McpManager | None = None,
    cors_origins: list[str] | None = None,
    invoke_token: str | None = None,
    secret_key: list[str] | None = None,
    start_dispatcher: bool = True,
    dispatcher_interval_s: float = 5.0,
    durable: bool = False,
    dbos_fast_polling: bool = False,
) -> FastAPI:
    # M13 (D2): durable mode re-points the unattended ``fire()`` seam (schedules + webhooks) at the
    # DBOS durable worker and replaces the in-process dispatcher with DBOS dynamic schedules.
    # OFF by default — DBOS is a process-global singleton, so launching it unconditionally would
    # break the many-apps-per-process fast suite; and non-DBOS deployments stay exactly M12. The
    # interactive streaming surfaces (/runs, /graphs/runs, /agents/{id}/runs, /agents/{id}/invoke)
    # stay on the M5 walker in both modes — only the unattended fire path becomes durable.
    gw = gateway or GatewayClient(inference_base_url)
    store = RunStore()
    mcp_store = McpStore()
    agents = AgentStore()
    triggers = TriggerStore()
    bench = BenchStore()  # M18: saved benchmark results + suites/cases + param presets
    # M19 §1.1: the tool/MCP auth seam. ``connections`` persists the (non-secret) bindings the IR's
    # ``tools`` block references by id; ``secrets`` encrypts the material behind each ``secret_ref``
    # (resolved server-side, inside the step — never in the IR/span/journal). Keys come from
    # ``secret_key`` (test injection) or ``THEYGENT_SECRET_KEY`` (comma-separated for rotation);
    # when unset, SecretStore makes an ephemeral key with a loud warning (dev-only — see
    # secrets.py).
    connections = ConnectionStore()
    secrets = SecretStore.from_keys(
        secret_key if secret_key is not None else _secret_keys_from_env()
    )
    # M17: the observability seam. One live SpanBus for the /trace/stream side-channel (shared with
    # the in-process durable runtime so its spans stream too), and the opt-in OTLP sink —
    # constructed
    # ONLY when OTEL_EXPORTER_OTLP_ENDPOINT is set (else None — the always-local default). The
    # Telemetry object (the capture wrapper's home) needs the sessionmaker, so it is built in the
    # lifespan once that exists; these two are its inputs.
    span_bus = SpanBus()
    otlp_sink = build_otlp_sink()
    # M12 §1.3: the first real auth — a single per-deploy bearer token gating the unattended invoke
    # + webhook-management surfaces. NOT RBAC/SSO/multi-user (those are far later, §4). Resolved at
    # construction so a test can inject one and the env drives production. When UNSET, the deploy
    # surfaces are closed (deny-by-default): an unattended entry point with no token is never open.
    token = invoke_token if invoke_token is not None else os.environ.get("THEYGENT_INVOKE_TOKEN")
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
        # M17: the capture wrapper's process resource — writes span/node_io through this
        # sessionmaker
        # (the normal data layer, never @DBOS.transaction — §4) and publishes live span events to
        # the
        # shared bus. The interactive walker reaches it via WalkContext.run_trace below.
        telemetry = Telemetry(
            sessionmaker=app.state.sessionmaker, span_bus=span_bus, otlp_sink=otlp_sink
        )
        app.state.telemetry = telemetry
        # M19 §1.1: the DB-backed connection resolver — the server-side auth seam an http tool /
        # connection-backed mcp_tool resolves through, at step time. Built once (needs the
        # sessionmaker); injected into the interactive WalkContext and the durable resources so the
        # secret resolution is identical on both runtimes (never in the IR/span/journal).
        tool_resolver = DbConnectionResolver(app.state.sessionmaker, connections, secrets)
        app.state.tool_resolver = tool_resolver
        # M19 §2.8: the gate backend (ratelimit counter + quota usage-read). Built once; injected
        # into the interactive WalkContext + durable resources so gates behave identically on both.
        gate_backend = GateBackend(app.state.sessionmaker)
        app.state.gates = gate_backend
        # M19 §2.2: the artifact store transcribe/speak resolve audio references through (local FS
        # in the user's trust domain; the bytes never journal). Injected into both runtimes.
        artifact_store = LocalArtifactStore()
        app.state.artifacts = artifact_store
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
        # M13 §4 (durable mode): the durable worker launches DBOS embedded (the desktop-sidecar
        # topology) and DBOS dynamic schedules REPLACE the in-process dispatcher — schedules dedupe
        # across instances, lifting the M12 single-dispatcher constraint. On boot we reconcile DBOS
        # schedules to the enabled schedule-triggers (create missing, drop orphans). The fire() seam
        # (webhooks + schedules) enqueues theygent_run on the durable queue.
        app.state.dispatcher = None
        app.state.durable_runtime = None
        if durable:
            from theygent_control_plane.durable import DurableRuntime

            # The durable path turns OFF provider-client retry (DBOS owns retry — the double-retry
            # hazard, m13-dbos.md §2), so it uses its own gateway against the same inference base.
            durable_gw = GatewayClient(inference_base_url, max_retries=0)
            app.state.durable_gateway = durable_gw
            runtime = DurableRuntime(
                database_url=db_url,
                gateway=durable_gw,
                mcp=mcp,
                store=store,
                agents=agents,
                triggers=triggers,
                sessionmaker=app.state.sessionmaker,
                fast_polling=dbos_fast_polling,
                telemetry=telemetry,  # M17: durable steps capture through the SAME wrapper
                tool_auth=tool_resolver,  # M19 §1.1: durable steps resolve connection auth too
                gates=gate_backend,  # M19 §2.8: durable gate steps use the same backend
                artifacts=artifact_store,  # M19 §2.2: durable audio steps use the same store
            )
            runtime.launch()
            app.state.durable_runtime = runtime
            async with app.state.sessionmaker() as session:
                enabled = await triggers.list_enabled_schedules(session)
            await runtime.reconcile_schedules(enabled)
        else:
            # M12 §3: the in-process schedule dispatcher over the persisted trigger registry.
            # Schedules "rehydrate" for free — the dispatcher re-reads enabled schedules from
            # Postgres every tick, so a fresh instance picks up every persisted definition with no
            # in-memory restore. The background loop is gated (start_dispatcher) so the fast suite
            # drives `tick` deterministically; the dispatcher object exists either way.
            dispatcher = ScheduleDispatcher(
                sessionmaker=app.state.sessionmaker,
                store=triggers,
                fire=fire,
                interval_s=dispatcher_interval_s,
            )
            app.state.dispatcher = dispatcher
            if start_dispatcher:
                dispatcher.start()
        try:
            yield
        finally:
            if app.state.dispatcher is not None:
                await app.state.dispatcher.stop()  # cancel the M12 schedule loop; in-process
            if app.state.durable_runtime is not None:
                app.state.durable_runtime.shutdown()  # DBOS.destroy() — tear down the embedded RT
                await app.state.durable_gateway.aclose()
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
    app.state.triggers = triggers
    app.state.connections = connections  # M19 §1.1
    app.state.secrets = secrets  # M19 §1.1
    app.state.invoke_token = token

    # Auth placeholder so RBAC slots in later without reshaping handlers (§7) — a no-op
    # dependency today; build nothing now. The cockpit/management surfaces stay open on
    # single-user localhost; only the *unattended* surfaces (§1.3) get a real check below.
    async def require_auth() -> None:
        return None

    @app.exception_handler(AuthError)
    async def _on_auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        # Map the auth failure to the house error shape (`{error:{message,code}}`), not FastAPI's
        # default `{detail}` — so an unauthenticated invoke reads like every other 4xx in the API.
        return _error(exc.message, status=exc.status, code=exc.code)

    async def require_token(authorization: str | None = Header(default=None)) -> None:
        # M12 §1.3: the minimum real auth — a single bearer token gating the unattended invoke +
        # webhook-management surfaces. Constant-time compare; deny-by-default when no token is
        # configured (an open unattended entry point is never the safe default). NOT RBAC/SSO.
        #
        # Attach-point for M11 §1.3 workspace/owner scoping WITHOUT a reshape: this dependency is
        # the ONE place resolving a credential to authority — no handler outside it checks auth. The
        # single global token is the degenerate single-tenant case of a future token→workspace
        # lookup; scoping lands by making THIS function resolve the bearer to a principal (and the
        # `agent`/`trigger` tables gain the deferred `workspace_id` column), so the call sites
        # (`Depends(require_token)`) and the firing path stay shaped as they are. Build only the
        # token now (no speculative principal that nothing consumes — that would erode the seam).
        presented = _bearer_token(authorization)
        if not token or presented is None or not hmac.compare_digest(presented, token):
            raise AuthError("a valid bearer token is required (Authorization: Bearer …)")

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

    async def _finish_trace(ctx: WalkContext, status: str, error: str | None = None) -> None:
        # M17: close the run-root span when a graph run terminalizes (mirrors store.set_status). The
        # root anchors the waterfall's t0 + total duration; its status mirrors the run. Best-effort
        # —
        # telemetry never fails the run it observes. Idempotent (first-writer-wins on the root
        # span).
        if ctx.run_trace is not None:
            with contextlib.suppress(Exception):
                await ctx.run_trace.finish(status=status, error=error)

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

        # M17: trace the prompt run too (a one-node run). No agent → the capture level is the
        # topology default under the deployment ceiling (full on localhost), so the prompt + answer
        # show in the click-through drawer like any node's I/O.
        telemetry: Telemetry = app.state.telemetry
        run_trace = telemetry.begin_run(
            run.id,
            executor_id=INPROC_EXECUTOR,
            capture_level=telemetry.resolve_capture(None),
            buffered=True,  # in-process walker: buffer + flush once at finish (no per-step DB I/O)
        )

        if req.stream:
            return await _stream_run(run, req.input, messages, req.params, run_trace)
        return await _complete_run(run, req.input, messages, req.params, run_trace)

    async def _stream_run(
        run: Run,
        user_input: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        run_trace: Any,
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
            await run_trace.finish(status="err", error=f"{code}: {message}")
            logger.warning("run.failed", extra={"run_id": run.id, "code": code, "status": status})
            return _error(message, status=status, code=code, run_id=run.id)
        except APIConnectionError as exc:
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await run_trace.finish(status="err", error=str(exc))
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
                first_token_ns: int | None = None
                # M17: the generation is the run's single ``llm`` node span (under the run-root) —
                # the gap before it is the open_stream/engine latency; the bar is the generation.
                async with run_trace.node_span(_PROMPT_NODE) as scope:
                    async for chunk in upstream:
                        if not chunk.choices:
                            continue
                        choice = chunk.choices[0]
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason
                        delta = choice.delta
                        if delta is None:
                            continue
                        # A reasoning model streams `reasoning_content` (its thinking) before
                        # `content` (its answer). Forward the thinking as visible progress so it
                        # doesn't look frozen, but NEVER into `output` — only content is the answer.
                        reasoning = getattr(delta, "reasoning_content", None)
                        if reasoning:
                            yield _sse("reasoning", {"runId": run.id, "reasoning": reasoning})
                        content = getattr(delta, "content", None)
                        if content:
                            if first_token_ns is None:
                                first_token_ns = now_ns()
                            output += content
                            yield _sse("delta", {"runId": run.id, "delta": content})
                    scope.set_io(inputs={"prompt": user_input}, outputs={"output": output})
                    attrs: dict[str, Any] = {"gen_ai.request.model": run.model}
                    if finish_reason:
                        attrs["gen_ai.response.finish_reason"] = finish_reason
                    if first_token_ns is not None:
                        attrs["ttft_ms"] = round((first_token_ns - scope.span.start_ns) / 1e6, 1)
                    scope.set_attributes(attrs)
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
                await run_trace.finish(status="ok", error=empty_reason)
                terminal = True
                logger.info("run.completed", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "completed"})
                yield "data: [DONE]\n\n"
            except Exception as exc:  # inference died mid-stream (§4): fail cleanly.
                # A failed run contributes no turns — write nothing to the thread, so it
                # never holds a dangling user turn with no answer. The run row records it.
                async with tx() as s:
                    await store.set_status(s, run.id, "failed", error=str(exc))
                await run_trace.finish(status="err", error=str(exc))
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
                        await run_trace.finish(
                            status="err", error="interrupted: client disconnected mid-stream"
                        )

        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _complete_run(
        run: Run,
        user_input: str,
        messages: list[dict[str, Any]],
        params: dict[str, Any],
        run_trace: Any,
    ) -> Any:
        output = ""
        finish_reason: str | None = None
        try:
            # M17: the single LLM call is the run's ``llm`` node span (the trace's one bar).
            async with run_trace.node_span(_PROMPT_NODE) as scope:
                completion = await gw.complete(
                    model=run.model, messages=messages, params=params, extra_headers=_headers(run)
                )
                choice = completion.choices[0] if completion.choices else None
                output = (choice.message.content if choice else "") or ""
                finish_reason = choice.finish_reason if choice else None
                scope.set_io(inputs={"prompt": user_input}, outputs={"output": output})
                scope.set_attributes(
                    {
                        "gen_ai.request.model": run.model,
                        "gen_ai.response.finish_reason": finish_reason,
                    }
                )
        except APIStatusError as exc:
            status, code, message = _map_inference_error(exc)
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=f"{code}: {message}")
            await run_trace.finish(status="err", error=f"{code}: {message}")
            return _error(message, status=status, code=code, run_id=run.id)
        except APIConnectionError as exc:
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await run_trace.finish(status="err", error=str(exc))
            return _error(
                "inference plane unreachable",
                status=503,
                code="inference_unreachable",
                run_id=run.id,
            )

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
        await run_trace.finish(status="ok", error=empty_reason)
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

    @app.post("/runs/{run_id}/resume", dependencies=[Depends(require_token)])
    async def resume_run(run_id: str, req: ResumeRunRequest) -> Any:
        # M14 §1.1: the durable human-in-the-loop entry. Deliver the awaited input to a `waiting`
        # run → DBOS.send to the checkpointed workflow, which resumes from the recv point (even
        # across a worker restart). Token-authed like /invoke — an unattended control surface.
        # Durable mode only; non-durable runs can't durably wait, so resume is a 400 there (honest).
        runtime = getattr(app.state, "durable_runtime", None)
        if runtime is None:
            return _error(
                "resume requires durable mode (the human node lowers onto DBOS.recv/send)",
                status=400,
                code="durable_required",
            )
        async with tx() as session:
            run = await store.get_run(session, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        if run.status != "waiting":
            return _error(
                f"run {run_id!r} is {run.status!r}, not waiting — nothing to resume",
                status=409,
                code="run_not_waiting",
            )
        # Validate the awaited input against the human node's declared schema (m14.md §1.1).
        # Advisory in M14: resolve the run's pinned IR, find the waiting node, and — if it declares
        # an input_schema with required keys — require the input to carry them. A loud 422 beats a
        # silently-wrong resume; richer JSON-Schema validation is a later additive tightening.
        invalid = await _validate_resume_input(run, req.input)
        if invalid is not None:
            return invalid
        await runtime.resume(run_id, req.input)
        logger.info("run.resumed", extra={"run_id": run_id, "node_id": run.awaiting_node})
        return JSONResponse(
            {"runId": run_id, "status": "resuming", "awaitingNode": run.awaiting_node},
            status_code=202,
        )

    async def _validate_resume_input(run: Run, value: Any) -> JSONResponse | None:
        # Resolve the run's pinned IR (content_hash > version) and find the awaiting human node; if
        # it declares input_schema.required, the delivered value must be an object with those keys.
        if not run.graph_id or not run.awaiting_node:  # pragma: no cover - waiting implies both set
            return None
        ir, err = await _resolve_agent_ir(
            run.graph_id, version=run.graph_version, content_hash_pin=run.content_hash
        )
        if err is not None or ir is None:  # pragma: no cover - the run's pin was valid at create
            return None
        node = next((n for n in ir.nodes if n.id == run.awaiting_node), None)
        if node is None or node.type != "human":  # pragma: no cover - awaiting_node is a human node
            return None
        schema = (node.config or {}).get("inputSchema") or (node.config or {}).get("input_schema")
        required = schema.get("required") if isinstance(schema, dict) else None
        if isinstance(required, list) and required:
            if not isinstance(value, dict) or any(k not in value for k in required):
                return _error(
                    f"resume input does not match node {run.awaiting_node!r} schema: requires "
                    f"keys {sorted(required)}",
                    status=422,
                    code="resume_schema_mismatch",
                )
        return None

    # ── theygent-native API: the run waterfall (M17 — observability) ─────
    # Additive READ surface over the span/node_io the capture wrapper persisted (§5). The in-UI
    # waterfall needs only /trace (bars + gaps, no payloads); /io is the lazy click-through; both
    # pass through the single ``authorize`` chokepoint (§1.9). /runs, /graphs/runs, /agents/* are
    # untouched (§8 the Do-NOT). No external trace backend — the UI reads theygent's own store (§0).

    @app.get("/runs/{run_id}/trace", dependencies=[Depends(require_auth)])
    async def get_run_trace(run_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        # The waterfall payload: the run's span tree (timing + status + worker attribution + scalar
        # attrs + edge sizes), ordered by start_ns. NO payloads — those are the lazy /io call
        # (§1.3).
        run = await store.get_run(session, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        if not authorize(
            LOCAL_PRINCIPAL, "trace:read", run
        ):  # the §1.9 chokepoint (no-op allow now)
            return _error("not permitted to read this run's trace", status=403, code="forbidden")
        telemetry: Telemetry = app.state.telemetry
        spans = await telemetry.trace_store.list_spans(session, run_id)
        return {
            "runId": run_id,
            "status": run.status,
            "spans": [s.model_dump(mode="json") for s in spans],
        }

    @app.get("/runs/{run_id}/trace/stream", dependencies=[Depends(require_auth)])
    async def stream_run_trace(run_id: str) -> Any:
        # The LIVE waterfall (§5/§6): subscribe to the in-process SpanBus and relay span open/close
        # events as they happen, so the waterfall grows during a streaming run (the in-flight amber
        # bar settles green/red on its close event). The durable record is /trace; this is the
        # ephemeral live view (mirrors the token DeltaBus ↔ persisted-output relationship, D7).
        async with tx() as s:
            run = await store.get_run(s, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        if not authorize(LOCAL_PRINCIPAL, "trace:read", run):
            return _error("not permitted to read this run's trace", status=403, code="forbidden")
        telemetry = app.state.telemetry
        bus = telemetry.bus
        queue = bus.subscribe(run_id)

        async def gen() -> AsyncIterator[str]:
            try:
                # If the run already terminalized, there will be no future events — close at once so
                # the client falls back to the static /trace snapshot (no hang). A small race window
                # is harmless: late events were captured by the snapshot.
                if run.status in ("completed", "failed"):
                    yield _sse("done", {"runId": run_id, "status": run.status})
                    return
                while True:
                    event = await queue.get()
                    if event is None:  # the run finished (bus.close sentinel)
                        yield _sse("done", {"runId": run_id})
                        return
                    yield _sse(f"span.{event.kind}", event.payload)
            finally:
                bus.unsubscribe(run_id, queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/runs/{run_id}/nodes/{node_id}/io", dependencies=[Depends(require_auth)])
    async def get_node_io(
        run_id: str, node_id: str, session: AsyncSession = Depends(get_session)
    ) -> Any:
        # The click-through (§5/§6): the full per-node I/O, lazy. Gated states are NOT errors — the
        # timeline stays legible; only payloads are gated, with a clear reason: not-permitted
        # (authz)
        # vs metadata (sizes only) vs off (capture disabled) vs simply not-captured. Never a 500.
        run = await store.get_run(session, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        telemetry: Telemetry = app.state.telemetry
        io = await telemetry.io_store.get_io(session, run_id, node_id)
        # Authz: io:read denial returns the timing/sizes shape with payloads omitted + a reason —
        # "captured but not visible to you" is a DIFFERENT message from "not captured" (§6).
        if not authorize(LOCAL_PRINCIPAL, "io:read", run):
            level = io.capture_level if io is not None else await _effective_capture(run.graph_id)
            return _io_response(
                run_id,
                node_id,
                level,
                inputs=None,
                outputs=None,
                bytes_in=io.bytes_in if io else 0,
                bytes_out=io.bytes_out if io else 0,
                truncated=io.truncated if io else False,
                reason="You don't have access to this run's context",
            )
        if io is not None:
            reason = (
                None
                if io.capture_level == "full"
                else "Sizes only — full I/O capture is off for this agent"
            )
            return _io_response(
                run_id,
                node_id,
                io.capture_level,
                inputs=io.inputs,
                outputs=io.outputs,
                bytes_in=io.bytes_in,
                bytes_out=io.bytes_out,
                truncated=io.truncated,
                reason=reason,
            )
        # No row: either capture is off for this agent, or the node didn't run / wasn't captured.
        level = await _effective_capture(run.graph_id)
        reason = (
            "I/O capture is disabled for this agent"
            if level == "off"
            else "No I/O captured for this node"
        )
        return _io_response(run_id, node_id, level, inputs=None, outputs=None, reason=reason)

    async def _effective_capture(agent_id: str | None) -> CaptureLevel:
        telemetry: Telemetry = app.state.telemetry
        return await telemetry.effective_capture_for(agent_id)

    def _io_response(
        run_id: str,
        node_id: str,
        capture_level: CaptureLevel,
        *,
        inputs: dict[str, Any] | None,
        outputs: dict[str, Any] | None,
        bytes_in: int = 0,
        bytes_out: int = 0,
        truncated: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        # snake_case, matching SpanView / Run / the existing theygent API convention.
        return {
            "run_id": run_id,
            "node_id": node_id,
            "capture_level": capture_level,
            "inputs": inputs,
            "outputs": outputs,
            "bytes_in": bytes_in,
            "bytes_out": bytes_out,
            "truncated": truncated,
            "reason": reason,
        }

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
        ir: IRDocument,
        *,
        input_value: Any,
        thread_id: str | None,
        stream: bool,
        trigger_id: str | None = None,
    ) -> Any:
        """Run a *validated* IRDocument through the M5 walker — the one execution path /graphs/runs
        (inline IR), /agents/{id}/runs + /agents/{id}/invoke (saved IR), and the M12 trigger
        ``fire`` seam all use (m11.md §3/§5, m12.md §3). Assumes the IR is shape/graph-valid; runs
        the up-front engine/tool/MCP membership checks (which depend on live control-plane state, so
        they run per-invocation, not at store time), creates the Run with the graph's recorded
        identity (§4) and the firing ``trigger_id`` (M12 §2, None = interactive), and
        streams/completes. No new execution code — only new *initiators*."""

        # Resolve every llm node's model up front and reject an engine-name binding before anything
        # is created or sent — the §8.4/§3.2 logical-id invariant on the graph path.
        try:
            llms = llm_models(ir)
            # M19 §1.3: transcribe/speak are logical-id-only too — reject an engine-name binding
            # up front, exactly like an llm (the audio negative test the §4 contract extends).
            audio_model_nodes(ir)
        except EngineNameNotAllowed as exc:
            return _error(str(exc), status=400, code="engine_name_not_allowed")

        # Resolve every tool node's name up front (m6.md §5 + M19 §2.3): valid if it is a key in the
        # IR's ``tools`` block (an M19 http/builtin binding) OR a directly-registered builtin —
        # otherwise a 400 here, before a Run exists. The connection a binding references is resolved
        # at STEP TIME (a dangling connection binds ``err`` honestly, never a create-time block —
        # its
        # id is an environment binding the importer re-maps, §1.1), so it is NOT checked here.
        for node, tool_name in tool_nodes(ir):
            if tool_name not in ir.tools and tool_name not in DEFAULT_REGISTRY:
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

        # Durable-only node types (human/subgraph/loop/map) cannot run on this interactive walker —
        # reject up front with a clear, actionable error (before a Run), not the walker's raw
        # NotImplementedError mis-mapped to a 502 "not implemented yet" (M14: these run on the
        # durable runtime via a trigger/worker, not the interactive run path).
        durable_only = sorted({n.id for n in ir.nodes if n.type in _DURABLE_ONLY_TYPES})
        if durable_only:
            kinds = sorted({n.type for n in ir.nodes if n.type in _DURABLE_ONLY_TYPES})
            return _error(
                f"node(s) {durable_only} use durable-only type(s) {kinds} "
                "(human/subgraph/loop/map) — these run on the durable runtime, not the interactive "
                "run path. Deploy the agent and invoke it via a durable trigger (schedule/webhook) "
                "or a durable worker.",
                status=400,
                code="durable_required",
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
                trigger_id=trigger_id,
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

        # M17: open the run's observability trace. The effective capture level is resolved ONCE per
        # run (§4) = deployment ceiling ∧ topology default ∧ the agent's policy (ir.id == the saved
        # agent id for a saved-agent run; None for an inline /graphs/runs → the topology default).
        # The interactive walker is stamped with the ``inproc`` executor (worker attribution, §1).
        telemetry: Telemetry = app.state.telemetry
        capture: CaptureLevel = await telemetry.effective_capture_for(ir.id)
        run_trace = telemetry.begin_run(
            run.id,
            executor_id=INPROC_EXECUTOR,
            capture_level=capture,
            buffered=True,  # in-process walker: buffer + flush once at finish (no per-step DB I/O)
        )
        ctx = WalkContext(
            gateway=gw,
            run_id=run.id,
            prior_messages=prior,
            extra_headers=_headers(run),
            mcp=mcp,
            run_trace=run_trace,
            tool_auth=app.state.tool_resolver,  # M19 §1.1: server-side connection auth resolution
            gates=app.state.gates,  # M19 §2.8: ratelimit/quota backend
            artifacts=app.state.artifacts,  # M19 §2.2: transcribe/speak audio reference store
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
            await _finish_trace(ctx, "err", f"{code}: {message}")
            logger.warning("graph_run.failed", extra={"run_id": run.id, "code": code})
            return _error(message, status=status, code=code, run_id=run.id)
        except APIConnectionError as exc:
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(
                "inference plane unreachable",
                status=503,
                code="inference_unreachable",
                run_id=run.id,
            )
        except RouterError as exc:  # router failed before any stream committed: clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="router_error", run_id=run.id)
        except TemplateError as exc:  # a bad $in token in an llm node (m9.md §1.2): clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="template_error", run_id=run.id)
        except TransformError as exc:  # a bad transform expr/ref (m19.md §2.9): clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="transform_error", run_id=run.id)

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
                await _finish_trace(ctx, "ok", result.empty_reason)
                logger.info("graph_run.completed", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "completed"})
                yield "data: [DONE]\n\n"
            except Exception as exc:  # inference died / router failed mid-walk: fail cleanly.
                async with tx() as s:
                    await store.set_status(s, run.id, "failed", error=str(exc))
                await _finish_trace(ctx, "err", str(exc))
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
                        await _finish_trace(
                            ctx, "err", "interrupted: client disconnected mid-stream"
                        )

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
            await _finish_trace(ctx, "err", f"{code}: {message}")
            return _error(message, status=status, code=code, run_id=run.id)
        except APIConnectionError as exc:
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(
                "inference plane unreachable",
                status=503,
                code="inference_unreachable",
                run_id=run.id,
            )
        except RouterError as exc:  # router named a missing handle / no handle field (§3.2).
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="router_error", run_id=run.id)
        except TemplateError as exc:  # a bad $in token in an llm node (m9.md §1.2): clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="template_error", run_id=run.id)
        except TransformError as exc:  # a bad transform expr/ref (m19.md §2.9): clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="transform_error", run_id=run.id)
        except Exception as exc:  # mid-walk drop on the non-stream path: fail cleanly, no turn.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
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
        await _finish_trace(ctx, "ok", result.empty_reason)
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

    @app.get("/agents/{agent_id}/io-policy", dependencies=[Depends(require_auth)])
    async def get_io_policy(agent_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        # M17 §5/§6: the effective + stored capture policy. ``effective`` is what actually happens
        # (ceiling ∧ topology ∧ stored), so the agent-settings control shows the real behavior —
        # e.g.
        # "Full requested; capped to Sizes only by this deployment". Through the authz chokepoint.
        if await agents.get_agent(session, agent_id) is None:
            return _error(f"unknown agent {agent_id!r}", status=404, code="agent_not_found")
        if not authorize(LOCAL_PRINCIPAL, "agent:configure", agent_id):
            return _error("not permitted to view this agent's policy", status=403, code="forbidden")
        telemetry: Telemetry = app.state.telemetry
        row = await telemetry.policy_store.get_policy(session, agent_id)
        view = telemetry.policy_store.view(
            agent_id=agent_id,
            row=row,
            ceiling=telemetry.ceiling,
            topo_default=telemetry.topology_default,
        )
        return view.model_dump(mode="json")

    @app.put("/agents/{agent_id}/io-policy", dependencies=[Depends(require_auth)])
    async def put_io_policy(agent_id: str, req: IoPolicyRequest) -> Any:
        # Set the capture policy (§1.8). Gated on ``agent:configure`` (the §1.9 chokepoint). Editing
        # it does NOT mint a new agent version — the policy lives beside the agent row, not in the
        # hashed IR (M11 immutability), so ``contentHash`` is unchanged (surfaced in the UI copy).
        telemetry: Telemetry = app.state.telemetry
        if not authorize(LOCAL_PRINCIPAL, "agent:configure", agent_id):
            return _error("not permitted to configure this agent", status=403, code="forbidden")
        async with tx() as session:
            if await agents.get_agent(session, agent_id) is None:
                return _error(f"unknown agent {agent_id!r}", status=404, code="agent_not_found")
            row = await telemetry.policy_store.upsert_policy(
                session,
                agent_id=agent_id,
                io_capture=req.io_capture,
                io_retention_seconds=req.io_retention_seconds,
                redact_rules=req.redact_rules,
                updated_by=LOCAL_PRINCIPAL.id,  # NULL in single-user; the auth milestone fills it
            )
            view = telemetry.policy_store.view(
                agent_id=agent_id,
                row=row,
                ceiling=telemetry.ceiling,
                topo_default=telemetry.topology_default,
            )
        logger.info(
            "agent.io_policy_set", extra={"agent_id": agent_id, "io_capture": req.io_capture}
        )
        return view.model_dump(mode="json")

    async def _resolve_agent_ir(
        agent_id: str, *, version: str | None, content_hash_pin: str | None
    ) -> tuple[IRDocument | None, JSONResponse | None]:
        """Resolve a saved agent's IR by reference (m11.md §3/§5): pinned ``content_hash`` >
        pinned ``version`` > latest. A dangling pin (typo'd hash, missing version) or unknown agent
        is a clean **404**, never a 500/hang — an unattended M12 trigger with a stale pin must fail
        honestly (m11.md §1 review #2 / m12.md §1.1). Returns ``(ir, None)`` on success or
        ``(None, error_response)``; the one resolver shared by /agents/{id}/runs, the unattended
        /agents/{id}/invoke, ``fire`` (triggers), and trigger creation."""
        async with tx() as session:
            agent_exists = await agents.get_agent(session, agent_id) is not None
            if content_hash_pin:
                sv = await agents.get_version_by_hash(session, agent_id, content_hash_pin)
            elif version:
                sv = await agents.get_version(session, agent_id, version)
            else:
                sv = await agents.latest_version(session, agent_id)
        if sv is None:
            if not agent_exists:
                return None, _error(
                    f"unknown agent {agent_id!r}", status=404, code="agent_not_found"
                )
            pinned = content_hash_pin or version
            detail = f"version {pinned!r}" if pinned else "any published version"
            return None, _error(
                f"agent {agent_id!r} has no {detail}", status=404, code="agent_version_not_found"
            )
        # The stored IR was validated at store time; re-parse (and validate) so the walker always
        # gets a validated IRDocument (the same precondition /graphs/runs guarantees).
        try:
            ir = parse_document(sv.ir)
            validate_graph(ir)
        except (ValidationError, GraphValidationError) as exc:  # pragma: no cover - stored valid
            return None, _error(str(exc), status=400, code="invalid_ir")
        return ir, None

    async def fire(trigger: Trigger, input_value: Any) -> Any:
        """The ONE firing seam (m12.md §1.2/§3) — the hard-to-reverse boundary M13 re-points at the
        durable worker. All three trigger kinds (http via /invoke, schedule via the dispatcher,
        webhook via /hooks) converge here: resolve the trigger's *pinned, saved* agent (never inline
        IR — §1.1) and run it through the EXISTING M5 walker path (``_execute_ir_run``, non-stream),
        stamping ``trigger_id`` for lineage (§2). No new execution code — only a new initiator. A
        run whose bound inference plane is unreachable ends ``failed`` cleanly inside the walker
        path (§1.4 honest failure), never a hang. Returns the non-stream run result dict, or an
        ``_error`` JSONResponse the caller relays / the dispatcher logs."""
        # M13 §4: in durable mode the fire seam enqueues theygent_run on the DBOS durable queue and
        # awaits its terminal result — the run now survives a crash and resumes (D2/D3). The trigger
        # definition, API, and rows are untouched; only the dispatch mechanism moved. Non-durable
        # mode keeps the M12 in-process walk (a mid-run crash is reconciled to failed, not resumed).
        runtime = getattr(app.state, "durable_runtime", None)
        if runtime is not None:
            logger.info(
                "trigger.fire_durable",
                extra={
                    "trigger_id": trigger.id,
                    "agent_id": trigger.agent_id,
                    "kind": trigger.kind,
                },
            )
            return await runtime.fire(trigger, input_value)
        ir, err = await _resolve_agent_ir(
            trigger.agent_id, version=trigger.version, content_hash_pin=trigger.content_hash
        )
        if err is not None:
            return err
        assert ir is not None
        logger.info(
            "trigger.fire",
            extra={"trigger_id": trigger.id, "agent_id": trigger.agent_id, "kind": trigger.kind},
        )
        return await _execute_ir_run(
            ir, input_value=input_value, thread_id=None, stream=False, trigger_id=trigger.id
        )

    @app.post("/agents/{agent_id}/runs", dependencies=[Depends(require_auth)])
    async def run_agent(agent_id: str, req: AgentRunRequest) -> Any:
        # Invoke-by-reference (m11.md §3/§5): resolve the agent's IR (pinned contentHash > pinned
        # version > latest), then run it through the EXISTING walker path (_execute_ir_run) — same
        # SSE relay, same Run row (recording the agent's graph_id/graph_version/contentHash), same
        # thread memory. The only new thing is *where the IR came from*: the registry, not the body.
        ir, err = await _resolve_agent_ir(
            agent_id, version=req.version, content_hash_pin=req.content_hash
        )
        if err is not None:
            return err
        assert ir is not None
        logger.info("agent.invoked", extra={"agent_id": agent_id})
        return await _execute_ir_run(
            ir, input_value=req.input, thread_id=req.thread_id, stream=req.stream
        )

    # ── theygent-native API: /triggers + invoke + hooks (M12 — the deploy primitive) ──
    # The gap between "an agent exists" (M11) and "an agent runs without a human in the cockpit"
    # (m12.md §0). Triggers fire SAVED, PINNED agents (§1.1) through the one ``fire`` seam over the
    # EXISTING run path (§3) — M12 builds new *initiators*, NOT the durable runtime (a mid-run crash
    # is still reconciled to ``failed``, M9 §2.1). All additive; /runs, /graphs/runs,
    # /agents/{id}/runs untouched (§6).

    def _validate_trigger(
        kind: TriggerKind, version: str | None, content_hash_pin: str | None, config: dict[str, Any]
    ) -> JSONResponse | None:
        # §1.1: a trigger pins EXACTLY ONE of version / content_hash — an unattended deploy runs an
        # immutable artifact, never "latest" (which could silently change under it).
        if bool(version) == bool(content_hash_pin):
            return _error(
                "a trigger must pin exactly one of `version` or `content_hash` (§1.1: an "
                "unattended deploy runs an immutable artifact)",
                status=400,
                code="invalid_trigger",
            )
        if kind == "schedule":
            cron = config.get("cron")
            if not isinstance(cron, str) or not cron_is_valid(cron):
                return _error(
                    "a `schedule` trigger requires config.cron (a valid cron expression)",
                    status=400,
                    code="invalid_trigger",
                )
        elif kind == "webhook":
            secret = config.get("secret")
            if not isinstance(secret, str) or not secret:
                return _error(
                    "a `webhook` trigger requires config.secret (the inbound signing secret)",
                    status=400,
                    code="invalid_trigger",
                )
        return None

    async def _sync_schedule(trigger: Trigger) -> None:
        # M13 §4: mirror a schedule trigger's state into a DBOS dynamic schedule (durable mode).
        # theygent's `trigger` row stays the source of truth; this keeps the DBOS schedule aligned
        # on every create/patch/delete so an enabled schedule fires and a disabled one does not.
        runtime = getattr(app.state, "durable_runtime", None)
        if runtime is None or trigger.kind != "schedule":
            return
        # Delete-then-recreate applies a cron edit AND the enabled flag in one path (upsert alone
        # wouldn't pick up a changed cron on an existing schedule).
        await runtime.delete_schedule(trigger.id)
        if trigger.enabled:
            await runtime.upsert_schedule(trigger)

    async def _drop_schedule(trigger_id: str) -> None:
        runtime = getattr(app.state, "durable_runtime", None)
        if runtime is not None:
            await runtime.delete_schedule(trigger_id)

    @app.post("/triggers", dependencies=[Depends(require_auth)])
    async def create_trigger(req: CreateTriggerRequest) -> Any:
        invalid = _validate_trigger(req.kind, req.version, req.content_hash, req.config)
        if invalid is not None:
            return invalid
        # The pinned agent must resolve NOW — registering a deploy of a nonexistent/dangling pin is
        # a 404 here, not a surprise at fire time (§1.1; the same honest-pin discipline as invoke).
        _ir, err = await _resolve_agent_ir(
            req.agent_id, version=req.version, content_hash_pin=req.content_hash
        )
        if err is not None:
            return err
        async with tx() as session:
            trigger = await triggers.create(
                session,
                agent_id=req.agent_id,
                kind=req.kind,
                version=req.version,
                content_hash=req.content_hash,
                config=req.config,
                enabled=req.enabled,
            )
        await _sync_schedule(trigger)
        logger.info(
            "trigger.created",
            extra={"trigger_id": trigger.id, "agent_id": req.agent_id, "kind": req.kind},
        )
        return JSONResponse(trigger.public_dump(), status_code=201)

    @app.get("/triggers", dependencies=[Depends(require_auth)])
    async def list_triggers(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        rows = await triggers.list_triggers(session, limit=limit, before=before)
        return {"triggers": [t.public_dump() for t in rows]}

    @app.get("/triggers/{trigger_id}", dependencies=[Depends(require_auth)])
    async def get_trigger(trigger_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        trigger = await triggers.get(session, trigger_id)
        if trigger is None:
            return _error(f"unknown trigger {trigger_id!r}", status=404, code="trigger_not_found")
        return trigger.public_dump()

    @app.patch("/triggers/{trigger_id}", dependencies=[Depends(require_auth)])
    async def patch_trigger(trigger_id: str, req: UpdateTriggerRequest) -> Any:
        async with tx() as session:
            existing = await triggers.get(session, trigger_id)
            if existing is None:
                return _error(
                    f"unknown trigger {trigger_id!r}", status=404, code="trigger_not_found"
                )
            # A config edit is re-validated against the (immutable) kind + pin — a webhook can't
            # lose its secret, a schedule can't get a bad cron (§3: enable/disable + edit config).
            if req.config is not None:
                invalid = _validate_trigger(
                    existing.kind, existing.version, existing.content_hash, req.config
                )
                if invalid is not None:
                    return invalid
            updated = await triggers.update(
                session, trigger_id, enabled=req.enabled, config=req.config
            )
        assert updated is not None
        await _sync_schedule(updated)  # M13: enable/disable + cron edits re-align the DBOS schedule
        logger.info("trigger.updated", extra={"trigger_id": trigger_id, "enabled": updated.enabled})
        return updated.public_dump()

    @app.delete("/triggers/{trigger_id}", dependencies=[Depends(require_auth)])
    async def delete_trigger(trigger_id: str) -> Response:
        async with tx() as session:
            deleted = await triggers.delete(session, trigger_id)
        if not deleted:
            return _error(f"unknown trigger {trigger_id!r}", status=404, code="trigger_not_found")
        await _drop_schedule(trigger_id)  # M13: drop the DBOS schedule alongside the trigger row
        return Response(status_code=204)

    # ── M19 §1.1: the connection resource (tool/MCP auth seam) ────────────────────────────────────
    # The ONLY new control-plane routes M19 adds (§3). A connection holds NON-SECRET config + a
    # secret_ref; the secret material is written to the encrypted secret store in the SAME
    # transaction and NEVER returned over the wire (public_dump redacts to hasSecret). Auth
    # resolution is server-side at step time (no per-node apply endpoint — §1.1 / §3).

    def _validate_connection(kind: ConnectionKind, config: dict[str, Any]) -> JSONResponse | None:
        """Reject a connection whose config is malformed or smuggles a secret (§1.1: secrets go in
        the write-only ``secret`` field, never ``config``). Returns an error response or None."""
        for key in _SECRETISH_CONFIG_KEYS:
            if key in config:
                return _error(
                    f"connection config must not contain the secret key {key!r}; pass the "
                    "credential as the write-only `secret` field (it is encrypted server-side, "
                    "never stored in the IR or the connection row)",
                    status=400,
                    code="invalid_connection",
                )
        if kind == "mcp_server":
            transport = config.get("transport", "stdio")
            if transport not in ("stdio", "http"):
                return _error(
                    f"mcp_server connection transport must be 'stdio' or 'http', got {transport!r}",
                    status=400,
                    code="invalid_connection",
                )
            if transport == "stdio" and not config.get("command"):
                return _error(
                    "mcp_server stdio connection requires a 'command' in config",
                    status=400,
                    code="invalid_connection",
                )
            if transport == "http" and not config.get("url"):
                return _error(
                    "mcp_server http connection requires a 'url' in config",
                    status=400,
                    code="invalid_connection",
                )
        return None

    @app.post("/connections", status_code=201, dependencies=[Depends(require_auth)])
    async def create_connection(req: CreateConnectionRequest) -> Any:
        invalid = _validate_connection(req.kind, req.config)
        if invalid is not None:
            return invalid
        # Secret + connection in ONE transaction: the secret is encrypted into the secret store and
        # the connection row stores only the resulting secret_ref (never the plaintext — §1.1).
        async with tx() as session:
            secret_ref = await secrets.create(session, req.secret) if req.secret else None
            conn = await connections.create(
                session,
                name=req.name,
                kind=req.kind,
                config=req.config,
                secret_ref=secret_ref,
                enabled=req.enabled,
            )
        logger.info("connection.created", extra={"connection_id": conn.id, "kind": conn.kind})
        return JSONResponse(conn.public_dump(), status_code=201)

    @app.get("/connections", dependencies=[Depends(require_auth)])
    async def list_connections(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        rows = await connections.list_connections(session, limit=limit, before=before)
        return {"connections": [c.public_dump() for c in rows]}

    @app.get("/connections/{connection_id}", dependencies=[Depends(require_auth)])
    async def get_connection(
        connection_id: str, session: AsyncSession = Depends(get_session)
    ) -> Any:
        conn = await connections.get(session, connection_id)
        if conn is None:
            return _error(
                f"unknown connection {connection_id!r}",
                status=404,
                code="connection_not_found",
            )
        return conn.public_dump()

    @app.patch("/connections/{connection_id}", dependencies=[Depends(require_auth)])
    async def patch_connection(connection_id: str, req: UpdateConnectionRequest) -> Any:
        async with tx() as session:
            existing = await connections.get(session, connection_id)
            if existing is None:
                return _error(
                    f"unknown connection {connection_id!r}",
                    status=404,
                    code="connection_not_found",
                )
            if req.config is not None:
                invalid = _validate_connection(existing.kind, req.config)
                if invalid is not None:
                    return invalid
            # Rotate the secret IN PLACE (same secret_ref) so no agent's contentHash moves (§1.1).
            # A non-empty ``secret`` rotates/attaches; an absent/empty one leaves it untouched.
            secret_ref = existing.secret_ref
            set_secret_ref = False
            if req.secret:
                if secret_ref is None:
                    secret_ref = await secrets.create(session, req.secret)
                    set_secret_ref = True  # newly attached → write the ref onto the row
                else:
                    await secrets.set(session, secret_ref, req.secret)  # same ref, new material
            updated = await connections.update(
                session,
                connection_id,
                name=req.name,
                config=req.config,
                secret_ref=secret_ref,
                set_secret_ref=set_secret_ref,
                enabled=req.enabled,
            )
        assert updated is not None
        logger.info("connection.updated", extra={"connection_id": connection_id})
        return updated.public_dump()

    @app.delete(
        "/connections/{connection_id}", status_code=204, dependencies=[Depends(require_auth)]
    )
    async def delete_connection(connection_id: str) -> Response:
        async with tx() as session:
            deleted = await connections.delete(session, connection_id)
            if deleted is not None:
                # Delete the encrypted secret alongside the connection (one transaction).
                await secrets.delete(session, deleted.secret_ref)
        if deleted is None:
            return _error(
                f"unknown connection {connection_id!r}",
                status=404,
                code="connection_not_found",
            )
        return Response(status_code=204)

    @app.post("/agents/{agent_id}/invoke", dependencies=[Depends(require_token)])
    async def invoke_agent(agent_id: str, req: InvokeRequest) -> Any:
        # The HTTP trigger (§3.1): the token-authed, cockpit-free sibling of /agents/{id}/runs. Same
        # resolution + same shared run path; the ONLY differences are the auth gate (require_token —
        # §1.3) and the non-stream default. Returns the awaited result (stream:false) or an SSE
        # stream (stream:true), either correlatable via GET /runs/{id}. Missing/bad token → 401
        # (handled by the dependency before we get here).
        ir, err = await _resolve_agent_ir(
            agent_id, version=req.version, content_hash_pin=req.content_hash
        )
        if err is not None:
            return err
        assert ir is not None
        logger.info("agent.invoked_unattended", extra={"agent_id": agent_id})
        return await _execute_ir_run(
            ir, input_value=req.input, thread_id=req.thread_id, stream=req.stream
        )

    @app.post("/hooks/{trigger_id}")
    async def fire_hook(trigger_id: str, request: Request) -> Any:
        # The webhook trigger (§3.3): an inbound URL a THIRD-PARTY system POSTs to. Authed by the
        # per-webhook signature (HMAC-SHA256 of the raw body with config.secret), NOT the bearer
        # token — the caller is an external system, not a credentialed user (§1.3). The raw payload
        # maps mechanically to the agent's input (§3.3: payload → input). Bad signature → 401.
        raw = await request.body()
        async with tx() as session:
            trigger = await triggers.get(session, trigger_id)
        if trigger is None or trigger.kind != "webhook":
            return _error(f"unknown webhook {trigger_id!r}", status=404, code="trigger_not_found")
        if not trigger.enabled:
            return _error(
                f"webhook {trigger_id!r} is disabled", status=409, code="trigger_disabled"
            )
        secret = (trigger.config or {}).get("secret")
        signature = request.headers.get("x-theygent-signature")
        if not isinstance(secret, str) or not _verify_signature(secret, raw, signature):
            return _error(
                "invalid or missing webhook signature (X-Theygent-Signature)",
                status=401,
                code="invalid_signature",
            )
        # Mechanical payload → input: the parsed JSON body is the run input (the agent drills it
        # with $in.in.<field> like any object input). A malformed body is a 400, never a 500.
        try:
            input_value = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return _error("webhook body is not valid JSON", status=400, code="invalid_payload")
        result = await fire(trigger, input_value)
        if isinstance(result, JSONResponse):  # a resolution/inference failure — relay it honestly.
            return result
        # Run completed in-process; return the result + run handle as 202 (an accepted, fired run).
        return JSONResponse(result, status_code=202)

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

    # ── M18 bench store: /bench/* ────────────────────────────────────────
    # The bench adds NO execution path (§0): model tests hit the inference data plane DIRECTLY in
    # the user's trust domain (§1.4 / §10 — never proxied here), agent tests reuse M11 invoke. These
    # endpoints only PERSIST what the bench produced — metrics + digests by default, raw capture
    # opt-in and local (§1.6). Listing follows the M8 §2 keyset shape; the error envelope is shared.

    def _suite_dump(suite: BenchSuite) -> dict[str, Any]:
        return suite.model_dump(mode="json")

    @app.post("/bench/suites", dependencies=[Depends(require_auth)])
    async def create_suite(req: CreateSuiteRequest) -> Any:
        suite = BenchSuite(
            name=req.name,
            target_kind=req.target_kind,
            modality=req.modality,
            logical_id=req.logical_id,
            binding=req.binding,
            agent_id=req.agent_id,
            version=req.version,
            content_hash=req.content_hash,
            cases=[
                BenchCase(
                    input=c.input,
                    expected=c.expected,
                    assertion=cast("Any", c.assertion),
                    assertion_config=c.assertion_config,
                    seq=i,
                )
                for i, c in enumerate(req.cases)
            ],
        )
        async with tx() as session:
            await bench.create_suite(session, suite)
        return JSONResponse(_suite_dump(suite), status_code=201)

    @app.get("/bench/suites", dependencies=[Depends(require_auth)])
    async def list_suites(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        rows = await bench.list_suites(session, limit=limit, before=before)
        return {"suites": [_suite_dump(s) for s in rows]}

    @app.get("/bench/suites/{suite_id}", dependencies=[Depends(require_auth)])
    async def get_suite(suite_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        suite = await bench.get_suite(session, suite_id)
        if suite is None:
            return _error(f"unknown suite {suite_id!r}", status=404, code="suite_not_found")
        return _suite_dump(suite)

    @app.post("/bench/runs", dependencies=[Depends(require_auth)])
    async def record_bench_run(req: RecordBenchRunRequest) -> Any:
        # The server owns the digests (§1.6): two runs differing only in a param get different
        # ``params_digest`` and are distinct results. Raw ``output`` is persisted ONLY when capture
        # is opted in (and even then as a local reference — never a blob in cloud topology, §10).
        run = BenchRun(
            target_kind=req.target_kind,
            modality=req.modality,
            logical_id=req.logical_id,
            model_ref=req.model_ref,
            binding=req.binding,
            params=req.params,
            params_digest=params_digest(req.params) if req.target_kind == "model" else None,
            agent_id=req.agent_id,
            version=req.version,
            content_hash=req.content_hash,
            metrics=req.metrics,
            output_digest=output_digest(req.output),
            # Capture is opt-in + local: we record a reference keyed by the result id, NEVER the raw
            # output bytes in this hosted table (§1.6 / §10). The blob store itself is out of M18.
            capture_ref=f"local://bench/{req.run_id or 'adhoc'}" if req.capture else None,
            suite_id=req.suite_id,
            case_id=req.case_id,
            assertion=req.assertion,
            assertion_passed=req.assertion_passed,
            run_id=req.run_id,
            label=req.label,
        )
        async with tx() as session:
            await bench.record_run(session, run)
        return JSONResponse(run.model_dump(mode="json"), status_code=201)

    @app.get("/bench/runs", dependencies=[Depends(require_auth)])
    async def list_bench_runs(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        logical_id: str | None = Query(default=None),
        agent_id: str | None = Query(default=None),
        suite_id: str | None = Query(default=None),
        case_id: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        rows = await bench.list_runs(
            session,
            limit=limit,
            before=before,
            logical_id=logical_id,
            agent_id=agent_id,
            suite_id=suite_id,
            case_id=case_id,
        )
        return {"runs": [r.model_dump(mode="json") for r in rows]}

    @app.get("/bench/compare", dependencies=[Depends(require_auth)])
    async def compare_bench_runs(
        a: str = Query(...),
        b: str = Query(...),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        # Two recorded results side by side (§2.4) — the A/B-versions + binding-swap headline. Each
        # side is a ``bench_run`` (it pins its own target), so this aligns metrics + reports whether
        # the outputs match (by digest, no raw payload). The deltas are computed on the metric keys
        # both sides share.
        run_a = await bench.get_run(session, a)
        run_b = await bench.get_run(session, b)
        if run_a is None or run_b is None:
            missing = a if run_a is None else b
            return _error(f"unknown bench run {missing!r}", status=404, code="bench_run_not_found")
        deltas: dict[str, Any] = {}
        for key in set(run_a.metrics) & set(run_b.metrics):
            va, vb = run_a.metrics.get(key), run_b.metrics.get(key)
            if isinstance(va, int | float) and isinstance(vb, int | float):
                deltas[key] = vb - va
        return {
            "a": run_a.model_dump(mode="json"),
            "b": run_b.model_dump(mode="json"),
            "metric_deltas": deltas,
            "outputs_match": (
                run_a.output_digest is not None and run_a.output_digest == run_b.output_digest
            ),
        }

    @app.post("/bench/presets", dependencies=[Depends(require_auth)])
    async def create_preset(req: CreatePresetRequest) -> Any:
        preset = BenchPreset(
            name=req.name,
            modality=req.modality,
            logical_id=req.logical_id,
            params=req.params,
        )
        async with tx() as session:
            await bench.create_preset(session, preset)
        return JSONResponse(preset.model_dump(mode="json"), status_code=201)

    @app.get("/bench/presets", dependencies=[Depends(require_auth)])
    async def list_presets(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        modality: str | None = Query(default=None),
        logical_id: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        rows = await bench.list_presets(
            session, limit=limit, before=before, modality=modality, logical_id=logical_id
        )
        return {"presets": [p.model_dump(mode="json") for p in rows]}

    @app.delete("/bench/presets/{preset_id}", dependencies=[Depends(require_auth)])
    async def delete_preset(preset_id: str) -> Response:
        async with tx() as session:
            deleted = await bench.delete_preset(session, preset_id)
        if not deleted:
            return _error(f"unknown preset {preset_id!r}", status=404, code="preset_not_found")
        return Response(status_code=204)

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
