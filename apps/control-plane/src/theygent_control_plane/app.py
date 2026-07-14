"""The control-plane API — theygent-native, owns one run end to end.

Surfaces (theygent-native, deliberately NOT OpenAI-shaped — the OpenAI-compat
surface lives on the inference plane):

  * POST /runs            create + execute a run; SSE stream when stream:true.
                          Optional ``session_id`` opts the run into conversational memory.
  * GET  /runs/{run_id}   run status (now read from Postgres — survives a restart)
  * GET  /healthz         liveness
  * GET  /readyz          readiness — can it reach the inference plane AND Postgres?

The control-plane reaches inference **only** over HTTP via the gateway-client (the
one hard-to-reverse rule). Postgres is the control-plane store: runs persist
and chat sessions replay prior turns. Two DB-session paths: read handlers take a request-
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
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession
from theygent_gateway_client import GatewayClient
from theygent_ir import (
    EXECUTABLE_TYPES,
    GraphValidationError,
    GuardrailConfig,
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
from theygent_control_plane.rag import IngestService, RagRetriever, RagStore
from theygent_control_plane.rag.ingest import IngestBusy
from theygent_control_plane.rag.parse import supported_extension
from theygent_control_plane.rag.retrieve import RetrievalError
from theygent_control_plane.rag.store import RagSourceKind
from theygent_control_plane.reasoning import ThinkSplitter, split_think
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
    new_ulid,
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
    read_usage,
    resolve_model_key,
    tool_nodes,
    usage_attributes,
    walk,
)

logger = logging.getLogger("theygent.control_plane")

# The control-plane forwards a LOGICAL model id, never an engine name. These are
# the managed-engine names. They must never appear as a `/runs` `model`. Kept as
# a local constant on purpose — the IR package is not imported on the plain /runs path.
_ENGINE_NAMES = frozenset({"mlx", "vllm", "llamacpp"})

# Node types that lower ONLY onto the durable runtime (DBOS recv/child-workflows/queue). The
# interactive walker (the `/graphs/runs`, `/agents/{id}/runs`, `/agents/{id}/invoke`, and
# non-durable `fire()` path) cannot run them — a `human` can't durably wait in-process, etc. Caught
# up front with a CLEAR ``durable_required`` error (before a Run), rather than surfacing as the
# walker's raw ``NotImplementedError`` mis-mapped to a 502 inference_error (the confusing
# "not implemented yet" a builder hit when running a human/approval graph interactively).
_DURABLE_ONLY_TYPES = frozenset({"human", "subgraph", "loop", "map"})

# How long graph-run priming waits for the first walker delta (or a pre-stream error) before it
# concludes the first step is a long, delta-less activity and starts the keepalive-wrapped stream.
# Fast pre-stream errors surface in well under a second; this only bounds the header latency when a
# graph's first step is itself a slow generation (an imagine/speak node with no llm ahead of it).
_GRAPH_PRIME_TIMEOUT = 15.0

_RUN_ID_HEADER = "x-theygent-run-id"

# Raw-body document uploads buffer in memory before the background ingest parses them —
# bound the buffer. Generous for real documents (a 500-page PDF is ~10 MiB).
_RAG_MAX_UPLOAD_BYTES = 50 * 1024 * 1024

# The synthetic single node a plain ``/runs`` prompt run is traced as. ``/runs`` has no
# IR/walker (it is the one-shot chat call straight to the gateway), but a prompt run is
# conceptually a one-node run — so the waterfall shows a run-root span + one ``llm`` bar (the
# generation, with ttft), the prompt as input and the answer as output. ``node_id == "llm"``.
_PROMPT_NODE = SimpleNamespace(id="llm", type="llm", kind="activity")


def _prompt_outputs(output: str, reasoning: str) -> dict[str, Any]:
    """The prompt run's captured outputs: the answer, plus the model's thinking under the
    reserved ``reasoning`` entry when there was any — so the thinking stays inspectable per-run
    after the live stream, subject to the same capture gating/caps as every other payload. It is
    never part of ``run.output`` or a stored session turn."""
    outputs: dict[str, Any] = {"output": output}
    if reasoning.strip():
        outputs["reasoning"] = reasoning
    return outputs


class RunRequest(BaseModel):
    input: str
    model: str
    params: dict[str, Any] = {}
    stream: bool = True
    # Opt-in conversational memory: None -> one-shot (nothing persisted to a session);
    # given (existing or new) -> prior turns replayed, this turn appended.
    session_id: str | None = None


class ResumeRunRequest(BaseModel):
    # Deliver the awaited input to a `waiting` run paused at a `human` node. `input` is
    # the human's response (Any — like a graph run input, it may be an object the agent drills).
    # Durable mode only: the resume maps to DBOS.send to the checkpointed workflow.
    input: Any = None


class GraphRunRequest(BaseModel):
    # The IR envelope, inline. Kept as a raw dict so the control-plane owns the 400 shape
    # when it fails IR validation, rather than FastAPI's generic 422. ``input`` binds to the
    # graph's input boundary node; ``session_id`` reuses session memory through the graph path
    # unchanged. Snake_case request fields, matching /runs.
    #
    # ``input`` is ``Any``, not ``str`` (deliberate contract extension — graph path only).
    # A multi-input agent's run input is naturally an OBJECT (e.g. ``{"path": ..., "question":
    # ...}``) that downstream nodes select from with ``$in.in.<field>`` — typing it ``str`` would
    # 422 exactly the agents that use named in-ports. The walker already binds any value
    # to the input boundary node; only this request type blocked it. ``/runs`` (a single prompt =
    # a chat message = a string) is intentionally NOT widened.
    ir: dict[str, Any]
    input: Any = None
    stream: bool = True
    session_id: str | None = None


class CreateAgentRequest(BaseModel):
    # Create an agent + its first version from an IR. The agent's id and the first
    # version's semver come FROM the IR document (the IR carries its identity; the registry
    # persists it under that key — so a stored agent and the Run it produces agree on graph_id /
    # graph_version / contentHash). ``name`` is an optional human label; it falls back to the IR's
    # own name. The IR is a raw dict so the control-plane owns the 400 on a validation failure.
    ir: dict[str, Any]
    name: str | None = None


class AddVersionRequest(BaseModel):
    # Add a new immutable version from an IR. The IR's id must match the URL agent id
    # (the version belongs to that agent); its ``version`` is the new coordinate.
    ir: dict[str, Any]


class AgentRunRequest(BaseModel):
    # Invoke a saved agent by reference. ``input`` binds to the agent's input boundary node
    # (Any, like /graphs/runs — a multi-input agent's input is naturally an object). The version is
    # resolved as: pinned ``content_hash`` (immutable, content-addressed) > pinned ``version`` >
    # latest published (default). ``session_id`` reuses session memory; ``stream`` mirrors the
    # other run surfaces. Ships input-only invocation; typed params are deferred.
    input: Any = None
    version: str | None = None
    content_hash: str | None = None
    session_id: str | None = None
    stream: bool = True


class InvokeRequest(BaseModel):
    # The token-authed, cockpit-free sibling of AgentRunRequest. Same resolution (pinned
    # content_hash > version > latest) and the same shared run path; the only differences are the
    # auth gate and the default — ``stream`` defaults to ``False`` because the caller is a
    # program awaiting a result or a ``run_id`` to poll, not a browser holding an SSE session open.
    input: Any = None
    version: str | None = None
    content_hash: str | None = None
    session_id: str | None = None
    stream: bool = False


class CreateSessionRequest(BaseModel):
    # Client-write ensure of a session. ``id`` omitted → the server mints one (the same idgen
    # runs use); given → idempotent ensure (an existing session updates its metadata iff
    # provided — whole-value replace, the value is opaque and client-owned).
    id: str | None = None
    metadata: dict[str, Any] | None = None


class AppendTurnsRequest(BaseModel):
    # A client-appended user/assistant pair (a direct-to-inference chat persisting its
    # history). Both non-blank: the no-blank-turns posture the run paths hold applies to
    # client writes too. The pair lands atomically with ``run_id`` NULL.
    user_content: str = Field(min_length=1)
    assistant_content: str = Field(min_length=1)


class CreateTriggerRequest(BaseModel):
    # Register a non-interactive entry point for a saved agent. A trigger ALWAYS pins
    # exactly one of ``version`` / ``content_hash`` — so an unattended deploy runs an
    # immutable artifact. ``config`` carries the kind-specific knobs (a cron expression for
    # ``schedule``, a signing ``secret`` for ``webhook``, an optional ``input`` template). No inline
    # IR is ever accepted: a trigger references a saved agent by id, never a pasted graph.
    agent_id: str
    kind: TriggerKind
    version: str | None = None
    content_hash: str | None = None
    config: dict[str, Any] = {}
    enabled: bool = True


class UpdateTriggerRequest(BaseModel):
    # PATCH: enable/disable or edit config only. The pin + kind are immutable (changing them
    # would change *which immutable artifact* runs — a re-pin is a new trigger).
    enabled: bool | None = None
    config: dict[str, Any] | None = None


class IoPolicyRequest(BaseModel):
    # Set an agent's I/O capture policy. ``io_capture`` is the persist decision
    # (off|metadata|full); the effective behavior is the AND with the deployment ceiling + topology
    # default, surfaced in the response so the UI shows the real behavior, not a lie. Keyed to the
    # STABLE agent id — editing it never changes the agent's contentHash (immutability of versions).
    io_capture: CaptureLevel
    io_retention_seconds: int | None = None
    redact_rules: dict[str, Any] | None = None


# ── bench request models ─────────────────────────────────────────────────────────────────────────


class BenchCaseInput(BaseModel):
    # One golden case in a suite. ``input``/``expected`` are the AUTHORED test spec.
    input: Any = None
    expected: Any = None
    assertion: str = "contains"  # exact|contains|regex|json-path-equals|llm-judge
    assertion_config: dict[str, Any] = {}


class CreateSuiteRequest(BaseModel):
    # Save a suite of golden cases pinned to a target. A model suite sets logical_id/binding;
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
    # Record one result captured AT THE BENCH. The server computes ``params_digest`` from
    # ``params`` and ``output_digest`` from ``output`` — and stores the raw ``output`` ONLY when
    # ``capture`` is true (opt-in, local). Metrics + digests are always stored.
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
    # Save a named, modality-scoped, LITERAL param set. Values only — never a run/agent link.
    name: str
    modality: str
    logical_id: str | None = None
    params: dict[str, Any] = {}


# ── connection request models (tool/MCP auth seam) ───────────────────────────────────────────────


class CreateConnectionRequest(BaseModel):
    # Create a tool/MCP auth binding. ``config`` is NON-SECRET only (url/headers/transport/
    # scopes); ``secret`` is the credential material — encrypted into the secret store and NEVER
    # stored in the connection row or returned over the wire. ``kind`` ∈ http_auth | mcp_server.
    name: str
    kind: ConnectionKind
    config: dict[str, Any] = {}
    secret: str | None = None  # write-only; goes to the encrypted secret store, never echoed back
    enabled: bool = True


class UpdateConnectionRequest(BaseModel):
    # PATCH: edit name / non-secret config / enabled, and ROTATE the secret. A rotation
    # keeps the SAME secret_ref (so no agent's contentHash moves). ``secret=""``/None leaves
    # the existing secret untouched; a non-empty ``secret`` rotates it. ``kind`` is immutable.
    name: str | None = None
    config: dict[str, Any] | None = None
    secret: str | None = None  # write-only; non-empty rotates the credential in place
    enabled: bool | None = None


class CreateRagSourceRequest(BaseModel):
    # Create a retrieval source. ``embedding_model`` is a LOGICAL inference-plane id, pinned at
    # creation (vectors from different models share no similarity space — switching models is an
    # explicit re-ingest). ``config`` is the non-secret ingest config; for ``crawl``:
    # root_url (required), max_pages, render_js, all validated below.
    name: str
    kind: RagSourceKind
    embedding_model: str
    config: dict[str, Any] = {}


class UpdateRagSourceRequest(BaseModel):
    # PATCH: rename / edit the ingest config. ``kind`` and ``embedding_model`` are immutable —
    # changing the model would silently orphan every existing vector (create a new source instead).
    name: str | None = None
    config: dict[str, Any] | None = None


class RagQueryRequest(BaseModel):
    # Test/inspect retrieval against one source — the same backend the ``rag`` node calls.
    query: str
    top_k: int = Field(default=5, ge=1, le=50)
    min_similarity: float | None = Field(default=None, ge=-1.0, le=1.0)


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
    # request with GET /runs/{id} (the request-identity payoff, on the error path too).
    if run_id is not None:
        body["runId"] = run_id
    return JSONResponse(status_code=status, content=body)


def _reject_unexecutable_nodes(ir: IRDocument) -> JSONResponse | None:
    """A 400 for node types the IR schema declares but no runtime executes yet (code/rag/
    retriever/memory/agent/condition/iterator). They pass ``validate_graph`` (they are legal IR),
    so without this gate a Run is created and then dies as a misleading 502 ``inference_error``
    ("not implemented yet"). Checked before a Run on every run initiator."""
    unexecutable = sorted({n.id for n in ir.nodes if n.type not in EXECUTABLE_TYPES})
    if not unexecutable:
        return None
    types_ = sorted({n.type for n in ir.nodes if n.type not in EXECUTABLE_TYPES})
    return _error(
        f"node(s) {unexecutable} use type(s) {types_} that are declared in the IR schema but "
        "not executable yet — no runtime implements them",
        status=400,
        code="node_type_unavailable",
    )


def _map_inference_error(exc: APIStatusError) -> tuple[int, str, str]:
    """Map an inference-plane error to a clean control-plane (status, code, message).

    The inference plane returns honest OpenAI-style errors; we relay
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
    """An unattended-surface auth failure. Carries the house error fields so the
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
    """Verify an inbound webhook signature: HMAC-SHA256 of the RAW request body keyed by
    the per-webhook secret, constant-time compared. Accepts ``sha256=<hex>`` (the GitHub-style
    convention real senders emit) or a bare hex digest — shaped to what real callers send, not one
    SDK's idea of it. Any absence/mismatch is a clean ``False`` → 401, never an exception."""
    if not provided:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    candidate = provided.split("=", 1)[1] if provided.startswith("sha256=") else provided
    # Compare as BYTES: str compare_digest raises TypeError on non-ASCII, and this header is
    # attacker-controlled — a crafted byte must yield a clean False → 401, never a 500.
    return hmac.compare_digest(candidate.strip().encode("utf-8"), expected.encode("ascii"))


def _default_cors_origins() -> list[str]:
    # The dev SPAs are served by Vite on their own ports (never bundled into this app), so
    # the browser needs CORS for each dev origin. Single-user localhost only — not a public
    # CORS posture. Override via THEYGENT_CORS_ORIGINS (comma-separated). Two SPAs talk to
    # the control-plane: the cockpit (:5173) and the visual interface (:5174).
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
    # Durable mode re-points the unattended ``fire()`` seam (schedules + webhooks) at the
    # DBOS durable worker and replaces the in-process dispatcher with DBOS dynamic schedules.
    # OFF by default — DBOS is a process-global singleton, so launching it unconditionally would
    # break the many-apps-per-process fast suite; and non-DBOS deployments stay on the
    # in-process path. The interactive streaming surfaces (/runs, /graphs/runs,
    # /agents/{id}/runs, /agents/{id}/invoke) stay on the interactive walker in both modes —
    # only the unattended fire path becomes durable.
    gw = gateway or GatewayClient(inference_base_url)
    store = RunStore()
    mcp_store = McpStore()
    agents = AgentStore()
    triggers = TriggerStore()
    bench = BenchStore()  # saved benchmark results + suites/cases + param presets
    rag_store = RagStore()  # retrieval sources/documents/chunks + hybrid search
    # The tool/MCP auth seam. ``connections`` persists the (non-secret) bindings the IR's
    # ``tools`` block references by id; ``secrets`` encrypts the material behind each ``secret_ref``
    # (resolved server-side, inside the step — never in the IR/span/journal). Keys come from
    # ``secret_key`` (test injection) or ``THEYGENT_SECRET_KEY`` (comma-separated for rotation);
    # when unset, SecretStore makes an ephemeral key with a loud warning (dev-only — see
    # secrets.py).
    connections = ConnectionStore()
    secrets = SecretStore.from_keys(
        secret_key if secret_key is not None else _secret_keys_from_env()
    )
    # The observability seam. One live SpanBus for the /trace/stream side-channel (shared with
    # the in-process durable runtime so its spans stream too), and the opt-in OTLP sink —
    # constructed ONLY when OTEL_EXPORTER_OTLP_ENDPOINT is set (else None — the always-local
    # default). The Telemetry object (the capture wrapper's home) needs the sessionmaker, so it
    # is built in the lifespan once that exists; these two are its inputs.
    span_bus = SpanBus()
    otlp_sink = build_otlp_sink()
    # A single per-deploy bearer token gates the unattended invoke + webhook-management surfaces.
    # NOT RBAC/SSO/multi-user (those are far later). Resolved at construction so a test can inject
    # one and the env drives production. When UNSET, the deploy surfaces are closed (deny-by-
    # default): an unattended entry point with no token is never open.
    token = invoke_token if invoke_token is not None else os.environ.get("THEYGENT_INVOKE_TOKEN")
    # The MCP server registry/connections. App-scoped: created once, connections are lazy
    # (no I/O at construction), torn down at shutdown. `mcp_manager` lets tests inject a
    # manager with a fake client factory.
    mcp = mcp_manager or McpManager()
    # Resolved at startup, not import, so importing the factory has no side effects and a
    # missing DATABASE_URL fails loudly in the lifespan rather than silently.
    db_url = database_url or os.environ.get("DATABASE_URL")

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        if not db_url:
            raise RuntimeError("DATABASE_URL is required (postgresql+asyncpg://…)")
        # Pooled async engine created once, disposed at shutdown. Never per-request.
        engine = db.create_engine(db_url)
        app.state.engine = engine
        app.state.sessionmaker = db.create_sessionmaker(engine)
        # The capture wrapper's process resource — writes span/node_io through this sessionmaker
        # (the normal data layer, never @DBOS.transaction) and publishes live span events to the
        # shared bus. The interactive walker reaches it via WalkContext.run_trace below.
        telemetry = Telemetry(
            sessionmaker=app.state.sessionmaker, span_bus=span_bus, otlp_sink=otlp_sink
        )
        app.state.telemetry = telemetry
        # The DB-backed connection resolver — the server-side auth seam an http tool /
        # connection-backed mcp_tool resolves through, at step time. Built once (needs the
        # sessionmaker); injected into the interactive WalkContext and the durable resources so the
        # secret resolution is identical on both runtimes (never in the IR/span/journal).
        tool_resolver = DbConnectionResolver(app.state.sessionmaker, connections, secrets)
        app.state.tool_resolver = tool_resolver
        # The gate backend (ratelimit counter + quota usage-read). Built once; injected
        # into the interactive WalkContext + durable resources so gates behave identically on both.
        gate_backend = GateBackend(app.state.sessionmaker)
        app.state.gates = gate_backend
        # The artifact store transcribe/speak resolve audio references through (local FS
        # in the user's trust domain; the bytes never journal). Injected into both runtimes.
        artifact_store = LocalArtifactStore()
        app.state.artifacts = artifact_store
        # The retrieval seam. The retriever is the injected backend a ``rag`` node queries
        # (embeds over the gateway with the source's logical model id, then hybrid-searches
        # Postgres); the ingest service runs crawl/parse → chunk → embed as in-process background
        # tasks with progress on the source row.
        rag_retriever = RagRetriever(app.state.sessionmaker, gw, store=rag_store)
        app.state.rag = rag_retriever
        rag_ingest = IngestService(app.state.sessionmaker, gw, store=rag_store)
        app.state.rag_ingest = rag_ingest
        # Sweep runs left non-terminal by a crash to `failed` before serving, so a
        # zombie can never lie about being `streaming` forever. Cheap honest mitigation, not resume.
        async with app.state.sessionmaker() as session, session.begin():
            swept = await store.reconcile_orphaned_runs(session)
        if swept:
            logger.warning("run.reconciled_orphans", extra={"count": swept})
        # Same honesty for retrieval ingests a restart interrupted.
        async with app.state.sessionmaker() as session, session.begin():
            rag_swept = await rag_store.reconcile_orphaned_sources(session)
        if rag_swept:
            logger.warning("rag.reconciled_orphans", extra={"count": rag_swept})
        # Rehydrate persisted MCP registrations into the in-memory manager so they
        # survive a restart. Registration only — connections stay lazy (re-established on next use).
        async with app.state.sessionmaker() as session:
            for name, cfg in await mcp_store.list_servers(session):
                await mcp.register(name, cfg)
        # In durable mode the durable worker launches DBOS embedded (the desktop-sidecar
        # topology) and DBOS dynamic schedules REPLACE the in-process dispatcher — schedules dedupe
        # across instances. On boot we reconcile DBOS schedules to the enabled schedule-triggers
        # (create missing, drop orphans). The fire() seam (webhooks + schedules) enqueues
        # theygent_run on the durable queue.
        app.state.dispatcher = None
        app.state.durable_runtime = None
        if durable:
            from theygent_control_plane.durable import DurableRuntime

            # The durable path turns OFF provider-client retry (DBOS owns retry — avoiding the
            # double-retry hazard), so it uses its own gateway against the same inference base.
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
                telemetry=telemetry,  # durable steps capture through the SAME wrapper
                tool_auth=tool_resolver,  # durable steps resolve connection auth too
                gates=gate_backend,  # durable gate steps use the same backend
                artifacts=artifact_store,  # durable audio steps use the same store
                rag=rag_retriever,  # durable rag steps retrieve through the same backend
                # A distinct executor identity per process role: launch-time recovery then claims
                # only workflows a crashed API process left pending — never ones a healthy
                # standalone worker is mid-step on (and vice versa).
                executor_id="control-plane",
            )
            runtime.launch()
            app.state.durable_runtime = runtime
            async with app.state.sessionmaker() as session:
                enabled = await triggers.list_enabled_schedules(session)
            await runtime.reconcile_schedules(enabled)
        else:
            # The in-process schedule dispatcher over the persisted trigger registry.
            # Schedules "rehydrate" for free — the dispatcher re-reads enabled schedules from
            # Postgres every tick, so a fresh instance picks up every persisted definition with no
            # in-memory restore. The background loop is gated (start_dispatcher) so the fast suite
            # drives `tick` deterministically; the dispatcher object exists either way.
            #
            # Double-fire guard: if the shared Postgres carries live durable trigger schedules
            # (a standalone worker mirrors every enabled schedule trigger into one), running this
            # in-process dispatcher TOO means every schedule fires twice per cron window — once
            # here, once via the worker. Nothing can safely arbitrate that split automatically,
            # so detect it and warn loudly at boot.
            if start_dispatcher:
                durable_schedules = 0
                with contextlib.suppress(Exception):  # no dbos schema = nothing to check
                    async with app.state.sessionmaker() as session:
                        durable_schedules = (
                            await session.execute(
                                sa_text(
                                    "SELECT count(*) FROM dbos.workflow_schedules "
                                    "WHERE schedule_name LIKE 'trigger-%'"
                                )
                            )
                        ).scalar_one()
                if durable_schedules:
                    logger.warning(
                        "dispatcher.durable_schedules_present",
                        extra={
                            "count": int(durable_schedules),
                            "hint": "durable trigger schedules exist in Postgres while the "
                            "in-process dispatcher is starting — if a worker process is "
                            "running, every schedule trigger will DOUBLE-FIRE. Run the API "
                            "with durable mode on (or stop the worker).",
                        },
                    )
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
            # Stop in-flight retrieval ingests first (they write through the sessionmaker).
            await rag_ingest.shutdown()
            if app.state.dispatcher is not None:
                await app.state.dispatcher.stop()  # cancel the in-process schedule loop
            if app.state.durable_runtime is not None:
                app.state.durable_runtime.shutdown()  # DBOS.destroy() — tear down the embedded RT
                await app.state.durable_gateway.aclose()
            await mcp.close_all()  # tear down every live MCP connection
            await gw.aclose()
            if otlp_sink is not None:
                # Flush the batched exporter off the loop — without this the tail of a run's
                # exported spans dies with the process.
                await asyncio.to_thread(otlp_sink.shutdown)
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
    app.state.connections = connections  # tool/MCP auth seam
    app.state.secrets = secrets  # encrypted secret store
    app.state.invoke_token = token

    # Auth placeholder so RBAC slots in later without reshaping handlers — a no-op
    # dependency today; build nothing now. The cockpit/management surfaces stay open on
    # single-user localhost; only the *unattended* surfaces get a real check below.
    async def require_auth() -> None:
        return None

    @app.exception_handler(AuthError)
    async def _on_auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        # Map the auth failure to the house error shape (`{error:{message,code}}`), not FastAPI's
        # default `{detail}` — so an unauthenticated invoke reads like every other 4xx in the API.
        return _error(exc.message, status=exc.status, code=exc.code)

    async def require_token(authorization: str | None = Header(default=None)) -> None:
        # The minimum real auth — a single bearer token gating the unattended invoke +
        # webhook-management surfaces. Constant-time compare; deny-by-default when no token is
        # configured (an open unattended entry point is never the safe default). NOT RBAC/SSO.
        #
        # Attach-point for future workspace/owner scoping without a reshape: this dependency is
        # the ONE place resolving a credential to authority — no handler outside it checks auth. The
        # single global token is the degenerate single-tenant case of a future token→workspace
        # lookup; scoping lands by making THIS function resolve the bearer to a principal (and the
        # `agent`/`trigger` tables gain the deferred `workspace_id` column), so the call sites
        # (`Depends(require_token)`) and the firing path stay shaped as they are. Build only the
        # token now (no speculative principal that nothing consumes — that would erode the seam).
        presented = _bearer_token(authorization)
        # Compare as BYTES: str compare_digest raises TypeError on non-ASCII input, and this
        # header is attacker-controlled — a crafted byte must yield a clean 401, never a 500.
        if (
            not token
            or presented is None
            or not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8"))
        ):
            raise AuthError("a valid bearer token is required (Authorization: Bearer …)")

    async def get_session() -> AsyncIterator[AsyncSession]:
        # Request-scoped session for read handlers. Writes that outlive the handler
        # (the streaming run) use `tx()` off the sessionmaker instead.
        async with app.state.sessionmaker() as session:
            yield session

    @contextlib.asynccontextmanager
    async def tx() -> AsyncIterator[AsyncSession]:
        # One transaction per logical operation: commit on success, roll back on error.
        async with app.state.sessionmaker() as session, session.begin():
            yield session

    def _headers(run: Run) -> dict[str, str]:
        # Request identity, not OTel: forward an opaque run id so the inference call
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
        # Close the run-root span when a graph run terminalizes (mirrors store.set_status). The
        # root anchors the waterfall's t0 + total duration; its status mirrors the run. Best-effort
        # — telemetry never fails the run it observes. Idempotent (first-writer-wins on the root
        # span).
        if ctx.run_trace is not None:
            with contextlib.suppress(Exception):
                await ctx.run_trace.finish(status=status, error=error)

    # ── theygent-native API: /runs ───────────────────────────────────────

    @app.post("/runs", dependencies=[Depends(require_auth)])
    async def create_run(req: RunRequest) -> Any:
        # An engine name is not a logical id. Reject before anything reaches the
        # wire — we never rewrite a logical id into an engine name either.
        if req.model in _ENGINE_NAMES:
            return _error(
                f"{req.model!r} is an engine name, not a logical model id; "
                "the `model` field must be a logical id",
                status=400,
                code="engine_name_not_allowed",
            )

        # Create the run (and ensure its session) in one transaction so it persists
        # immediately — GET /runs/{id} works during streaming and across a restart.
        async with tx() as session:
            if req.session_id:
                await store.ensure_chat_session(session, req.session_id)
            run = await store.create_run(
                session, model=req.model, session_id=req.session_id, params=req.params
            )

        # Naive full replay: prior turns verbatim + the new input. No summarization,
        # no truncation — those are deliberate later additions.
        messages: list[dict[str, Any]] = []
        if req.session_id:
            async with tx() as session:
                messages = list(await store.load_session_messages(session, req.session_id))
        messages.append({"role": "user", "content": req.input})
        logger.info(
            "run.created",
            extra={"run_id": run.id, "model": run.model, "session_id": run.session_id},
        )

        # Trace the prompt run too (a one-node run). No agent → the capture level is the
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
                model=run.model,
                messages=messages,
                params=params,
                extra_headers=_headers(run),
                include_usage=True,  # final chunk carries token usage → the llm span
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
                reasoning_acc = ""  # accumulated thinking → the node's captured `reasoning` entry
                finish_reason: str | None = None
                first_token_ns: int | None = None
                usage: dict[str, int] | None = None
                # Engine variance: thinking may arrive INLINE in `content` as <think> tags
                # (a raw-template server) instead of the separate `reasoning_content` field —
                # one splitter per stream routes it to `event: reasoning` either way.
                splitter = ThinkSplitter()
                # The generation is the run's single ``llm`` node span (under the run-root) —
                # the gap before it is the open_stream/engine latency; the bar is the generation.
                async with run_trace.node_span(_PROMPT_NODE) as scope:
                    async for chunk in upstream:
                        # Usage rides the stream's final chunk, whose choices list is empty (or
                        # all-null through a proxy) — read it BEFORE the choices guard.
                        usage = read_usage(chunk) or usage
                        if not chunk.choices:
                            continue
                        choice = chunk.choices[0]
                        if choice.finish_reason:
                            finish_reason = choice.finish_reason
                        delta = choice.delta
                        if delta is None:
                            continue
                        # A reasoning model streams its thinking before its answer. Forward the
                        # thinking as visible progress so it doesn't look frozen, but NEVER into
                        # `output` — only the answer is the answer. It IS accumulated for the
                        # node's captured outputs, so it stays inspectable after the stream.
                        reasoning = getattr(delta, "reasoning_content", None)
                        if reasoning:
                            reasoning_acc += reasoning
                            yield _sse("reasoning", {"runId": run.id, "reasoning": reasoning})
                        content = getattr(delta, "content", None)
                        if content:
                            answer, thinking = splitter.push(content)
                            if thinking:
                                reasoning_acc += thinking
                                yield _sse("reasoning", {"runId": run.id, "reasoning": thinking})
                            if answer:
                                if first_token_ns is None:
                                    first_token_ns = now_ns()  # ttft = first ANSWER token
                                output += answer
                                yield _sse("delta", {"runId": run.id, "delta": answer})
                    # Stream end: release what the splitter held (a false-partial tag is answer
                    # text; an unclosed think block stays reasoning, leaving the output honestly
                    # blank for `_empty_output_reason`).
                    answer, thinking = splitter.flush()
                    if thinking:
                        reasoning_acc += thinking
                        yield _sse("reasoning", {"runId": run.id, "reasoning": thinking})
                    if answer:
                        if first_token_ns is None:
                            first_token_ns = now_ns()
                        output += answer
                        yield _sse("delta", {"runId": run.id, "delta": answer})
                    scope.set_io(
                        inputs={"prompt": user_input},
                        outputs=_prompt_outputs(output, reasoning_acc),
                    )
                    attrs: dict[str, Any] = {"gen_ai.request.model": run.model}
                    if finish_reason:
                        attrs["gen_ai.response.finish_reason"] = finish_reason
                    if first_token_ns is not None:
                        attrs["ttft_ms"] = round((first_token_ns - scope.span.start_ns) / 1e6, 1)
                    attrs.update(usage_attributes(usage))
                    scope.set_attributes(attrs)
                # Success: mark completed AND append the turn pair in ONE transaction. The output
                # is persisted; an empty answer (model hit its token cap before producing
                # content) is surfaced honestly and contributes no session turn (no blank
                # assistant).
                empty_reason = _empty_output_reason(output, finish_reason)
                async with tx() as s:
                    await store.set_status(
                        s, run.id, "completed", output=output, error=empty_reason
                    )
                    if run.session_id and empty_reason is None:
                        await store.append_turn(
                            s,
                            session_id=run.session_id,
                            run_id=run.id,
                            user_content=user_input,
                            assistant_content=output,
                        )
                await run_trace.finish(status="ok", error=empty_reason)
                terminal = True
                logger.info("run.completed", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "completed"})
                yield "data: [DONE]\n\n"
            except Exception as exc:  # inference died mid-stream: fail cleanly.
                # A failed run contributes no turns — write nothing to the session, so it
                # never holds a dangling user turn with no answer. The run row records it.
                async with tx() as s:
                    await store.set_status(s, run.id, "failed", error=str(exc))
                await run_trace.finish(status="err", error=str(exc))
                terminal = True
                logger.warning("run.failed_midstream", extra={"run_id": run.id})
                yield _sse("run", {"runId": run.id, "status": "failed", "error": str(exc)})
                yield "data: [DONE]\n\n"
            finally:
                # The client disconnected/aborted mid-stream: the generator is cancelled and
                # neither branch above ran, so terminalize the run instead of leaving a
                # `streaming` zombie until the next restart's reconcile sweep. BOTH cleanups ride
                # in ONE detached, shielded task: the cancel scope re-delivers CancelledError at
                # every await here (a BaseException `suppress(Exception)` can't catch), so a
                # second sequential await would be skipped — losing the buffered trace flush and
                # leaking the run's telemetry buffer. The detached task completes regardless;
                # status-guarded so it can't clobber a real outcome.
                if not terminal:

                    async def _cleanup() -> None:
                        await _terminalize_interrupted(run.id)
                        with contextlib.suppress(Exception):
                            await run_trace.finish(
                                status="err", error="interrupted: client disconnected mid-stream"
                            )

                    cleanup = asyncio.create_task(_cleanup())
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(cleanup)

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
            # The single LLM call is the run's ``llm`` node span (the trace's one bar).
            async with run_trace.node_span(_PROMPT_NODE) as scope:
                completion = await gw.complete(
                    model=run.model, messages=messages, params=params, extra_headers=_headers(run)
                )
                choice = completion.choices[0] if completion.choices else None
                raw_content = (choice.message.content if choice else "") or ""
                # A raw-template server may leave the model's thinking inline as <think> tags;
                # only the answer becomes the output. The thinking — inline OR the separate
                # `reasoning_content` message field — is captured into the node's outputs (same
                # posture as the streaming path), never folded into the answer.
                output, thinking = split_think(raw_content)
                field_reasoning = (
                    getattr(choice.message, "reasoning_content", None) or "" if choice else ""
                )
                reasoning = "\n\n".join(p for p in (field_reasoning, thinking) if p.strip())
                finish_reason = choice.finish_reason if choice else None
                scope.set_io(
                    inputs={"prompt": user_input}, outputs=_prompt_outputs(output, reasoning)
                )
                scope.set_attributes(
                    {
                        "gen_ai.request.model": run.model,
                        "gen_ai.response.finish_reason": finish_reason,
                        # A non-stream completion carries usage on the response body itself.
                        **usage_attributes(read_usage(completion)),
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
        # and contributes no session turn — same posture as the streaming path (no blank turns).
        empty_reason = _empty_output_reason(output, finish_reason)
        async with tx() as s:
            await store.set_status(s, run.id, "completed", output=output, error=empty_reason)
            if run.session_id and empty_reason is None:
                await store.append_turn(
                    s,
                    session_id=run.session_id,
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
        # A thin, read-only, paginated list over already-persisted runs (newest first). Additive,
        # not a reshape; /runs/{id} and POST /runs are untouched.
        # NOT an aggregation/summary endpoint.
        runs = await store.list_runs(session, limit=limit, before=before)
        return {"runs": [r.model_dump(mode="json") for r in runs]}

    @app.get("/runs/{run_id}", dependencies=[Depends(require_auth)])
    async def get_run(run_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        run = await store.get_run(session, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        return run.model_dump(mode="json")

    @app.post("/runs/{run_id}/resume", dependencies=[Depends(require_auth)])
    async def resume_run(run_id: str, req: ResumeRunRequest) -> Any:
        # The durable human-in-the-loop entry. Deliver the awaited input to a `waiting` run →
        # DBOS.send to the checkpointed workflow, which resumes from the recv point (even across
        # a worker restart). Authed like the other interactive run surfaces (the person answering
        # a human gate is the cockpit user; starting the durable run uses the same auth) — the
        # unattended surfaces (/invoke, /hooks) keep their token/signature gates.
        # Durable mode only; non-durable runs can't durably wait, so resume is a 400 there (honest).
        runtime = getattr(app.state, "durable_runtime", None)
        if runtime is None:
            return _error(
                "resume requires durable mode (the human node lowers onto DBOS.recv/send)",
                status=400,
                code="durable_required",
            )
        # Imported here, not at module top: the durable package (and its engine SDK) loads only
        # when durable mode is actually live — which it is, since `runtime` exists.
        from theygent_control_plane.durable.runtime import WorkflowGoneError

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
        if not run.awaiting_node:
            # A waiting run always records its awaiting node (mark_waiting writes both). Without
            # it the resume cannot be targeted at the node's delivery topic — a send to nowhere
            # would 202 and never be consumed. Refuse loudly instead.
            return _error(
                f"run {run_id!r} is waiting but records no awaiting node — the resume cannot be "
                "delivered",
                status=409,
                code="awaiting_node_missing",
            )
        # Validate the awaited input against the human node's declared schema. Resolve the
        # run's pinned IR, find the waiting node, and — if it declares an input_schema with
        # required keys — require the input to carry them. A loud 422 beats a silently-wrong
        # resume; richer JSON-Schema validation is a later additive tightening.
        invalid = await _validate_resume_input(run, req.input)
        if invalid is not None:
            return invalid
        # Deliver on the awaiting node's own topic: a duplicate resume (double-clicked Approve, a
        # client retry racing the workflow's status flip) buffers an inert message on the already-
        # consumed node's topic instead of silently satisfying the run's NEXT human gate.
        try:
            await runtime.resume(run_id, req.input, node_id=run.awaiting_node)
        except WorkflowGoneError:
            # The row says waiting but the workflow is gone (durable state repointed/reset, or the
            # workflow was cancelled/dead-lettered): the wait can never be satisfied. Terminalize
            # honestly so the run isn't stuck 'waiting' forever with no API path out.
            async with tx() as s:
                await store.set_status(
                    s, run_id, "failed", error="waiting workflow no longer exists — cannot resume"
                )
            return _error(
                f"run {run_id!r} was waiting but its durable workflow no longer exists; "
                "the run has been marked failed",
                status=410,
                code="workflow_gone",
            )
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

    # ── theygent-native API: the run waterfall (observability) ─────
    # Additive READ surface over the span/node_io the capture wrapper persisted. The in-UI
    # waterfall needs only /trace (bars + gaps, no payloads); /io is the lazy click-through; both
    # pass through the single ``authorize`` chokepoint. /runs, /graphs/runs, /agents/* are
    # untouched. No external trace backend — the UI reads theygent's own store.

    @app.get("/runs/{run_id}/trace", dependencies=[Depends(require_auth)])
    async def get_run_trace(run_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        # The waterfall payload: the run's span tree (timing + status + worker attribution + scalar
        # attrs + edge sizes), ordered by start_ns. NO payloads — those are the lazy /io call.
        run = await store.get_run(session, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        if not authorize(
            LOCAL_PRINCIPAL, "trace:read", run
        ):  # the authz chokepoint (no-op allow now)
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
        # The LIVE waterfall: subscribe to the in-process SpanBus and relay span open/close
        # events as they happen, so the waterfall grows during a streaming run (the in-flight amber
        # bar settles green/red on its close event). The durable record is /trace; this is the
        # ephemeral live view (mirrors the token DeltaBus ↔ persisted-output relationship).
        async with tx() as s:
            run = await store.get_run(s, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        if not authorize(LOCAL_PRINCIPAL, "trace:read", run):
            return _error("not permitted to read this run's trace", status=403, code="forbidden")
        telemetry = app.state.telemetry
        bus = telemetry.bus

        async def gen() -> AsyncIterator[str]:
            # Subscribe INSIDE the generator (so an unstarted response never strands a queue),
            # then re-read the status: the bus's close sentinel only reaches queues subscribed at
            # that moment, so deciding from a pre-subscribe snapshot would hang forever when the
            # run terminalized in the gap. Subscribe-then-check closes the race: a terminal fresh
            # read → done now; a non-terminal one → the close is still ahead of the subscription.
            queue = bus.subscribe(run_id)
            try:
                async with tx() as s:
                    fresh = await store.get_run(s, run_id)
                if fresh is None or fresh.status in ("completed", "failed"):
                    yield _sse(
                        "done", {"runId": run_id, "status": fresh.status if fresh else "unknown"}
                    )
                    return
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except TimeoutError:
                        # Heartbeat: keeps proxies from idling the stream out, and re-checks the
                        # store so a close that somehow never reached this subscriber (e.g. the
                        # run finished on another process — the bus is process-local) still ends
                        # the stream instead of hanging the client forever.
                        async with tx() as s:
                            current = await store.get_run(s, run_id)
                        if current is None or current.status not in (
                            "created",
                            "streaming",
                            "waiting",  # paused at a human gate — spans resume after the input
                        ):
                            yield _sse(
                                "done",
                                {
                                    "runId": run_id,
                                    "status": current.status if current else "unknown",
                                },
                            )
                            return
                        yield ": keepalive\n\n"
                        continue
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
        # The click-through: the full per-node I/O, lazy. Gated states are NOT errors — the
        # timeline stays legible; only payloads are gated, with a clear reason: not-permitted
        # (authz) vs metadata (sizes only) vs off (capture disabled) vs simply not-captured.
        # Never a 500.
        run = await store.get_run(session, run_id)
        if run is None:
            return _error(f"unknown run {run_id!r}", status=404, code="run_not_found")
        telemetry: Telemetry = app.state.telemetry
        io = await telemetry.io_store.get_io(session, run_id, node_id)
        # Authz: io:read denial returns the timing/sizes shape with payloads omitted + a reason —
        # "captured but not visible to you" is a DIFFERENT message from "not captured".
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

    @app.get("/sessions", dependencies=[Depends(require_auth)])
    async def list_sessions(
        limit: int = Query(default=50, ge=1, le=200),
        before: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        # Read-only paginated session list (newest activity first). Same additive shape as
        # GET /runs.
        sessions = await store.list_chat_sessions(session, limit=limit, before=before)
        return {"sessions": [s.model_dump(mode="json") for s in sessions]}

    @app.get("/sessions/{session_id}", dependencies=[Depends(require_auth)])
    async def get_session_detail(
        session_id: str, session: AsyncSession = Depends(get_session)
    ) -> Any:
        detail = await store.get_chat_session(session, session_id)
        if detail is None:
            return _error(f"unknown session {session_id!r}", status=404, code="session_not_found")
        return detail.model_dump(mode="json")

    # Sessions are writable by the client too (a deliberate contract extension): a
    # direct-to-inference chat — which never passes through a run — can persist its history
    # here. The run paths keep writing their own turns exactly as before; these endpoints are
    # a second writer, serialized by the same FOR UPDATE lock in ``append_turn``.

    @app.post("/sessions", status_code=201, dependencies=[Depends(require_auth)])
    async def create_session_endpoint(req: CreateSessionRequest) -> Any:
        # Idempotent ensure: a missing id mints one server-side (the same idgen runs use); an
        # existing id updates metadata iff provided (whole-value replace) and returns the row.
        session_id = req.id or new_ulid()
        async with tx() as session:
            summary = await store.upsert_chat_session(
                session, session_id=session_id, metadata=req.metadata
            )
        return JSONResponse(summary.model_dump(mode="json"), status_code=201)

    @app.post("/sessions/{session_id}/turns", status_code=201, dependencies=[Depends(require_auth)])
    async def append_session_turns(session_id: str, req: AppendTurnsRequest) -> Any:
        # Append a client-written user/assistant pair with run_id NULL — the same FOR-UPDATE +
        # max(position)+1 mechanics as the run paths, so client and run writers interleave
        # without colliding. No implicit create: an unknown session is a 404, not an ensure
        # (POST /sessions is the ensure).
        async with tx() as session:
            if await store.get_chat_session(session, session_id) is None:
                return _error(
                    f"unknown session {session_id!r}", status=404, code="session_not_found"
                )
            appended = await store.append_turn(
                session,
                session_id=session_id,
                run_id=None,
                user_content=req.user_content,
                assistant_content=req.assistant_content,
            )
        if not appended:
            # The session vanished between the (unlocked) existence check and the locked
            # append — a concurrent DELETE won the race and append_turn wrote nothing. Same
            # 404 as if it never existed; never a 201 carrying an empty pair.
            return _error(f"unknown session {session_id!r}", status=404, code="session_not_found")
        return JSONResponse(
            {"messages": [m.model_dump(mode="json") for m in appended]}, status_code=201
        )

    @app.delete("/sessions/{session_id}", dependencies=[Depends(require_auth)])
    async def delete_session_endpoint(session_id: str) -> Any:
        # One transaction: detach the session's runs (run history outlives the conversation it
        # happened in, so run.session_id is nulled — never a run delete), drop its messages,
        # drop the session row.
        async with tx() as session:
            deleted = await store.delete_chat_session(session, session_id)
        if not deleted:
            return _error(f"unknown session {session_id!r}", status=404, code="session_not_found")
        return Response(status_code=204)

    # ── artifacts (the audio-in/audio-out blob surface for the run path) ───────────
    # An audio agent is orchestrated by the control-plane walker (input -> transcribe -> llm ->
    # speak -> output), so its input audio is fetched and its output audio is stored HERE (the
    # artifact store already backs the transcribe/speak nodes). These two routes let a browser
    # participate: POST bytes to get a reference to pass as run input, GET the reference the
    # speak node produced. This is the LOCAL, single-user realization of the artifact store —
    # the cloud-aware signed-URL blob store stays the deferred upgrade (artifacts.py). A GET
    # serves ONLY stored `art_` ids (never an arbitrary path/url a ref may also carry), so it
    # can't be turned into a read-any-file primitive.

    @app.post("/artifacts", dependencies=[Depends(require_auth)])
    async def upload_artifact(request: Request) -> Any:
        # Raw body upload (Content-Type is the artifact's mime) — no multipart, so a browser posts
        # a Blob directly. Returns the reference dict {ref, contentType, bytes} the run input uses.
        data = await request.body()
        if not data:
            return _error("empty artifact body", status=400, code="empty_artifact")
        content_type = request.headers.get("content-type") or "application/octet-stream"
        ref = await app.state.artifacts.put(data, content_type)
        return JSONResponse(ref, status_code=201)

    @app.get("/artifacts/{ref}", dependencies=[Depends(require_auth)])
    async def download_artifact(ref: str) -> Any:
        # Serve a STORED artifact only — an `art_<...>` id the store minted. Reject anything else
        # (a path/url the store's fetch would also resolve) so this is not an arbitrary-file read.
        if not ref.startswith("art_") or "/" in ref or "\\" in ref:
            return _error("not a stored artifact id", status=400, code="invalid_artifact_ref")
        try:
            data, content_type = await app.state.artifacts.fetch(ref)
        except (FileNotFoundError, ValueError):
            return _error(f"unknown artifact {ref!r}", status=404, code="artifact_not_found")
        return Response(content=data, media_type=content_type)

    # ── theygent-native API: /graphs/runs (the IR walker) ───────────
    # The consumer of the IR seam: validate an IRDocument, walk it node by node, and
    # produce a Run exactly as /runs does — but IR-driven. A graph run is still a
    # Run, so GET /runs/{id} is unchanged. /runs itself is untouched.

    @app.post("/graphs/runs", dependencies=[Depends(require_auth)])
    async def create_graph_run(req: GraphRunRequest) -> Any:
        # 1. Validate the IR — Pydantic envelope then semantic graph checks. Either failure is a
        #    400 with a precise message; NO Run is created.
        try:
            ir = parse_document(req.ir)
            validate_graph(ir)
        except (ValidationError, GraphValidationError) as exc:
            return _error(str(exc), status=400, code="invalid_ir")
        # 2. Execute on the shared IR-run path — the SAME path /agents/{id}/runs reuses; the
        #    only difference between the two surfaces is where the IR came from (inline body here,
        #    the registry there). Everything below — engine/tool/MCP up-front checks, the Run row,
        #    SSE relay, session memory — is identical.
        return await _execute_ir_run(
            ir, input_value=req.input, session_id=req.session_id, stream=req.stream
        )

    async def _execute_ir_run(
        ir: IRDocument,
        *,
        input_value: Any,
        session_id: str | None,
        stream: bool,
        trigger_id: str | None = None,
    ) -> Any:
        """Run a *validated* IRDocument through the interactive walker — the one execution path
        /graphs/runs (inline IR), /agents/{id}/runs + /agents/{id}/invoke (saved IR), and the
        trigger ``fire`` seam all use. Assumes the IR is shape/graph-valid; runs the up-front
        engine/tool/MCP membership checks (which depend on live control-plane state, so they run
        per-invocation, not at store time), creates the Run with the graph's recorded identity and
        the firing ``trigger_id`` (None = interactive), and streams/completes. No new execution
        code — only new *initiators*."""

        # Resolve every llm node's model up front and reject an engine-name binding before anything
        # is created or sent — the logical-id invariant on the graph path.
        try:
            llms = llm_models(ir)
            # transcribe/speak/imagine are logical-id-only too — reject an engine-name binding
            # up front, exactly like an llm node.
            audio_model_nodes(ir)
            # A model guardrail resolves a binding at step time too — reject an engine-name
            # binding here like every other model node, not as a mid-run failure.
            for node in ir.nodes:
                if node.type == "guardrail":
                    gcfg = GuardrailConfig.model_validate(node.config)
                    if gcfg.check.model is not None:
                        resolve_model_key(ir, gcfg.check.model.model)
        except EngineNameNotAllowed as exc:
            return _error(str(exc), status=400, code="engine_name_not_allowed")

        # Resolve every tool node's name up front: valid if it is a key in the IR's ``tools``
        # block (an http/builtin binding) OR a directly-registered builtin — otherwise a 400 here,
        # before a Run exists. The connection a binding references is resolved at STEP TIME (a
        # dangling connection binds ``err`` honestly, never a create-time block — its id is an
        # environment binding the importer re-maps), so it is NOT checked here.
        for node, tool_name in tool_nodes(ir):
            if tool_name not in ir.tools and tool_name not in DEFAULT_REGISTRY:
                return _error(
                    f"node {node.id!r}: unknown tool {tool_name!r}",
                    status=400,
                    code="tool_not_found",
                )
            # A `tool` node runs a builtin or an http binding as a step; an mcp binding is the
            # wrong shape here (the `mcp_tool` node runs MCP as a step). Reject it up front with a
            # clear pointer, rather than let the step bind a cryptic err at runtime.
            binding = ir.tools.get(tool_name)
            if getattr(binding, "kind", None) == "mcp":
                return _error(
                    f"node {node.id!r}: tool {tool_name!r} is an MCP binding — run it from an "
                    "`mcp_tool` node (or wire it as a capability into an llm's tools port), not a "
                    "plain `tool` node",
                    status=400,
                    code="tool_binding_mismatch",
                )

        # A builtin tool a model can CALL must self-describe. ``validate_graph`` already
        # requires every llm ``tools`` key to be declared in ``ir.tools``; here we additionally
        # reject a builtin binding with no registry schema — a model needs the description to call
        # it (an http binding's self-description is enforced in validate_graph; mcp describes at
        # runtime).
        for node in ir.nodes:
            if node.type != "llm":
                continue
            for tkey in (node.config or {}).get("tools") or []:
                binding = ir.tools.get(tkey)
                ref = (
                    getattr(binding, "ref", None)
                    if getattr(binding, "kind", "") == "builtin"
                    else None
                )
                if ref is not None and DEFAULT_REGISTRY.schema(ref) is None:
                    return _error(
                        f"node {node.id!r}: builtin tool {tkey!r} is not model-callable: no "
                        "description/parameters schema (a model needs it to decide the call)",
                        status=400,
                        code="tool_not_self_describing",
                    )

        # mcp_tool nodes: the server must be registered (400, no Run). The tool is checked
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

        # rag nodes: the referenced source must exist (400, no Run) — mirroring tool/MCP
        # membership. Its ingest state is NOT checked here: an empty/ingesting source binds an
        # honest ``err`` at step time (state changes between create and run; a stale block here
        # would be a lie).
        rag_source_ids = sorted(
            {str((n.config or {}).get("source") or "") for n in ir.nodes if n.type == "rag"}
        )
        if rag_source_ids:
            async with app.state.sessionmaker() as rag_session:
                for rag_source_id in rag_source_ids:
                    if not await rag_store.source_exists(rag_session, rag_source_id):
                        return _error(
                            f"unknown rag source {rag_source_id!r}",
                            status=400,
                            code="rag_source_not_found",
                        )

        # Node types the IR schema declares but NO runtime executes yet (e.g. code/condition)
        # pass validate_graph — reject them up front with a clear 400 (before a Run), not the
        # walker's raw NotImplementedError mis-mapped to a 502 inference_error.
        unavailable_err = _reject_unexecutable_nodes(ir)
        if unavailable_err is not None:
            return unavailable_err

        # Durable-only node types (human/subgraph/loop/map) cannot run on this interactive walker —
        # reject up front with a clear, actionable error (before a Run), not the walker's raw
        # NotImplementedError mis-mapped to a 502 "not implemented yet" (these run on the
        # durable runtime via a durable run or trigger, not the interactive run path).
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

        # Record the first llm node's resolved logical id as the Run's model so GET /runs/{id}
        # stays meaningful (no llm node -> empty, an inert graph).
        run_model = llms[0][1] if llms else ""
        chash = content_hash(ir)

        # Create + persist the Run with the graph's identity recorded. For a saved-agent
        # invoke this is the agent's coordinate (ir.id == agent id, ir.version == the resolved
        # version, chash == the stored content hash), so the Run row carries it.
        async with tx() as session:
            if session_id:
                await store.ensure_chat_session(session, session_id)
            run = await store.create_run(
                session,
                model=run_model,
                session_id=session_id,
                params=None,
                graph_id=ir.id,
                graph_version=ir.version,
                content_hash=chash,
                trigger_id=trigger_id,
            )

        # Session memory is unchanged through the graph path: prior turns replay into the llm
        # node's messages (the walker prepends ctx.prior_messages).
        prior: list[dict[str, Any]] = []
        if session_id:
            async with tx() as session:
                prior = list(await store.load_session_messages(session, session_id))
        logger.info(
            "graph_run.created",
            extra={
                "run_id": run.id,
                "graph_id": run.graph_id,
                "graph_version": run.graph_version,
                "content_hash": run.content_hash,
                "session_id": run.session_id,
            },
        )

        # Open the run's observability trace. The effective capture level is resolved ONCE per
        # run = deployment ceiling ∧ topology default ∧ the agent's policy (ir.id == the saved
        # agent id for a saved-agent run; None for an inline /graphs/runs → the topology default).
        # The interactive walker is stamped with the ``inproc`` executor (worker attribution).
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
            tool_auth=app.state.tool_resolver,  # server-side connection auth resolution
            gates=app.state.gates,  # ratelimit/quota backend
            artifacts=app.state.artifacts,  # transcribe/speak audio reference store
            rag=app.state.rag,  # retrieval backend for rag nodes/capabilities
        )
        if stream:
            return await _stream_graph(ir, run, input_value, ctx)
        return await _complete_graph(ir, run, input_value, ctx)

    async def _stream_graph(ir: Any, run: Run, user_input: Any, ctx: WalkContext) -> Any:
        # Prime the walker before committing to a 200 SSE response: a pre-stream error (a 503/404
        # from the llm node's open_stream, or a RouterError from a router that runs before any
        # llm) surfaces as a clean status, never a 200-then-broken-stream (mirrors /runs).
        # But priming must not BLOCK the response headers on a long, delta-less FIRST step (an
        # imagine/speak node that yields nothing until it finishes): race it against a short timeout
        # so such a step starts the stream promptly (the keepalive-wrapped relay then covers it and
        # its own failure surfaces as a mid-stream failed frame) instead of stalling headers for the
        # whole generation. Fast pre-stream errors still surface well within the window as clean
        # HTTP statuses (every real error path and test returns in well under a second).
        result = WalkResult()
        walker = walk(ir, user_input, ctx, result)
        first: asyncio.Task[Any] = asyncio.ensure_future(anext(walker))
        carry: asyncio.Task[Any] | None = None
        try:
            primed = [await asyncio.wait_for(asyncio.shield(first), _GRAPH_PRIME_TIMEOUT)]
        except StopAsyncIteration:
            primed = []
        except (
            TimeoutError
        ):  # a slow, delta-less first step is still running — relay it (keepalived)
            primed = []
            carry = first
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
        except TemplateError as exc:  # a bad $in token: clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="template_error", run_id=run.id)
        except TransformError as exc:  # a bad transform expr/ref: clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="transform_error", run_id=run.id)
        except Exception as exc:  # anything else pre-stream (mirrors _complete_graph's catch-all):
            # without this, an unexpected error here — a not-yet-implemented node type executing
            # before the first llm delta, a telemetry/DB hiccup — escaped as a raw 500 and left
            # the Run row a permanent 'created' zombie with its buffered trace leaked.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=502, code="inference_error", run_id=run.id)

        async def gen() -> AsyncIterator[str]:
            terminal = False
            try:
                async with tx() as s:
                    await store.set_status(s, run.id, "streaming")
                yield _sse("run", {"runId": run.id, "status": "streaming", "model": run.model})
                for delta in primed:
                    yield _graph_delta_sse(run.id, delta)
                # Keepalive-wrapped: a delta-less activity (a slow image/audio generation) must not
                # idle the SSE connection out and read as a client disconnect. `carry` is the
                # first step handed over from a timed-out priming.
                async for sse in _relay_walker_sse(walker, run.id, first=carry):
                    yield sse
                # Success: complete the run AND append the turn pair in ONE transaction. The
                # assistant turn is the run's canonical output (the output node's value), which
                # equals the streamed llm text for a trivial graph but may be a tool result.
                # The output is persisted; an honest empty-output reason is surfaced when
                # applicable.
                output = _coerce_output(result.output)
                async with tx() as s:
                    await store.set_status(
                        s, run.id, "completed", output=output, error=result.empty_reason
                    )
                    # No turn for an empty output — never store a blank assistant turn (the
                    # output != "" guard holds even when no empty_reason was derived).
                    if run.session_id and result.empty_reason is None and output != "":
                        await store.append_turn(
                            s,
                            session_id=run.session_id,
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
                # Client disconnected mid-stream: terminalize so the run isn't a `streaming`
                # zombie (mirrors /runs — one detached, shielded task for BOTH cleanups, because
                # the cancel scope re-raises at every await here and would skip a second
                # sequential await, losing the trace flush and leaking the telemetry buffer).
                # The walker is in-process, so a cancel also stops the underlying inference call
                # when the generator is closed.
                if not terminal:

                    async def _cleanup() -> None:
                        await _terminalize_interrupted(run.id)
                        await _finish_trace(
                            ctx, "err", "interrupted: client disconnected mid-stream"
                        )

                    cleanup = asyncio.create_task(_cleanup())
                    with contextlib.suppress(BaseException):
                        await asyncio.shield(cleanup)

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
        except RouterError as exc:  # router named a missing handle / no handle field.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="router_error", run_id=run.id)
        except TemplateError as exc:  # a bad $in token: clean status.
            async with tx() as s:
                await store.set_status(s, run.id, "failed", error=str(exc))
            await _finish_trace(ctx, "err", str(exc))
            return _error(str(exc), status=422, code="template_error", run_id=run.id)
        except TransformError as exc:  # a bad transform expr/ref: clean status.
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
            # Persist the output, with an honest reason when an upstream error left it empty.
            # No turn for an empty output — never store a blank assistant turn (the output != ""
            # guard holds even when no empty_reason was derived).
            await store.set_status(s, run.id, "completed", output=output, error=result.empty_reason)
            if run.session_id and result.empty_reason is None and output != "":
                await store.append_turn(
                    s,
                    session_id=run.session_id,
                    run_id=run.id,
                    user_content=_coerce_output(user_input),
                    assistant_content=output,
                )
        await _finish_trace(ctx, "ok", result.empty_reason)
        logger.info("graph_run.completed", extra={"run_id": run.id})
        return {"runId": run.id, "status": "completed", "output": output}

    # ── theygent-native API: /agents/* (the agent registry) ────────
    # A saved agent has a stable id, immutable versioned content addressed by contentHash, and is
    # invoked BY REFERENCE — not by pasting a graph. Purely additive: /runs and /graphs/runs are
    # untouched; inline IR stays the authoring loop, /agents/* is the saved loop. The registry
    # stores the canonical IR — it invents no agent format — and the hash it stores IS the walker's
    # hash, so a stored agent and the Run it produces agree byte-for-byte. Single-user localhost:
    # no auth/RBAC/sharing built.

    @app.post("/agents", status_code=201, dependencies=[Depends(require_auth)])
    async def create_agent(req: CreateAgentRequest) -> Any:
        # Validate the IR; the agent id + first version come FROM the IR (the IR carries its
        # identity). View is stripped before hashing. A NEW agent only — re-using an existing
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
        # Paginated, newest-first list; each row carries the latest version + count, composed
        # client-side-friendly in one query (no aggregating endpoint).
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
                "must carry the same id as the URL",
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
                # Immutability guard: different content under an existing (id, version).
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
        # The stored IR (+ view) for that version — the canonical document, view-stripped for
        # hashing but returned with its view alongside. The IR is authored/edited as JSON, so this
        # is what the editor reloads.
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
        # The effective + stored capture policy. ``effective`` is what actually happens
        # (ceiling ∧ topology ∧ stored), so the agent-settings control shows the real behavior —
        # e.g. "Full requested; capped to Sizes only by this deployment".
        # Through the authz chokepoint.
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
        # Set the capture policy. Gated on ``agent:configure`` (the authz chokepoint). Editing
        # it does NOT mint a new agent version — the policy lives beside the agent row, not in the
        # hashed IR (version immutability), so ``contentHash`` is unchanged
        # (surfaced in the UI copy).
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
        """Resolve a saved agent's IR by reference: pinned ``content_hash`` > pinned ``version`` >
        latest. A dangling pin (typo'd hash, missing version) or unknown agent is a clean **404**,
        never a 500/hang — an unattended trigger with a stale pin must fail honestly. Returns
        ``(ir, None)`` on success or ``(None, error_response)``; the one resolver shared by
        /agents/{id}/runs, the unattended /agents/{id}/invoke, ``fire`` (triggers), and trigger
        creation."""
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
        """The ONE firing seam — the hard-to-reverse boundary re-pointed at the durable worker in
        durable mode. All three trigger kinds (http via /invoke, schedule via the dispatcher,
        webhook via /hooks) converge here: resolve the trigger's *pinned, saved* agent (never inline
        IR) and run it through the existing walker path (``_execute_ir_run``, non-stream), stamping
        ``trigger_id`` for lineage. No new execution code — only a new initiator. A run whose bound
        inference plane is unreachable ends ``failed`` cleanly inside the walker path (honest
        failure), never a hang. Returns the non-stream run result dict, or an ``_error``
        JSONResponse the caller relays / the dispatcher logs."""
        # In durable mode the fire seam enqueues theygent_run on the DBOS durable queue and
        # awaits its terminal result — the run now survives a crash and resumes. The trigger
        # definition, API, and rows are untouched; only the dispatch mechanism moved. Non-durable
        # mode keeps the in-process walk (a mid-run crash is reconciled to failed, not resumed).
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
            ir, input_value=input_value, session_id=None, stream=False, trigger_id=trigger.id
        )

    @app.post("/agents/{agent_id}/runs", dependencies=[Depends(require_auth)])
    async def run_agent(agent_id: str, req: AgentRunRequest) -> Any:
        # Invoke-by-reference: resolve the agent's IR (pinned contentHash > pinned version >
        # latest), then run it through the existing walker path (_execute_ir_run) — same SSE relay,
        # same Run row (recording the agent's graph_id/graph_version/contentHash), same session
        # memory. The only new thing is *where the IR came from*: the registry, not the body.
        ir, err = await _resolve_agent_ir(
            agent_id, version=req.version, content_hash_pin=req.content_hash
        )
        if err is not None:
            return err
        assert ir is not None
        logger.info("agent.invoked", extra={"agent_id": agent_id})
        return await _execute_ir_run(
            ir, input_value=req.input, session_id=req.session_id, stream=req.stream
        )

    @app.post("/agents/{agent_id}/durable-runs", dependencies=[Depends(require_auth)])
    async def run_agent_durable(agent_id: str, req: AgentRunRequest) -> Any:
        # Run a SAVED agent on the durable runtime and hand back a run id to poll — the way a user
        # executes a durable-only agent (loop/map/subgraph/human) from the UI, since those types
        # can't run on the interactive path. This is ADDITIVE: it touches no interactive handler; it
        # is a second durable-run initiator beside the trigger `fire()` seam, and reuses only the
        # read-only IR resolver + the durable runtime's enqueue. Requires durable mode (else a clean
        # 400, exactly like resume). Fire-and-poll: enqueue, return the run id, and the caller polls
        # GET /runs/{id}.
        runtime = getattr(app.state, "durable_runtime", None)
        if runtime is None:
            return _error(
                "durable runs require the control-plane in durable mode",
                status=400,
                code="durable_required",
            )
        ir, err = await _resolve_agent_ir(
            agent_id, version=req.version, content_hash_pin=req.content_hash
        )
        if err is not None:
            return err
        assert ir is not None
        unavailable_err = _reject_unexecutable_nodes(ir)
        if unavailable_err is not None:
            return unavailable_err
        agent_ref = {
            "agent_id": ir.id,
            "version": req.version,
            # Pin what was just validated: an unpinned request would otherwise re-resolve
            # `latest` at worker pickup, so a version published between enqueue and pickup
            # would run a different IR than the one validated here.
            "content_hash": req.content_hash or (None if req.version else ir.content_hash),
            "enqueued_ns": now_ns(),  # the workflow emits the enqueue→pickup wait span
        }
        handle = await runtime.enqueue_run(agent_ref, req.input, session_id=req.session_id)
        run_id = handle.workflow_id
        logger.info("agent.durable_run", extra={"agent_id": agent_id, "run_id": run_id})
        # The run row is written by the workflow's first step on worker pickup (async), so wait
        # (bounded) for it to exist — else the caller's immediate GET /runs/{id} would 404 and its
        # poll would give up before the run even started.
        for _ in range(40):  # ~2s
            async with tx() as session:
                if await store.get_run(session, run_id) is not None:
                    break
            await asyncio.sleep(0.05)
        return JSONResponse({"run_id": run_id}, status_code=202)

    # ── theygent-native API: /triggers + invoke + hooks (the deploy primitive) ──
    # Triggers fire SAVED, PINNED agents through the one ``fire`` seam over the existing run path
    # — new *initiators*, NOT a new runtime (a mid-run crash is still reconciled to ``failed``).
    # All additive; /runs, /graphs/runs, /agents/{id}/runs untouched.

    def _validate_trigger(
        kind: TriggerKind, version: str | None, content_hash_pin: str | None, config: dict[str, Any]
    ) -> JSONResponse | None:
        # A trigger pins EXACTLY ONE of version / content_hash — an unattended deploy runs an
        # immutable artifact, never "latest" (which could silently change under it).
        if bool(version) == bool(content_hash_pin):
            return _error(
                "a trigger must pin exactly one of `version` or `content_hash` "
                "(an unattended deploy runs an immutable artifact)",
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
        # In durable mode, mirror a schedule trigger's state into a DBOS dynamic schedule.
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
        # a 404 here, not a surprise at fire time (the same honest-pin discipline as invoke).
        _ir, err = await _resolve_agent_ir(
            req.agent_id, version=req.version, content_hash_pin=req.content_hash
        )
        if err is not None:
            return err
        assert _ir is not None
        # Same deploy-time honesty for runnability: an agent with schema-declared-but-unexecutable
        # node types can never fire on ANY runtime — refuse the deploy here rather than have every
        # fire fail (in non-durable mode an up-front-rejected fire leaves no Run row to inspect).
        unavailable_err = _reject_unexecutable_nodes(_ir)
        if unavailable_err is not None:
            return unavailable_err
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
            # lose its secret, a schedule can't get a bad cron (enable/disable + edit config).
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
        await _sync_schedule(updated)  # enable/disable + cron edits re-align the DBOS schedule
        logger.info("trigger.updated", extra={"trigger_id": trigger_id, "enabled": updated.enabled})
        return updated.public_dump()

    @app.delete("/triggers/{trigger_id}", dependencies=[Depends(require_auth)])
    async def delete_trigger(trigger_id: str) -> Response:
        async with tx() as session:
            deleted = await triggers.delete(session, trigger_id)
        if not deleted:
            return _error(f"unknown trigger {trigger_id!r}", status=404, code="trigger_not_found")
        await _drop_schedule(trigger_id)  # drop the DBOS schedule alongside the trigger row
        return Response(status_code=204)

    # ── the connection resource (tool/MCP auth seam) ─────────────────────────────────────────────
    # A connection holds NON-SECRET config + a secret_ref; the secret material is written to the
    # encrypted secret store in the SAME transaction and NEVER returned over the wire
    # (public_dump redacts to hasSecret). Auth resolution is server-side at step time
    # (no per-node apply endpoint).

    def _validate_connection(kind: ConnectionKind, config: dict[str, Any]) -> JSONResponse | None:
        """Reject a connection whose config is malformed or smuggles a secret (secrets go in
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
        # the connection row stores only the resulting secret_ref (never the plaintext).
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
            # Rotate the secret IN PLACE (same secret_ref) so no agent's contentHash moves.
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

    # ── retrieval sources (/rag/*) ────────────────────────────────────────
    # A source is a named document collection an agent's ``rag`` node queries. Ingestion
    # (crawl or upload) runs as an in-process background job; the UI polls the source row for
    # progress — the same polled-progress shape as model downloads. The IR references a source
    # by its stable id (hashed content, like a connection id): re-ingesting never moves a hash.

    def _validate_rag_config(kind: RagSourceKind, config: dict[str, Any]) -> JSONResponse | None:
        if kind == "crawl":
            root_url = str(config.get("root_url") or "").strip()
            if not root_url.startswith(("http://", "https://")):
                return _error(
                    "crawl source config requires a 'root_url' (http/https)",
                    status=400,
                    code="invalid_rag_source",
                )
            max_pages = config.get("max_pages")
            if max_pages is not None and (not isinstance(max_pages, int) or max_pages < 1):
                return _error(
                    "'max_pages' must be a positive integer",
                    status=400,
                    code="invalid_rag_source",
                )
        return None

    @app.post("/rag/sources", status_code=201, dependencies=[Depends(require_auth)])
    async def create_rag_source(req: CreateRagSourceRequest) -> Any:
        if not req.name.strip():
            return _error("source name must be non-empty", status=400, code="invalid_rag_source")
        # The logical-id invariant, same as every model-carrying surface: an engine name is
        # not a model id.
        if req.embedding_model.strip() in _ENGINE_NAMES or not req.embedding_model.strip():
            return _error(
                f"embedding_model must be a logical model id, got {req.embedding_model!r}",
                status=400,
                code="engine_name_not_allowed",
            )
        invalid = _validate_rag_config(req.kind, req.config)
        if invalid is not None:
            return invalid
        async with tx() as session:
            source = await rag_store.create_source(
                session,
                name=req.name.strip(),
                kind=req.kind,
                config=req.config,
                embedding_model=req.embedding_model.strip(),
            )
        logger.info("rag.source_created", extra={"source_id": source.id, "kind": source.kind})
        return JSONResponse(source.public_dump(), status_code=201)

    @app.get("/rag/sources", dependencies=[Depends(require_auth)])
    async def list_rag_sources(
        limit: int = Query(default=100, ge=1, le=200),
        before: str | None = Query(default=None),
        session: AsyncSession = Depends(get_session),
    ) -> Any:
        rows = await rag_store.list_sources(session, limit=limit, before=before)
        return {"sources": [s.public_dump() for s in rows]}

    @app.get("/rag/sources/{source_id}", dependencies=[Depends(require_auth)])
    async def get_rag_source(source_id: str, session: AsyncSession = Depends(get_session)) -> Any:
        source = await rag_store.get_source(session, source_id)
        if source is None:
            return _error(
                f"unknown rag source {source_id!r}", status=404, code="rag_source_not_found"
            )
        return source.public_dump()

    @app.get("/rag/sources/{source_id}/documents", dependencies=[Depends(require_auth)])
    async def list_rag_documents(
        source_id: str, session: AsyncSession = Depends(get_session)
    ) -> Any:
        source = await rag_store.get_source(session, source_id)
        if source is None:
            return _error(
                f"unknown rag source {source_id!r}", status=404, code="rag_source_not_found"
            )
        docs = await rag_store.list_documents(session, source_id)
        return {"documents": [d.public_dump() for d in docs]}

    @app.patch("/rag/sources/{source_id}", dependencies=[Depends(require_auth)])
    async def patch_rag_source(source_id: str, req: UpdateRagSourceRequest) -> Any:
        async with tx() as session:
            existing = await rag_store.get_source(session, source_id)
            if existing is None:
                return _error(
                    f"unknown rag source {source_id!r}", status=404, code="rag_source_not_found"
                )
            if req.config is not None:
                invalid = _validate_rag_config(existing.kind, req.config)
                if invalid is not None:
                    return invalid
            updated = await rag_store.update_source(
                session, source_id, name=req.name, config=req.config
            )
        assert updated is not None
        return updated.public_dump()

    @app.delete("/rag/sources/{source_id}", status_code=204, dependencies=[Depends(require_auth)])
    async def delete_rag_source(source_id: str) -> Response:
        # Stop any in-flight ingest first, then cascade-delete documents + chunks with the row.
        app.state.rag_ingest.cancel(source_id)
        async with tx() as session:
            deleted = await rag_store.delete_source(session, source_id)
        if not deleted:
            return _error(
                f"unknown rag source {source_id!r}", status=404, code="rag_source_not_found"
            )
        logger.info("rag.source_deleted", extra={"source_id": source_id})
        return Response(status_code=204)

    @app.post(
        "/rag/sources/{source_id}:ingest", status_code=202, dependencies=[Depends(require_auth)]
    )
    async def ingest_rag_source(source_id: str) -> Any:
        # Start (or re-run) the crawl. Idempotent economy: unchanged pages are hash-skipped,
        # only new/changed content re-embeds. 202 + poll GET /rag/sources/{id} for progress.
        async with tx() as session:
            source = await rag_store.get_source(session, source_id)
            if source is None:
                return _error(
                    f"unknown rag source {source_id!r}", status=404, code="rag_source_not_found"
                )
            if source.kind != "crawl":
                return _error(
                    "only a crawl source ingests on demand — upload sources ingest per "
                    "uploaded document (POST /rag/sources/{id}/documents)",
                    status=400,
                    code="invalid_rag_source",
                )
            invalid = _validate_rag_config(source.kind, source.config)
            if invalid is not None:
                return invalid
            # Flip the row before the task starts so the 202's immediate follow-up poll
            # already reads ``ingesting`` (the task re-asserts it).
            await rag_store.set_source_state(session, source_id, status="ingesting", error=None)
        try:
            app.state.rag_ingest.start_crawl(source)
        except IngestBusy as exc:
            return _error(str(exc), status=409, code="rag_ingest_busy")
        logger.info("rag.crawl_started", extra={"source_id": source_id})
        return JSONResponse({**source.public_dump(), "status": "ingesting"}, status_code=202)

    @app.post("/rag/sources/{source_id}:cancel", dependencies=[Depends(require_auth)])
    async def cancel_rag_ingest(
        source_id: str, session: AsyncSession = Depends(get_session)
    ) -> Any:
        source = await rag_store.get_source(session, source_id)
        if source is None:
            return _error(
                f"unknown rag source {source_id!r}", status=404, code="rag_source_not_found"
            )
        # Cancelling a settled source is a no-op, mirroring download cancel semantics.
        app.state.rag_ingest.cancel(source_id)
        return source.public_dump()

    @app.post(
        "/rag/sources/{source_id}/documents",
        status_code=202,
        dependencies=[Depends(require_auth)],
    )
    async def upload_rag_document(
        source_id: str, request: Request, filename: str = Query(default="")
    ) -> Any:
        # Raw body upload, like /artifacts (Content-Type is the file's mime; no multipart, so a
        # browser posts the File blob directly). ``filename`` carries the name + extension the
        # parser dispatches on. Parsing/chunking/embedding run in the background; poll the source.
        name = filename.strip()
        if not name:
            return _error(
                "a 'filename' query parameter is required (the extension picks the parser)",
                status=400,
                code="invalid_rag_document",
            )
        if not supported_extension(name):
            return _error(
                f"unsupported document type for {name!r}",
                status=422,
                code="unsupported_document",
            )
        # Cheap gates BEFORE buffering the body: the source must exist, and the declared size
        # must fit the cap (the post-read check below covers a lying/absent Content-Length —
        # raw-body uploads buffer in memory, so unbounded input is a memory hazard).
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > _RAG_MAX_UPLOAD_BYTES:
            return _error(
                f"document exceeds the {_RAG_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB upload limit",
                status=413,
                code="document_too_large",
            )
        async with app.state.sessionmaker() as check_session:
            if not await rag_store.source_exists(check_session, source_id):
                return _error(
                    f"unknown rag source {source_id!r}", status=404, code="rag_source_not_found"
                )
        data = await request.body()
        if not data:
            return _error("empty document body", status=400, code="invalid_rag_document")
        if len(data) > _RAG_MAX_UPLOAD_BYTES:
            return _error(
                f"document exceeds the {_RAG_MAX_UPLOAD_BYTES // (1024 * 1024)} MiB upload limit",
                status=413,
                code="document_too_large",
            )
        async with tx() as session:
            source = await rag_store.get_source(session, source_id)
            if source is None:
                return _error(
                    f"unknown rag source {source_id!r}", status=404, code="rag_source_not_found"
                )
            await rag_store.set_source_state(session, source_id, status="ingesting")
        try:
            app.state.rag_ingest.start_upload(
                source,
                filename=name,
                content_type=request.headers.get("content-type"),
                data=data,
            )
        except IngestBusy as exc:
            return _error(str(exc), status=409, code="rag_ingest_busy")
        logger.info(
            "rag.upload_started",
            extra={"source_id": source_id, "uri": name, "bytes": len(data)},
        )
        return JSONResponse({**source.public_dump(), "status": "ingesting"}, status_code=202)

    @app.post("/rag/sources/{source_id}/query", dependencies=[Depends(require_auth)])
    async def query_rag_source(source_id: str, req: RagQueryRequest) -> Any:
        # The same retrieval the ``rag`` node runs — exposed so a builder can inspect what the
        # model would see before wiring the source into an agent.
        try:
            result = await app.state.rag.retrieve(
                source_id=source_id,
                query=req.query,
                top_k=req.top_k,
                min_similarity=req.min_similarity,
            )
        except RetrievalError as exc:
            status = 404 if "unknown rag source" in str(exc) else 400
            return _error(str(exc), status=status, code="rag_query_failed")
        return result

    @app.post("/agents/{agent_id}/invoke", dependencies=[Depends(require_token)])
    async def invoke_agent(agent_id: str, req: InvokeRequest) -> Any:
        # The HTTP invoke trigger: the token-authed, cockpit-free sibling of /agents/{id}/runs.
        # Same resolution + same shared run path; the ONLY differences are the auth gate
        # (require_token) and the non-stream default. Returns the awaited result (stream:false) or
        # an SSE stream (stream:true), either correlatable via GET /runs/{id}. Missing/bad token →
        # 401 (handled by the dependency before we get here).
        ir, err = await _resolve_agent_ir(
            agent_id, version=req.version, content_hash_pin=req.content_hash
        )
        if err is not None:
            return err
        assert ir is not None
        logger.info("agent.invoked_unattended", extra={"agent_id": agent_id})
        return await _execute_ir_run(
            ir, input_value=req.input, session_id=req.session_id, stream=req.stream
        )

    @app.post("/hooks/{trigger_id}")
    async def fire_hook(trigger_id: str, request: Request) -> Any:
        # The webhook trigger: an inbound URL a THIRD-PARTY system POSTs to. Authed by the
        # per-webhook signature (HMAC-SHA256 of the raw body with config.secret), NOT the bearer
        # token — the caller is an external system, not a credentialed user. The raw payload
        # maps mechanically to the agent's input (payload → input). Bad signature → 401.
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

    # ── management plane: /admin/mcp/* (MCP server registry) ─────────
    # theygent-native management surface (like the inference plane's /admin/*). Servers connect
    # lazily; registration is pure metadata. `env` stays in the user's trust domain — it is
    # passed into the spawned subprocess, never logged with values, never resolved in the cloud.

    def _mcp_view(name: str) -> dict[str, Any]:
        cfg = mcp.config(name)
        assert cfg is not None
        # `env` keys are listed but VALUES are never echoed back (sovereignty: user's trust domain).
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
        try:
            raw = await request.json()
        except Exception:  # malformed JSON body is the caller's error, not a server fault
            return _error("request body is not valid JSON", status=400, code="invalid_json")
        try:
            cfg = McpServerConfig.model_validate(raw)
        except ValidationError as exc:
            return _error(
                f"invalid MCP server config: {exc}", status=422, code="invalid_mcp_config"
            )
        await mcp.register(name, cfg)
        # Persist the registration so it survives a restart (the manager is in-memory).
        async with tx() as s:
            await mcp_store.upsert_server(s, name, cfg)
        return JSONResponse(_mcp_view(name))

    # ── bench store: /bench/* ────────────────────────────────────────
    # The bench adds NO execution path: model tests hit the inference data plane DIRECTLY in the
    # user's trust domain (never proxied here), agent tests reuse the invoke surface. These
    # endpoints only PERSIST what the bench produced — metrics + digests by default, raw capture
    # opt-in and local. Listing follows the keyset pagination shape; the error envelope is shared.

    def _suite_dump(suite: BenchSuite) -> dict[str, Any]:
        return suite.model_dump(mode="json")

    @app.post("/bench/suites", dependencies=[Depends(require_auth)])
    async def create_suite(req: CreateSuiteRequest) -> Any:
        try:
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
        except ValidationError as exc:
            # The request model keeps `assertion` a free string, so an unknown kind surfaces at
            # domain construction — the caller's error (422), never a 500.
            return _error(f"invalid bench suite: {exc}", status=422, code="invalid_bench_suite")
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
        # The server owns the digests: two runs differing only in a param get different
        # ``params_digest`` and are distinct results. Raw ``output`` is persisted ONLY when capture
        # is opted in (and even then as a local reference — never a blob in cloud topology).
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
            # output bytes in this hosted table. The blob store is a separate, deferred concern.
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
        # Two recorded results side by side — the A/B-versions + binding-swap headline. Each
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
        # Drop the persisted registration too, so a delete also survives a restart.
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
        # Honest readiness: not-ready if EITHER dependency is down, so an unreachable inference
        # plane OR Postgres is a distinguishable not-ready — never a green light that 500s on
        # first request.
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
        # MCP is informational only: a registered-but-unconnected server does NOT make the
        # control-plane not-ready — connections are lazy, so "ready" means "can start", not "every
        # external dependency is up". An unspawnable server surfaces as a clean error at the
        # node invocation, never here.
        return JSONResponse({"status": "ready", "mcp": {"servers": mcp.states()}})

    return app


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _graph_delta_sse(run_id: str, delta: Any) -> str:
    """Relay one walker ``Delta`` to SSE, routing a model's *thinking* to ``event: reasoning``, its
    *answer* to ``event: delta``, and a tool invocation to ``event: tool_call`` (visible
    progress: which tool the autonomous loop is calling). Same split the prompt-run path makes, so a
    graph node streams visible progress and the cockpit never looks frozen."""
    kind = getattr(delta, "kind", "content")
    if kind == "reasoning":
        return _sse("reasoning", {"runId": run_id, "reasoning": delta.content})
    if kind == "tool_call":
        return _sse("tool_call", {"runId": run_id, "toolCall": delta.content})
    return _sse("delta", {"runId": run_id, "delta": delta.content})


async def _relay_walker_sse(
    walker: AsyncIterator[Any],
    run_id: str,
    *,
    first: asyncio.Task[Any] | None = None,
    keepalive_interval: float = 15.0,
) -> AsyncIterator[str]:
    """Relay a walker's ``Delta`` stream as SSE, emitting a keepalive comment during any gap
    longer than ``keepalive_interval``.

    A node that produces no deltas until it finishes (a slow image/audio generation, a slow tool)
    leaves the SSE connection silent for the whole step; a proxy or browser reads that idle as a
    disconnect and aborts the run. Racing each next-delta against a keepalive timeout keeps the
    connection alive without touching the walk. The in-flight step is *shielded* from the timeout
    (so a keepalive never cancels it) and cancelled only if the client actually goes away (the
    generator is closed).

    ``first`` is an already-in-flight ``anext(walker)`` task handed over from priming: a graph whose
    FIRST step is a long delta-less activity is started during priming and, rather than stalling the
    response headers, resumed here so the keepalive covers it too."""
    it = walker.__aiter__()
    pending: asyncio.Task[Any] | None = first
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            try:
                delta = await asyncio.wait_for(asyncio.shield(pending), keepalive_interval)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            except StopAsyncIteration:
                pending = None
                return
            pending = None
            yield _graph_delta_sse(run_id, delta)
    finally:
        # Client disconnected: cancel the shielded in-flight step so the underlying inference call
        # stops instead of running on detached.
        if pending is not None and not pending.done():
            pending.cancel()
            with contextlib.suppress(BaseException):
                await pending


def _empty_output_reason(output: str, finish_reason: str | None) -> str | None:
    """An honest reason when a run completed but produced no answer, else ``None``. A blank
    ``content`` with ``finish_reason == 'length'`` means the model hit its token cap before
    answering — classically a reasoning model that spent the whole budget on its hidden
    ``reasoning_content``. Surfacing this stops an empty completion from masquerading as a clean
    success (and tells the user the concrete fix: raise maxTokens)."""
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
    registry. The hash is the walker's ``content_hash`` (the one function, so a stored agent and a
    walked graph never disagree). The stored ``ir`` is the default-filled model dump with ``view``
    removed and ``contentHash`` stamped to the computed value, so the stored document is
    self-describing and re-parses to the identical model + identical hash. The ``view``
    (React-Flow layout) is stored alongside but NEVER hashed — dragging a node must not mint a new
    version."""

    chash = content_hash(ir)
    doc = ir.model_dump(mode="json", by_alias=True, exclude_none=False)
    view = doc.pop("view", None)
    doc["contentHash"] = chash
    return doc, view, chash


def _coerce_output(value: Any) -> str:
    """The run's output as a string for the wire + session storage. A graph's output node may
    bind a non-string value (e.g. ``http_fetch``'s ``{status, body, headers}``); serialize it so
    the ``output`` field and the ``assistant`` turn stay strings. A graph that binds the llm text
    passes through unchanged — backward-compatible."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)
