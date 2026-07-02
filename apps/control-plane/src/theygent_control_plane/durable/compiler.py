"""The §8.7 compiler, in DBOS terms — the **in-workflow lowering** of an IR to a durable run
(M13 §1/§2, decisions D3/D4).

There is exactly ONE registered workflow, ``theygent_run`` — never one per agent (D3). DBOS recovers
crashed workflows by **name-based lookup** against workflows registered before ``DBOS.launch()``, so
a per-agent dynamic workflow would be unrecoverable. ``theygent_run`` resolves the **immutable**,
content-pinned IR (M11/M12) and then **walks it deterministically**, dispatching by ``kind``:

* ``activity`` (``llm``/``tool``/``mcp_tool``) → a ``@DBOS.step`` whose body is the runtime-agnostic
  executor from ``walker.py`` (``execute_llm``/``execute_tool``/``execute_mcp_tool``). The step is
  the checkpoint: on resume a COMPLETED activity replays from the journal and is **not** re-executed
  (the headline — no duplicated side effects; §7).
* ``orchestration`` (``router``) → inline **deterministic** logic in the workflow. No I/O here — the
  determinism guard (§8.1). Replay re-derives the same branch from the same journaled step results.
* ``boundary`` (``input``/``output``) → the workflow's argument / return value.

The deterministic traversal REUSES the exact helpers the M5 walker uses (``topological_order``,
``_is_skipped``, ``_collect_in_ports``, ``_resolve_ref``, edge-liveness, ``finalize_empty_reason``),
so walker/compiler parity is structural, not coincidental (guarded by the parity test). The ``dbos``
import lives only in this package — never in ``walker.py``, a node handler, or the IR (D4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dbos import DBOS, Queue, SetWorkflowID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from theygent_ir import (
    BuiltinTool,
    GuardrailConfig,
    HttpTool,
    HumanConfig,
    IRDocument,
    LlmConfig,
    LoopConfig,
    MapConfig,
    McpTool,
    McpToolConfig,
    Node,
    QuotaConfig,
    RateLimitConfig,
    RouterConfig,
    SpeakConfig,
    SubgraphConfig,
    ToolConfig,
    TranscribeConfig,
    TransformConfig,
    content_hash,
    parse_document,
    topological_order,
)

from theygent_control_plane.mcp import McpManager
from theygent_control_plane.store import AgentStore, RunStore, TriggerStore
from theygent_control_plane.tools import DEFAULT_REGISTRY
from theygent_control_plane.walker import (
    ActivityOutcome,
    EngineNameNotAllowed,
    RouterError,
    TemplateError,
    TransformError,
    _bind_gate,
    _bind_guardrail,
    _bind_outcome,
    _capability_binding,
    _capability_tool_nodes,
    _collect_in_ports,
    _io_input_snapshot,
    _io_output_snapshot,
    _is_blank,
    _is_skipped,
    _node_span_status,
    _parse_if_json,
    _PortInputs,
    _RefError,
    _render_messages,
    _resolve_ref,
    _single_in_value,
    _success_handles,
    _to_openai_tool_choice,
    assistant_tool_calls_message,
    build_capability_schemas,
    build_http_call,
    build_tool_schemas,
    evaluate_guardrail_rule,
    execute_guardrail_model,
    execute_http_tool,
    execute_llm,
    execute_mcp_connection_tool,
    execute_mcp_tool,
    execute_quota,
    execute_ratelimit,
    execute_speak,
    execute_tool,
    execute_transcribe,
    execute_transform,
    finalize_empty_reason,
    guardrail_model_passed,
    is_http_tool,
    llm_models,
    mcp_config_from_connection,
    merge_usage,
    resolve_gate_key,
    resolve_model,
    resolve_model_key,
    tool_result_message,
    usage_attributes,
)
from theygent_control_plane.walker import ToolCall as _ToolCall

from theygent_control_plane.durable.bus import DeltaBus  # isort: skip

logger = logging.getLogger("theygent.control_plane.durable")

# M14 §1.4: the durable queue map fan-out enqueues per-element child workflows on. Separate from the
# top-level RUN_QUEUE so a wide fan-out never starves top-level fires. Created at import so it is
# registered before ``DBOS.launch()`` (same discipline as RUN_QUEUE). Concurrency is bounded per-map
# at the application layer (a semaphore around enqueue+await in ``_map_fanout``) rather than at the
# queue, because a DBOS queue's concurrency is fixed at construction while ``map.concurrency`` is
# per-node config; the thin-M14 bound is "how many branches in flight at once".
MAP_QUEUE = Queue("theygent_map")

# M14 §1.1: the topic prefix the ``human`` node recvs on and ``POST /runs/{id}/resume`` sends to.
# The topic is per-NODE (``human:<node_id>``): DBOS buffers sends per topic FIFO, so with one shared
# topic a duplicate resume (double-clicked Approve, client retry) would buffer a second message that
# silently satisfied the run's NEXT human gate with a stale payload. Per-node topics make a stray
# duplicate inert — it sits on the already-consumed node's topic forever.
HUMAN_TOPIC = "human"


def human_topic(node_id: str | None) -> str:
    """The delivery topic for one ``human`` node's awaited input (``human:<node_id>``). A missing
    node id (defensive — an old row without ``awaiting_node``) falls back to the bare prefix."""
    return f"{HUMAN_TOPIC}:{node_id}" if node_id else HUMAN_TOPIC


# A pragmatic "wait forever" for an un-timed human wait (DBOS.recv has no None=infinite; it takes a
# float seconds). 100 years — the run survives restarts while paused, the whole point of the durable
# wait; a real ``timeout`` in config bounds it precisely.
_FOREVER_SECONDS = 100 * 365 * 24 * 3600


class SubgraphDepthError(RuntimeError):
    """A ``subgraph``/``loop``/``map`` body would expand past its ``maxDepth`` (m14.md §1.2). The
    depth bound prevents unbounded / mutually-recursive composition; exceeding it fails the run
    honestly rather than recursing forever."""


class LoopError(RuntimeError):
    """A ``loop`` could not complete (m14.md §1.3): a body iteration failed, or its ``condition``
    referenced a field absent from the iteration output. Fails the run honestly, with a clear
    reason."""


class MapError(RuntimeError):
    """A ``map`` could not complete (m14.md §1.4): its input is not a list, or — under the
    ``fail_fast`` policy — an element failed. Fails the run honestly with a clear reason."""


class HumanTimeout(RuntimeError):
    """A ``human`` wait exceeded its ``timeout`` and the node's ``on_timeout`` policy is ``fail``
    (m14.md §1.1). Fails the run honestly (M9) rather than hanging or fabricating an input."""


# Gateway client is imported lazily for typing only via the resources holder below.


@dataclass
class DurableResources:
    """The process-local resources the durable steps reach (M13 D2/D6). DBOS workflows/steps are
    module-level functions, so they cannot take an app instance as an argument; instead the runtime
    sets this singleton at launch and the steps read it. Only serializable *data* flows through the
    workflow→step boundary (DBOS checkpoints it); these are the non-serializable *resources*
    (clients, the session factory) that live for the process lifetime."""

    gateway: Any  # GatewayClient (kept Any to avoid importing the SDK wrapper at module load)
    mcp: McpManager
    store: RunStore
    agents: AgentStore
    triggers: TriggerStore
    sessionmaker: async_sessionmaker[AsyncSession]
    bus: DeltaBus
    # M17: the capture wrapper resource (same object the interactive walker uses, so spans/node_io
    # land identically under both runtimes — the §1.5 one-wrapper rule). May be None in a degraded
    # setup; the durable walk guards on it (telemetry never fails the run it observes).
    telemetry: Any = None  # observability.Telemetry
    # M19 §1.1: the connection resolver an http tool / connection-backed mcp_tool resolves auth
    # through, server-side INSIDE the step. The SAME resolver the interactive walker uses (secret
    # resolution is identical on both runtimes; never in the IR/span/journal). May be None.
    tool_auth: Any = None  # walker.ConnectionResolver
    # M19 §2.8: the gate backend (ratelimit counter + quota usage-read) — the SAME one the
    # interactive walker uses, so gates behave identically on both runtimes. May be None.
    gates: Any = None  # gates.GateBackend
    # M19 §2.2: the artifact store transcribe/speak resolve audio references through. May be None.
    artifacts: Any = None  # artifacts.LocalArtifactStore


_RES: DurableResources | None = None


def set_resources(resources: DurableResources) -> None:
    """Install the process-local resources the steps use. Called once by the runtime at launch."""
    global _RES
    _RES = resources


def _res() -> DurableResources:
    if _RES is None:  # pragma: no cover - a launch bug, never a normal path
        raise RuntimeError("durable resources not set — DurableRuntime.launch() was not called")
    return _RES


def _coerce_output(value: Any) -> str:
    """The run's output as a string for the wire + persistence (mirrors app.py ``_coerce_output``):
    ``None`` → ``""``, a string passes through, anything else is JSON."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value)


@contextlib.asynccontextmanager
async def _null_acm() -> AsyncIterator[None]:
    """A no-op async CM yielding ``None`` — used when no :class:`RunTrace` is wired (M17)."""
    yield None


def _durable_node_span(run_trace: Any, node: Node) -> Any:
    """The node-span CM for the durable walk (M17 §4) — the SAME wrapper the interactive walker
    uses,
    so a durable run's waterfall is identical in shape. A no-op when telemetry is unwired."""
    if run_trace is not None:
        return run_trace.node_span(node)
    return _null_acm()


# ── activity steps (the §8.7 durable activities — runtime-agnostic bodies wrapped) ──
# Retries are ON with sensible defaults (m13-dbos.md §2): a transient inference/DB failure retries
# via DBOS — which is why the durable gateway turns provider-client retry OFF (DBOS owns
# retry, no double-retry). This REPLACES the M12 gateway-level retry the durable path removed;
# without it a single transient 503 fails the whole run with no retry at any layer. Tuning the
# counts/intervals per-activity is the deferred §5 work; the win on top is RESUME-after-crash.
#
# **The guarantee is honest (decision D9):** a COMPLETED (journaled) step replays exactly once and
# is never re-executed. An INTERRUPTED step (process died mid-execution) or a RETRIED step is
# **at-least-once** — its body runs again. So an activity with a non-idempotent external side effect
# can repeat it (the §8 Do-NOT "don't make activities non-idempotent" is the contract, not the
# enforcement). llm/tool/mcp reads are safe to repeat; a side-effecting tool must be idempotent (the
# human node inherits this). retries_allowed=True is sound here only because these bodies are
# read-shaped; a future write-shaped activity must opt into idempotency keys, not inherit this.
_RETRY = {"retries_allowed": True, "max_attempts": 3, "interval_seconds": 1.0, "backoff_rate": 2.0}


@DBOS.step(**_RETRY)
async def _llm_step(
    run_id: str,
    node_id: str,
    model_id: str,
    params: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> dict[str, Any]:
    """ONE ``llm`` model turn as a durable step. Streams tokens to the in-process bus as a side
    effect (§6 — non-durable), returns the assembled answer + any ``tool_calls`` (the journaled
    values). On replay this body does NOT run, so tokens are not regenerated/re-streamed and the
    tool calls are re-derived from the journal, not re-requested (D7; M21 — each turn is one step so
    the tool loop's progress is durable)."""
    res = _res()
    bus = res.bus

    def on_delta(content: str, kind: str) -> None:
        bus.publish(run_id, node_id, content, kind)

    out = await execute_llm(
        res.gateway,
        model_id=model_id,
        params=params,
        messages=messages,
        extra_headers={"x-theygent-run-id": run_id},
        on_delta=on_delta,
        tools=tools,
        tool_choice=tool_choice,
    )
    return {
        "output": out.output,
        "finish_reason": out.finish_reason,
        "truncated_empty": out.truncated_empty,
        "tool_calls": [
            {"id": c.id, "name": c.name, "arguments": c.arguments} for c in out.tool_calls
        ],
        # Token usage journals WITH the turn: a replayed step re-reports the same usage instead of
        # re-metering (and a resumed run never loses a completed turn's accounting). None when the
        # upstream reported nothing; readers use .get() so pre-usage journal entries replay fine.
        "usage": out.usage,
    }


@DBOS.step(**_RETRY)
async def _tool_step(
    run_id: str, node_id: str, tool_name: str, args: dict[str, Any]
) -> dict[str, Any]:
    # The built-in tool registry is process-wide and stateless (m6.md §3) — the durable step uses
    # the same DEFAULT_REGISTRY the interactive walker does.
    out = await execute_tool(DEFAULT_REGISTRY, tool_name, args)
    return {"ok": out.ok, "value": out.value}


@DBOS.step(**_RETRY)
async def _mcp_step(
    run_id: str, node_id: str, server: str, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    out = await execute_mcp_tool(_res().mcp, server, tool, args)
    return {"ok": out.ok, "value": out.value}


@DBOS.step(**_RETRY)
async def _mcp_conn_step(
    run_id: str, node_id: str, connection_id: str, tool: str, args: dict[str, Any]
) -> dict[str, Any]:
    """A CONNECTION-BACKED mcp_tool as a durable step (M19 §2.4): resolve the connection (auth)
    HERE, inside the step (server-side — §1.1), build the transport config, then call. The secret
    never journals; a completed step replays from the journal."""
    res = _res()
    try:
        conn = await res.tool_auth(connection_id) if res.tool_auth is not None else None
    except Exception as exc:  # e.g. an undecryptable secret — an honest err, never a run failure
        return {"ok": False, "value": f"mcp connection {connection_id!r}: {exc}"}
    if conn is None:
        return {"ok": False, "value": f"mcp connection {connection_id!r} not found or disabled"}
    cfg = mcp_config_from_connection(conn)
    out = await execute_mcp_connection_tool(res.mcp, connection_id, cfg, tool, args)
    return {"ok": out.ok, "value": out.value}


@DBOS.step(**_RETRY)
async def _http_step(
    run_id: str,
    node_id: str,
    connection_id: str,
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any,
    response_map: str | None,
    idempotency_key: str | None,
    timeout_s: float | None,
) -> dict[str, Any]:
    """The http-tool activity as a durable step (M19 §2.3). The connection is resolved + the secret
    decrypted HERE, inside the step (server-side — §1.1), then the call runs. The request is built
    deterministically in the workflow body and passed in, so step args stay serializable +
    replay-stable. A completed step replays from the journal (no duplicated POST); the
    ``idempotency_key`` covers the crash-after-send window (§2.5)."""
    resolver = _res().tool_auth
    try:
        conn = await resolver(connection_id) if resolver is not None else None
    except Exception as exc:  # e.g. an undecryptable secret — an honest err, never a run failure
        return {"ok": False, "value": f"http tool connection {connection_id!r}: {exc}"}
    out = await execute_http_tool(
        conn,
        method=method,
        url=url,
        headers=headers,
        body=body,
        response_map=response_map,
        idempotency_key=idempotency_key,
        timeout_s=timeout_s,
    )
    return {"ok": out.ok, "value": out.value}


async def _durable_tool_call(
    ir: IRDocument, call: _ToolCall, run_id: str, node_id: str, iteration: int
) -> ActivityOutcome:
    """Dispatch ONE model-emitted tool call to its EXISTING durable step (M21), so each call is
    independently journaled — a crash mid-loop resumes at the first incomplete step (D9), completed
    calls replay from the journal. Mirrors the interactive ``execute_tool_call`` but via @DBOS.step
    bodies. Plain helper (not itself a step): it composes steps inside the workflow body."""
    # M22: an ir.tools key (M21) OR a tool/mcp_tool NODE id wired as a capability (binding from the
    # node's inline config — D3/D6). Same dispatch below either way; legacy keys tried first.
    binding = ir.tools.get(call.name) or _capability_binding(ir, call.name)
    if binding is None:
        if call.name in DEFAULT_REGISTRY:  # a directly-registered builtin
            o = await _tool_step(run_id, node_id, call.name, call.arguments)
            return ActivityOutcome(ok=o["ok"], value=o["value"])
        return ActivityOutcome(ok=False, value=f"unknown tool {call.name!r}")
    if isinstance(binding, BuiltinTool):
        o = await _tool_step(run_id, node_id, binding.ref, call.arguments)
        return ActivityOutcome(ok=o["ok"], value=o["value"])
    if isinstance(binding, McpTool):
        if binding.server:
            o = await _mcp_step(run_id, node_id, binding.server, binding.tool, call.arguments)
        else:
            o = await _mcp_conn_step(
                run_id, node_id, binding.connection or "", binding.tool, call.arguments
            )
        return ActivityOutcome(ok=o["ok"], value=o["value"])
    if isinstance(binding, HttpTool):
        props = frozenset((binding.parameter_schema or {}).get("properties", {}).keys())
        ports = _PortInputs(
            values=call.arguments, declared=props | frozenset(call.arguments.keys())
        )
        idem = binding.idempotency_key
        if idem:  # D9 at-least-once: a write tool called twice in one loop mustn't collide
            idem = f"{idem}-{node_id}-{iteration}-{call.id}"
        cfg = ToolConfig(
            tool=call.name,
            method=binding.method,
            url_template=binding.url_template,
            body_template=binding.body_template,
            headers=binding.headers,
            response_map=binding.response_map,
            idempotency_key=idem,
            timeout_seconds=getattr(binding, "timeout_seconds", None),
        )
        try:
            hc = build_http_call(binding, cfg, ports, node_id=node_id, run_id=run_id)
        except Exception as exc:  # template error → structured err (m6 §4)
            return ActivityOutcome(ok=False, value=f"http tool {call.name!r}: {exc}")
        o = await _http_step(
            run_id,
            node_id,
            hc.connection_id,
            hc.method,
            hc.url,
            hc.headers,
            hc.body,
            hc.response_map,
            hc.idempotency_key,
            hc.timeout,
        )
        return ActivityOutcome(ok=o["ok"], value=o["value"])
    return ActivityOutcome(ok=False, value=f"tool {call.name!r}: unsupported binding")


@DBOS.step(**_RETRY)
async def _tool_schemas_step(
    run_id: str,
    node_id: str,
    ir_dict: dict[str, Any],
    tool_keys: list[str],
    cap_node_ids: list[str],
) -> list[dict[str, Any]]:
    """Build the OpenAI tool-schema union for one llm node as a JOURNALED step. Schema building is
    real I/O for MCP-backed tools (``list_tools`` lazily (re)connects the server), so it must not
    run bare in the workflow body: the body re-executes on crash-recovery, and a transient MCP
    failure there would silently drop the schemas, flip the tool loop off, and make the resumed
    walk ignore journaled turns that DID carry tool calls. Journaling the union keeps replay
    stable — the recovering process replays the same schemas the original run negotiated."""
    res = _res()
    ir = parse_document(ir_dict)
    schemas: list[dict[str, Any]] = []
    if tool_keys:
        schemas += await build_tool_schemas(
            ir, tool_keys, registry=DEFAULT_REGISTRY, mcp=res.mcp, tool_auth=res.tool_auth
        )
    if cap_node_ids:
        by_id = {n.id: n for n in ir.nodes}
        nodes = [by_id[i] for i in cap_node_ids if i in by_id]
        schemas += await build_capability_schemas(
            nodes, registry=DEFAULT_REGISTRY, mcp=res.mcp, tool_auth=res.tool_auth
        )
    return schemas


@DBOS.step(**_RETRY)
async def _guardrail_model_step(
    run_id: str,
    node_id: str,
    model_id: str,
    params: dict[str, Any],
    prompt: str,
    input_value: Any,
) -> dict[str, Any]:
    """A MODEL guardrail's classifier call as a durable step — the cheap judge call before the
    expensive node. Journals ``{"answer", "usage"}``: the caller decides pass/block from the
    answer and lands the usage on the call's generate span (the judge call spends real tokens,
    so the quota gate must see them). Entries journaled before usage was carried replay as a
    bare answer string — the caller reads both shapes, so an old run resumes fine."""
    out = await execute_guardrail_model(
        _res().gateway,
        model_id=model_id,
        params=params,
        prompt=prompt,
        input_value=input_value,
        extra_headers={"x-theygent-run-id": run_id},
    )
    return {"answer": out.output, "usage": out.usage}


@DBOS.step(**_RETRY)
async def _ratelimit_step(scope: str, limit: int, window_seconds: int) -> bool:
    """The ``ratelimit`` gate as a durable step (M19 §2.8) — the counter hit is I/O. Returns
    True=allow. (A repeated step on resume re-counts a hit; a gate is a soft policy, not a financial
    side effect, so at-least-once is acceptable here — the §1.6 lean-seam posture.)"""
    return await execute_ratelimit(
        _res().gates, scope=scope, limit=limit, window_seconds=window_seconds
    )


@DBOS.step(**_RETRY)
async def _quota_step(agent_id: str | None, budget_tokens: int, window_seconds: int) -> bool:
    """The ``quota`` gate as a durable step (M19 §2.8/§1.6) — reads accumulated span token usage.
    Returns True=allow."""
    return await execute_quota(
        _res().gates, agent_id=agent_id, budget_tokens=budget_tokens, window_seconds=window_seconds
    )


@DBOS.step(**_RETRY)
async def _transcribe_step(
    run_id: str, node_id: str, model_id: str, params: dict[str, Any], audio_ref: Any
) -> dict[str, Any]:
    """The ``transcribe`` activity as a durable step (M19 §2.2): audio-ref → text. The audio bytes
    go to the inference base URL, never a control-plane route (§10). Returns the ok/err outcome."""
    res = _res()
    out = await execute_transcribe(
        res.gateway,
        res.artifacts,
        model_id=model_id,
        params=params,
        audio_ref=audio_ref,
        extra_headers={"x-theygent-run-id": run_id},
    )
    return {"ok": out.ok, "value": out.value}


@DBOS.step(**_RETRY)
async def _speak_step(
    run_id: str, node_id: str, model_id: str, params: dict[str, Any], text: str
) -> dict[str, Any]:
    """The ``speak`` activity as a durable step (M19 §2.2): text → audio REFERENCE (the bytes are an
    artifact, not journaled — so a resumed run replays the ref, not the audio)."""
    res = _res()
    out = await execute_speak(
        res.gateway,
        res.artifacts,
        model_id=model_id,
        params=params,
        text=text,
        extra_headers={"x-theygent-run-id": run_id},
    )
    return {"ok": out.ok, "value": out.value}


# ── app-DB steps (idempotent — at-least-once safe, D6) ──────────────────────────────


@DBOS.step(**_RETRY)
async def _resolve_ir_step(agent_ref: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve the trigger's *pinned, saved* agent to its canonical IR (M11/M12 §1.1): pinned
    ``content_hash`` > pinned ``version`` > latest. Returns the stored canonical IR dict, or
    ``None`` for a dangling pin / unknown agent (the workflow fails the run honestly). Never inline
    IR — a trigger always references a saved agent by reference (D3)."""
    res = _res()
    agent_id = agent_ref["agent_id"]
    version = agent_ref.get("version")
    chash = agent_ref.get("content_hash")
    async with res.sessionmaker() as session:
        if chash:
            sv = await res.agents.get_version_by_hash(session, agent_id, chash)
        elif version:
            sv = await res.agents.get_version(session, agent_id, version)
        else:
            sv = await res.agents.latest_version(session, agent_id)
    return dict(sv.ir) if sv is not None else None


@DBOS.step(**_RETRY)
async def _create_run_step(
    run_id: str,
    model: str,
    graph_id: str | None,
    graph_version: str | None,
    chash: str | None,
    trigger_id: str | None,
) -> None:
    """Idempotently create the Run row with id == the DBOS workflow id (D6), so a resumed run reuses
    the same row and ``GET /runs/{id}`` correlates across a crash. ON CONFLICT DO NOTHING makes a
    step re-execution a no-op."""
    res = _res()
    async with res.sessionmaker() as session, session.begin():
        await res.store.ensure_run(
            session,
            run_id=run_id,
            model=model,
            graph_id=graph_id,
            graph_version=graph_version,
            content_hash=chash,
            trigger_id=trigger_id,
        )


@DBOS.step(**_RETRY)
async def _complete_run_step(
    run_id: str, status: str, output: str | None, error: str | None
) -> None:
    """Terminalize the Run (idempotent: setting the same terminal values twice is a no-op). The
    durable ``fire()`` path is un-threaded (thread_id None), so there is no thread turn to append —
    completion is just the status/output/error write (M9 §2.2 persists the output)."""
    res = _res()
    async with res.sessionmaker() as session, session.begin():
        await res.store.set_status(session, run_id, status, output=output, error=error)  # type: ignore[arg-type]


async def _fail_run(run_id: str, run_trace: Any, reason: str) -> dict[str, Any]:
    """Terminalize a failed run so the row can never be left non-terminal. The journaled step is
    the normal path (idempotent on replay); if the step call itself raises — e.g. a recovered
    workflow whose failure point now precedes its journaled operations, so the step intercept
    collides with a differently-named recorded operation — fall back to a DIRECT store write.
    An unjournaled duplicate write is harmless (set_status is idempotent); a permanently
    non-terminal durable run (excluded from the reconcile sweep, un-resumable) is not."""
    try:
        await _complete_run_step(run_id, "failed", None, reason)
    except Exception:
        logger.warning("durable.terminalize_fallback", extra={"run_id": run_id, "error": reason})
        try:
            res = _res()
            async with res.sessionmaker() as session, session.begin():
                await res.store.set_status(session, run_id, "failed", error=reason)
        except Exception:  # pragma: no cover - even the direct write failed; nothing left to try
            logger.exception("durable.terminalize_failed", extra={"run_id": run_id})
    await _finish_run_trace(run_trace, "err", reason)
    return {"runId": run_id, "status": "failed", "error": reason}


@DBOS.step(**_RETRY)
async def _mark_waiting_step(run_id: str, node_id: str) -> None:
    """Pause the Run at a ``human`` node (M14 §1.1): status → ``waiting`` + record the node, so the
    run is excluded from M9's sweep while paused and ``POST /runs/{id}/resume`` can find the node.
    Idempotent (the recovered workflow re-marks the same wait — a no-op write before the recv
    replays its buffered value)."""
    res = _res()
    async with res.sessionmaker() as session, session.begin():
        await res.store.mark_waiting(session, run_id, node_id)


@DBOS.step(**_RETRY)
async def _set_running_step(run_id: str) -> None:
    """Flip a resumed ``human`` run back to a running status (``streaming``) once its input
    arrives — so the run no longer reads as ``waiting`` after recv returns (and ``awaiting_node``
    clears)."""
    res = _res()
    async with res.sessionmaker() as session, session.begin():
        await res.store.set_status(session, run_id, "streaming")


# ── the deterministic durable walk (orchestration is inline; activities are steps) ──


def _node_input(ports: _PortInputs, node: Node) -> Any:
    """The value to feed a ``subgraph``/``loop``/``map`` body run (M14 §1.2 data mapping via M10
    named ports). One in-port → that port's value verbatim (a single-input child gets the raw
    value); several in-ports → the ``{port: value}`` object (the child's input boundary receives it
    and drills with ``$in.in.<port>``); no in-port → ``None``. The M10 mapping at the composition
    seam."""
    declared = node.ports.in_
    if not declared:
        return None
    if len(declared) == 1:
        return ports.values.get(declared[0].id)
    return {p.id: ports.values.get(p.id) for p in declared}


def _child_ref(config: Any, depth: int) -> dict[str, Any]:
    """Build the pinned child ``agent_ref`` for a composed body, carrying the nesting ``depth`` (M14
    §1.2). Depth rides INSIDE the opaque ``agent_ref`` dict so the frozen ``theygent_run`` signature
    is untouched (m14.md §0 / §4 the Do-NOT) — ``_resolve_ir_step`` ignores it, the depth guard
    reads it. ``config`` is a Subgraph/Loop/MapConfig — all share ``agent``/``version``/
    ``content_hash``."""
    return {
        "agent_id": config.agent,
        "version": config.version,
        "content_hash": config.content_hash,
        "depth": depth,
    }


def _eval_loop_condition(condition: str, value: Any, node_id: str) -> bool:
    """Evaluate a ``loop`` stop-condition over the iteration output (M14 §1.3). Reuses the router's
    ``$in`` resolver (m14.md §1.3 "reuse the router expression evaluator") against a one-port map
    binding the output to ``in`` — so ``$in.in.<field>`` drills into it exactly as everywhere else.
    Truthy → stop the loop. A field absent from a present output is a loud :class:`LoopError` (the
    M9/M10 no-silent-nonsense rule), never a silent ``False``."""
    ports = _PortInputs(values={"in": value}, declared=frozenset({"in"}))
    try:
        resolved = _resolve_ref(condition, ports, node_id)
    except _RefError as exc:
        raise LoopError(
            f"loop {node_id!r}: condition {condition!r} could not resolve over the iteration "
            f"output: {exc}"
        ) from exc
    return bool(resolved)


async def _map_fanout(
    run_id: str,
    node: Node,
    child_ref: dict[str, Any],
    elements: list[Any],
    concurrency: int | None,
    run_trace: Any = None,
) -> list[dict[str, Any]]:
    """Fan out one ``theygent_run`` child per element on the durable queue and await all, preserving
    element order (M14 §1.4). Each element's child has a DETERMINISTIC workflow id
    (``<run>-map-<node>-<i>``), so on resume a completed branch dedups (replays from the journal)
    and
    only the incomplete branches re-run — the headline durable-fan-out property. ``concurrency`` (if
    set) bounds how many branches the parent has in flight at once via a semaphore around
    enqueue+await; ``None`` enqueues all at once. Child failures come back as ``status='failed'``
    result dicts (``theygent_run`` never raises), so the join is total and the policy is decided by
    the caller."""
    sem = asyncio.Semaphore(concurrency) if concurrency and concurrency > 0 else None

    async def _one(index: int, element: Any) -> dict[str, Any]:
        cwid = f"{run_id}-map-{node.id}-{index}"
        _log_branch(run_id, node.id, "map", index)

        async def _go() -> dict[str, Any]:
            # One span per fan-out branch (named `<node>#<i>`), so the parent waterfall shows
            # every element instead of one opaque map bar. Deterministic id → replay-idempotent;
            # the branch's full trace lives under its child run.
            branch_cm = run_trace.branch_span(node, index) if run_trace is not None else _null_acm()
            async with branch_cm:
                with SetWorkflowID(cwid):
                    handle = await MAP_QUEUE.enqueue_async(
                        theygent_run, dict(child_ref), element, None, None
                    )
                return await handle.get_result()

        if sem is None:
            return await _go()
        async with sem:
            return await _go()

    return list(await asyncio.gather(*[_one(i, e) for i, e in enumerate(elements)]))


async def _durable_walk(
    ir: IRDocument,
    input_value: Any,
    run_id: str,
    depth: int = 0,
    run_trace: Any = None,
    ir_dict: dict[str, Any] | None = None,
) -> tuple[Any, bool, str | None]:
    """Walk a validated IR deterministically, awaiting an activity step per ``activity`` node and
    running ``orchestration``/``boundary`` inline (M13 §2). This mirrors ``walker.walk`` exactly —
    same traversal order, same edge-liveness/skip logic, same value threading — but the I/O lives in
    journaled steps so the run resumes from the last completed activity. Returns
    ``(output, output_produced, empty_reason)``. The thread-memory replay is empty on the durable
    ``fire()`` path (un-threaded); prior messages are threaded in by the caller if ever needed.

    M14 adds the four additive-lowering types (m14.md §1), each a new branch here, classified by its
    existing ``kind`` — NOT a new subsystem: ``human`` (boundary) → ``DBOS.recv`` durable wait;
    ``subgraph`` (boundary) → a ``theygent_run`` child workflow; ``loop`` (orchestration) → bounded
    inline repetition over child workflows, deterministic control; ``map`` (orchestration) → durable
    fan-out/join over the queue. ``depth`` is the composition nesting level (0 at the top), guarded
    against ``maxDepth`` before any child is spawned.

    M17: ``run_trace`` (a :class:`~observability.RunTrace`) wraps each node in the SAME span wrapper
    the interactive walker uses (§1.5), so a durable run's waterfall is identical in shape — and
    each
    node span is stamped with the DBOS worker that ran it (worker attribution), so a crash-resumed
    run visibly hops workers (first-writer-wins on the deterministic span id keeps the pre-crash
    rows). A no-op when telemetry is unwired."""

    values: dict[tuple[str, str], Any] = {}
    skipped: set[str] = set()
    live_handles: dict[str, set[str]] = {}
    truncated_empty_nodes: list[str] = []
    output: Any = None
    output_produced = False

    # M22 (parity with the interactive walker): a capability tool node (source of a `tool` edge) is
    # not run as a step — the model calls it lazily inside the llm loop. Skip it in the topo walk.
    capability_nodes = {e.source for e in ir.edges if e.channel == "tool"}

    for node in topological_order(ir):
        if node.id in capability_nodes:
            continue
        if _is_skipped(node, ir.edges, skipped, live_handles):
            skipped.add(node.id)
            _log_node(run_id, node, skipped=True)
            if run_trace is not None:
                await run_trace.skipped(node)
            continue
        _log_node(run_id, node, skipped=False)

        io_inputs = _io_input_snapshot(node, ir.edges, values, skipped, live_handles)
        async with _durable_node_span(run_trace, node) as scope:
            if node.kind == "boundary":
                if node.type == "input":
                    live_handles[node.id] = _success_handles(node)
                    for handle in live_handles[node.id]:
                        values[(node.id, handle)] = input_value
                elif node.type == "output":
                    live_handles[node.id] = set()
                    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                    if output_produced:
                        # Two output nodes both executed: the run output would silently be
                        # whichever topo order visits last. Loud, like the interactive walker.
                        raise TemplateError(
                            f"node {node.id!r}: a second output node executed — the run output "
                            "would be ambiguous. Route exclusive branches (router/guardrail) so "
                            "at most one output node is live per run."
                        )
                    output = _single_in_value(ports, node)
                    output_produced = True
                elif node.type == "human":
                    # M14 §1.1: a durable wait. Persist `waiting`, then `DBOS.recv` — the workflow
                    # is
                    # checkpointed here and survives a worker crash. `POST /runs/{id}/resume` → send
                    # delivers the input and the workflow resumes from the checkpoint. The awaited
                    # input binds the node's success handle(s) (the response flows downstream).
                    config = HumanConfig.model_validate(node.config)
                    await _mark_waiting_step(run_id, node.id)
                    timeout = config.timeout if config.timeout is not None else _FOREVER_SECONDS
                    message = await DBOS.recv_async(human_topic(node.id), timeout_seconds=timeout)
                    await _set_running_step(run_id)
                    if message is None:  # timed out (recv → None) — honest fail or declared default
                        if config.on_timeout == "fail":
                            raise HumanTimeout(
                                f"human node {node.id!r}: no input within {config.timeout}s "
                                "(on_timeout=fail)"
                            )
                        received = config.default
                    else:
                        # The resume payload is {"input": …}; tolerate a bare payload (be liberal).
                        received = message.get("input") if isinstance(message, dict) else message
                    live_handles[node.id] = _success_handles(node)
                    for handle in live_handles[node.id]:
                        values[(node.id, handle)] = received
                elif node.type == "subgraph":
                    # M14 §1.2: an agent calls a SAVED, PINNED agent as an independently durable
                    # child
                    # workflow. The pin is frozen into the parent IR; the depth guard prevents
                    # unbounded recursion. The parent's in-port value (M10 mapping) is the child's
                    # run
                    # input; the child's output binds the node's ok handle (a failed child binds
                    # err).
                    config = SubgraphConfig.model_validate(node.config)
                    if depth + 1 > config.max_depth:
                        raise SubgraphDepthError(
                            f"subgraph {node.id!r}: maxDepth {config.max_depth} exceeded at depth "
                            f"{depth + 1} (unbounded/recursive composition)"
                        )
                    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                    child_input = _node_input(ports, node)
                    child_wid = f"{run_id}-sg-{node.id}"
                    with SetWorkflowID(child_wid):
                        handle = await DBOS.start_workflow_async(
                            theygent_run, _child_ref(config, depth + 1), child_input, None, None
                        )
                    child = await handle.get_result()
                    if child.get("status") == "failed":
                        outcome = ActivityOutcome(
                            ok=False,
                            value=f"subgraph {config.agent!r} failed: {child.get('error')}",
                        )
                    else:
                        outcome = ActivityOutcome(ok=True, value=child.get("output"))
                    _bind_outcome(node, outcome, values, live_handles)
                else:  # no other boundary types exist (NODE_TYPE_KIND pins them) — guard anyway.
                    raise NotImplementedError(
                        f"boundary node {node.id!r} (type {node.type!r}) is not implemented yet"
                    )

            elif node.kind == "activity":
                ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                if node.type == "llm":
                    config = LlmConfig.model_validate(node.config)
                    model_id, params = resolve_model(ir, config)
                    messages = _render_messages(node, config, ports)
                    # M21 — the same bounded tool loop the interactive walker runs, but each model
                    # turn is a journaled `_llm_step` and each tool call its existing journaled step
                    # (`_durable_tool_call`), so a crash mid-loop resumes at the first incomplete
                    # step
                    # (D9). With no tools this is exactly one `_llm_step` — byte-identical to
                    # pre-M21.
                    # M22 (parity with the walker): union the legacy ir.tools keys (config.tools)
                    # with the capability nodes wired to this llm's `tools` port. Capability schemas
                    # name the function by NODE id; both dispatch through _durable_tool_call. Built
                    # inside a journaled step — MCP schema lookup is I/O and must replay stably.
                    cap_nodes = _capability_tool_nodes(ir, node.id)
                    schemas: list[dict[str, Any]] = []
                    if config.tools or cap_nodes:
                        doc = ir_dict or ir.model_dump(mode="json", by_alias=True)
                        schemas = await _tool_schemas_step(
                            run_id,
                            node.id,
                            doc,
                            list(config.tools or []),
                            [n.id for n in cap_nodes],
                        )
                    has_tools = bool(schemas)
                    tool_schemas = schemas if has_tools else None
                    tool_choice = _to_openai_tool_choice(config.tool_choice) if has_tools else None
                    # M17 §2: each journaled turn is a `model.generate` phase span (child of the
                    # node
                    # span). Carries the GenAI-semconv model/finish scalars.
                    gen_cm = (
                        scope.child_phase("model.generate") if scope is not None else _null_acm()
                    )
                    final_output = ""
                    final_finish: str | None = None
                    usage_acc: dict[str, int] | None = None
                    truncated = False
                    capped = False
                    async with gen_cm as gen_scope:
                        iteration = 0
                        while True:
                            res = await _llm_step(
                                run_id,
                                node.id,
                                model_id,
                                params,
                                messages,
                                tool_schemas,
                                tool_choice,
                            )
                            final_finish = res.get("finish_reason")
                            # Node total across tool-loop turns (.get: pre-usage journal entries
                            # replay without the key).
                            usage_acc = merge_usage(usage_acc, res.get("usage"))
                            calls = [
                                _ToolCall(id=c["id"], name=c["name"], arguments=c["arguments"])
                                for c in res.get("tool_calls", [])
                            ]
                            if not (calls and has_tools):
                                final_output = res["output"]
                                truncated = res["truncated_empty"]
                                break
                            if iteration >= config.max_tool_iterations:
                                capped = True
                                final_output = res["output"]
                                break
                            messages.append(assistant_tool_calls_message(calls))
                            for ci, call in enumerate(calls):
                                # Each tool call is a child span under the llm node span — the
                                # same waterfall shape as the interactive walker (one wrapper,
                                # both runtimes). The phase id carries iteration + call index so
                                # repeat calls to one tool get distinct rows; deterministic per
                                # walk, so replay idempotency holds.
                                tool_cm = (
                                    scope.child_phase(
                                        f"tool.{call.name}#{iteration}.{ci}",
                                        name=f"tool.{call.name}",
                                    )
                                    if scope is not None
                                    else _null_acm()
                                )
                                async with tool_cm as tool_scope:
                                    outcome = await _durable_tool_call(
                                        ir, call, run_id, node.id, iteration
                                    )
                                    if tool_scope is not None:
                                        tool_scope.set_attributes(
                                            {"tool.name": call.name, "tool.ok": outcome.ok}
                                        )
                                messages.append(tool_result_message(call, outcome))
                            # A FORCED tool_choice ("required" / a named function) applies to the
                            # FIRST turn only — re-sending it would force a tool call on every
                            # turn, so the model could never produce a final answer and a write
                            # tool would re-execute until the iteration cap.
                            if tool_choice is not None and tool_choice != "auto":
                                tool_choice = "auto"
                            iteration += 1
                        # Usage lands on the generate span only (never mirrored onto the node
                        # span) — the quota gate sums across a run's spans; a mirror would
                        # double-count.
                        if gen_scope is not None:
                            attrs: dict[str, Any] = {"gen_ai.request.model": model_id}
                            if final_finish:
                                attrs["gen_ai.response.finish_reason"] = final_finish
                            attrs.update(usage_attributes(usage_acc))
                            gen_scope.set_attributes(attrs)
                    if truncated or (capped and _is_blank(final_output)):
                        truncated_empty_nodes.append(node.id)
                    if scope is not None:
                        scope.set_attributes({"gen_ai.request.model": model_id})
                    live_handles[node.id] = _success_handles(node)
                    for handle in live_handles[node.id]:
                        values[(node.id, handle)] = final_output
                elif node.type == "tool":
                    config = ToolConfig.model_validate(node.config)
                    # M19 §2.3: route http-vs-builtin. An ``http`` binding → a journaled http step
                    # (connection auth resolved inside the step); else the M6 builtin step. Both go
                    # through the ok/err contract.
                    http_binding = is_http_tool(ir, config.tool)
                    if isinstance(http_binding, HttpTool):
                        try:
                            call = build_http_call(
                                http_binding, config, ports, node_id=node.id, run_id=run_id
                            )
                        except Exception as exc:  # template error → structured err (m6 §4)
                            outcome = ActivityOutcome(
                                ok=False, value=f"http tool {config.tool!r}: {exc}"
                            )
                        else:
                            step_out = await _http_step(
                                run_id,
                                node.id,
                                call.connection_id,
                                call.method,
                                call.url,
                                call.headers,
                                call.body,
                                call.response_map,
                                call.idempotency_key,
                                call.timeout,
                            )
                            outcome = ActivityOutcome(ok=step_out["ok"], value=step_out["value"])
                    elif isinstance(ir.tools.get(config.tool), McpTool):
                        # An mcp binding is the wrong shape for a step-mode tool node (the mcp_tool
                        # node runs MCP as a step). The interactive path rejects this up front; the
                        # durable-runs path skips those checks, so bind a clear err here for parity
                        # of message quality (never a cryptic tool-not-found key).
                        outcome = ActivityOutcome(
                            ok=False,
                            value=f"tool {config.tool!r} is an MCP binding — use an mcp_tool node",
                        )
                    else:
                        # A ``builtin`` binding names its callable via ``ref``; a bare name is a
                        # directly-registered builtin (parity with the interactive walker).
                        ir_binding = ir.tools.get(config.tool)
                        builtin_name = (
                            ir_binding.ref if isinstance(ir_binding, BuiltinTool) else config.tool
                        )
                        try:
                            args = {
                                k: _resolve_ref(v, ports, node.id) for k, v in config.args.items()
                            }
                        except Exception as exc:  # an unresolvable arg ref is a structured err (§4)
                            outcome = ActivityOutcome(ok=False, value=str(exc))
                        else:
                            step_out = await _tool_step(run_id, node.id, builtin_name, args)
                            outcome = ActivityOutcome(ok=step_out["ok"], value=step_out["value"])
                    _bind_outcome(node, outcome, values, live_handles)
                elif node.type == "mcp_tool":
                    config = McpToolConfig.model_validate(node.config)
                    tool = config.tool
                    target = config.connection or config.server
                    try:
                        args = {k: _resolve_ref(v, ports, node.id) for k, v in config.args.items()}
                    except Exception as exc:
                        outcome = ActivityOutcome(
                            ok=False, value=f"mcp {target!r} tool {tool!r} failed: {exc}"
                        )
                    else:
                        # M19 §2.4: connection-backed → resolve auth in the step; else M7 server.
                        if config.connection:
                            step_out = await _mcp_conn_step(
                                run_id, node.id, config.connection, tool, args
                            )
                        else:
                            step_out = await _mcp_step(
                                run_id, node.id, config.server or "", tool, args
                            )
                        outcome = ActivityOutcome(ok=step_out["ok"], value=step_out["value"])
                    _bind_outcome(node, outcome, values, live_handles)
                elif node.type == "guardrail":  # MODEL guardrail (activity; rule⇒inline below)
                    config = GuardrailConfig.model_validate(node.config)
                    mcfg = config.check.model
                    assert mcfg is not None  # validate_graph guarantees this for a model check
                    model_id, params = resolve_model_key(ir, mcfg.model)
                    # A branch-local name: rebinding `input_value` here would corrupt the RUN
                    # input the `input` boundary reads when a guardrail sorts before it.
                    gr_input = _single_in_value(ports, node)
                    # The classifier call runs inside a `model.generate` phase span, like an llm
                    # turn. Its usage lands on THAT span only (never mirrored onto the node span)
                    # — the quota gate SUMS across a run's spans; a mirror would double-count.
                    gen_cm = (
                        scope.child_phase("model.generate") if scope is not None else _null_acm()
                    )
                    async with gen_cm as gen_scope:
                        step_out = await _guardrail_model_step(
                            run_id, node.id, model_id, params, mcfg.prompt, gr_input
                        )
                        # A journal entry written before usage was carried replays as the bare
                        # answer string.
                        if isinstance(step_out, str):
                            answer, gr_usage = step_out, None
                        else:
                            answer, gr_usage = step_out.get("answer", ""), step_out.get("usage")
                        if gen_scope is not None:
                            gen_scope.set_attributes(
                                {"gen_ai.request.model": model_id, **usage_attributes(gr_usage)}
                            )
                    _bind_guardrail(
                        node,
                        guardrail_model_passed(answer, mcfg.pass_on),
                        gr_input,
                        config.on_block,
                        values,
                        live_handles,
                    )
                elif node.type in ("ratelimit", "quota"):  # M19 §2.8: the gate seam
                    gate_input = _single_in_value(ports, node)
                    if node.type == "ratelimit":
                        rcfg = RateLimitConfig.model_validate(node.config)
                        key = resolve_gate_key(rcfg.key_expr, ports, node.id)
                        allowed = await _ratelimit_step(
                            f"{ir.id}:{node.id}:{key}", rcfg.limit, rcfg.window_seconds
                        )
                        reason = f"rate limit exceeded ({rcfg.limit}/{rcfg.window_seconds}s)"
                    else:
                        qcfg = QuotaConfig.model_validate(node.config)
                        allowed = await _quota_step(ir.id, qcfg.budget_tokens, qcfg.window_seconds)
                        reason = (
                            f"token budget exceeded ({qcfg.budget_tokens}/{qcfg.window_seconds}s)"
                        )
                    _bind_gate(node, allowed, gate_input, reason, values, live_handles)
                elif node.type == "transcribe":  # M19 §2.2 audio-ref → text
                    tcfg = TranscribeConfig.model_validate(node.config)
                    model_id, binding_params = resolve_model_key(ir, tcfg.model)
                    audio_ref = _single_in_value(ports, node)
                    step_out = await _transcribe_step(
                        run_id, node.id, model_id, {**binding_params, **tcfg.params}, audio_ref
                    )
                    _bind_outcome(
                        node,
                        ActivityOutcome(ok=step_out["ok"], value=step_out["value"]),
                        values,
                        live_handles,
                    )
                elif node.type == "speak":  # M19 §2.2 text → audio reference
                    scfg = SpeakConfig.model_validate(node.config)
                    model_id, binding_params = resolve_model_key(ir, scfg.model)
                    text_val = _single_in_value(ports, node)
                    step_out = await _speak_step(
                        run_id,
                        node.id,
                        model_id,
                        {**binding_params, **scfg.params},
                        text_val if isinstance(text_val, str) else json.dumps(text_val),
                    )
                    _bind_outcome(
                        node,
                        ActivityOutcome(ok=step_out["ok"], value=step_out["value"]),
                        values,
                        live_handles,
                    )
                else:  # agent / rag / retriever / memory / code — deferred (§7)
                    raise NotImplementedError(
                        f"activity node {node.id!r} (type {node.type!r}) is not implemented yet"
                    )

            elif node.kind == "orchestration":
                if node.type == "router":
                    # Inline, deterministic — NO I/O (determinism guard §8.1). Mirrors _walk_router.
                    config = RouterConfig.model_validate(node.config)
                    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                    try:
                        selected = _resolve_ref(config.select, ports, node.id)
                    except _RefError as exc:
                        raise RouterError(f"router {node.id!r}: {exc}") from exc
                    out_ids = {p.id for p in node.ports.out}
                    if not isinstance(selected, str) or selected not in out_ids:
                        raise RouterError(
                            f"router {node.id!r}: select {config.select!r} resolved to "
                            f"{selected!r}, not one of its out-handles {sorted(out_ids)}"
                        )
                    live_handles[node.id] = {selected}
                    values[(node.id, selected)] = _single_in_value(ports, node)
                elif node.type == "transform":
                    # M19 §2.9: deterministic JSON-template reshape, inline (no I/O). Same body as
                    # the interactive walker (execute_transform), so durable parity holds.
                    config = TransformConfig.model_validate(node.config)
                    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                    value = execute_transform(config.expr, ports, node.id)
                    live_handles[node.id] = _success_handles(node)
                    for handle_id in live_handles[node.id]:
                        values[(node.id, handle_id)] = value
                elif node.type == "guardrail":  # RULE guardrail (orchestration; model⇒step above)
                    # Deterministic predicate over journaled input — NO I/O (the §1.2 determinism
                    # guard; a model guardrail is the activity branch above). Mirrors
                    # _walk_guardrail_rule.
                    config = GuardrailConfig.model_validate(node.config)
                    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                    # A branch-local name: rebinding `input_value` here would corrupt the RUN
                    # input the `input` boundary reads when a guardrail sorts before it (an
                    # optional, unfed in-port gives the guardrail zero indegree).
                    gr_input = _single_in_value(ports, node)
                    assert config.check.rule is not None  # validate_graph guarantees this
                    passed = evaluate_guardrail_rule(config.check.rule, gr_input)
                    _bind_guardrail(node, passed, gr_input, config.on_block, values, live_handles)
                elif node.type == "loop":
                    # M14 §1.3: bounded, deterministic repetition. Each iteration runs the pinned
                    # body
                    # agent as a child workflow (deterministic id → a completed iteration replays
                    # from
                    # the journal on resume, never re-runs); the previous output feeds the next
                    # input.
                    # The control (counter, condition over journaled output) does NO I/O — the §8.1
                    # determinism guard. maxIterations caps it; an optional condition stops early.
                    config = LoopConfig.model_validate(node.config)
                    if depth + 1 > config.max_depth:
                        raise SubgraphDepthError(
                            f"loop {node.id!r}: maxDepth {config.max_depth} exceeded at depth "
                            f"{depth + 1}"
                        )
                    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                    current = _node_input(ports, node)
                    for i in range(config.max_iterations):
                        child_wid = f"{run_id}-loop-{node.id}-{i}"
                        _log_branch(run_id, node.id, "loop", i)
                        # One span per iteration (named `<node>#<i>`), so the parent waterfall
                        # shows each pass instead of one opaque loop bar. Deterministic id →
                        # replay-idempotent; the iteration's full trace lives under the child run.
                        branch_cm = (
                            run_trace.branch_span(node, i) if run_trace is not None else _null_acm()
                        )
                        async with branch_cm:
                            with SetWorkflowID(child_wid):
                                handle = await DBOS.start_workflow_async(
                                    theygent_run,
                                    _child_ref(config, depth + 1),
                                    current,
                                    None,
                                    None,
                                )
                            child = await handle.get_result()
                        if child.get("status") == "failed":
                            raise LoopError(
                                f"loop {node.id!r}: iteration {i} failed: {child.get('error')}"
                            )
                        current = child.get("output")
                        if config.condition and _eval_loop_condition(
                            config.condition, current, node.id
                        ):
                            break
                    live_handles[node.id] = _success_handles(node)
                    for handle_id in live_handles[node.id]:
                        values[(node.id, handle_id)] = current
                elif node.type == "map":
                    # M14 §1.4: durable fan-out/join. One child workflow per element on the durable
                    # queue; a crash mid-fan-out resumes only the incomplete branches (deterministic
                    # per-element ids). Partial-failure policy is config: fail_fast vs collect.
                    config = MapConfig.model_validate(node.config)
                    if depth + 1 > config.max_depth:
                        raise SubgraphDepthError(
                            f"map {node.id!r}: maxDepth {config.max_depth} exceeded at depth "
                            f"{depth + 1}"
                        )
                    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                    collection = _node_input(ports, node)
                    if not isinstance(collection, list):
                        parsed = _parse_if_json(collection)
                        if not isinstance(parsed, list):
                            raise MapError(
                                f"map {node.id!r}: input is not a list (got "
                                f"{type(collection).__name__}); map fans out over a collection"
                            )
                        collection = parsed
                    results = await _map_fanout(
                        run_id,
                        node,
                        _child_ref(config, depth + 1),
                        collection,
                        config.concurrency,
                        run_trace,
                    )
                    failures = [
                        (i, r) for i, r in enumerate(results) if r.get("status") == "failed"
                    ]
                    if config.on_error == "fail_fast" and failures:
                        i, r = failures[0]
                        raise MapError(
                            f"map {node.id!r}: element {i} failed (fail_fast): {r.get('error')}"
                        )
                    if config.on_error == "collect":
                        value: Any = [
                            {"index": i, "status": r.get("status"), "output": r.get("output")}
                            if r.get("status") != "failed"
                            else {"index": i, "status": "failed", "error": r.get("error")}
                            for i, r in enumerate(results)
                        ]
                    else:  # fail_fast and all succeeded → the ordered list of element outputs
                        value = [r.get("output") for r in results]
                    live_handles[node.id] = _success_handles(node)
                    for handle_id in live_handles[node.id]:
                        values[(node.id, handle_id)] = value
                else:  # no other orchestration types exist (NODE_TYPE_KIND pins) — guard anyway.
                    raise NotImplementedError(
                        f"orchestration node {node.id!r} (type {node.type!r}) "
                        "is not implemented yet"
                    )

            # M17: record what the node received + emitted and its ok/err status (the durable
            # node_io capture, governed by the run's effective policy — §4).
            if scope is not None:
                scope.set_io(
                    inputs=io_inputs,
                    outputs=_io_output_snapshot(node, values, live_handles),
                )
                scope.set_status(_node_span_status(node, live_handles))

    empty_reason = finalize_empty_reason(
        ir,
        output=output,
        output_produced=output_produced,
        truncated_empty_nodes=truncated_empty_nodes,
        skipped=skipped,
        live_handles=live_handles,
        values=values,
    )
    return output, output_produced, empty_reason


def _log_node(run_id: str, node: Any, *, skipped: bool) -> None:
    # The OTel attach-point seam (M3 §5 / M5 / m13-dbos.md §2): a structured per-node record keyed
    # by run_id. DBOS also emits a span per step; wiring an OTLP exporter is the deferred milestone.
    logger.info(
        "durable.node",
        extra={
            "run_id": run_id,
            "node_id": node.id,
            "node_kind": node.kind,
            "node_type": node.type,
            "skipped": skipped,
        },
    )


def _log_branch(run_id: str, node_id: str, kind: str, index: int) -> None:
    # M14 §2: loop/map emit a span PER iteration/branch so a trace reads against the drawn graph.
    # Same attach-point fidelity as `_log_node` (the seam M5/M13 established) — a structured record
    # keyed by run_id + node_id + index, with the per-branch span NAME stamped as `<node_id>#<i>`.
    # Each iteration/branch is ALSO a DBOS child workflow, so DBOS's own tracer emits a workflow
    # span per branch natively; wiring an OTLP exporter onto both remains the deferred milestone
    # (M13 deferred it). The `#<i>` suffix is what makes the trace legible against the graph node.
    logger.info(
        "durable.branch",
        extra={
            "run_id": run_id,
            "node_id": node_id,
            "span_name": f"{node_id}#{index}",
            "branch_kind": kind,  # "loop" | "map"
            "index": index,
        },
    )


# ── the one registered durable workflow (D3) ────────────────────────────────────────


@DBOS.workflow(name="theygent_run")
async def theygent_run(
    agent_ref: dict[str, Any],
    input_value: Any,
    thread_id: str | None,
    trigger_id: str | None,
) -> dict[str, Any]:
    """The single generic durable workflow (D3). Resolve the pinned saved agent's **immutable** IR,
    create the Run (id == this workflow's id), walk it durably, persist the terminal outcome. Every
    trigger kind converges here via the re-pointed ``fire()`` seam (M13 §4). Returns the same
    non-stream result dict the M12 ``fire`` returned, so the contract above is unchanged.

    ``thread_id`` is accepted for shape-parity with the interactive run path but is ``None`` on the
    durable ``fire()`` route (thread memory is an interactive-cockpit concern); a future durable
    threaded entry threads prior messages in through a step without reshaping this signature."""

    run_id = DBOS.workflow_id  # stable across resume — the run row is keyed by it
    # M14 §1.2: the composition nesting depth rides inside the opaque agent_ref dict (the frozen
    # signature is untouched). 0 at the top; a subgraph/loop/map child is spawned with depth+1.
    depth = int(agent_ref.get("depth", 0)) if isinstance(agent_ref, dict) else 0
    ir_dict = await _resolve_ir_step(agent_ref)
    if ir_dict is None:
        # A dangling pin should be caught at trigger-create time (M12 §1.1); if it somehow reaches
        # here, fail honestly rather than hang. No run row exists yet — record one so it is visible.
        reason = f"agent {agent_ref.get('agent_id')!r} pin did not resolve"
        await _create_run_step(run_id, "", agent_ref.get("agent_id"), None, None, trigger_id)
        await _complete_run_step(run_id, "failed", None, reason)
        return {"runId": run_id, "status": "failed", "error": reason}

    ir = parse_document(ir_dict)
    chash = content_hash(ir)
    # Resolve the first llm's logical id for the Run row; an engine-name binding fails honestly.
    try:
        llms = llm_models(ir)
        model = llms[0][1] if llms else ""
    except EngineNameNotAllowed as exc:
        await _create_run_step(run_id, "", ir.id, ir.version, chash, trigger_id)
        await _complete_run_step(run_id, "failed", None, str(exc))
        return {"runId": run_id, "status": "failed", "error": str(exc)}

    await _create_run_step(run_id, model, ir.id, ir.version, chash, trigger_id)

    # M17: open the run's observability trace. Worker attribution = ``DBOS.executor_id`` (the worker
    # executing this workflow — ``local`` single-worker, a distinct id per distributed worker; on a
    # crash + resume the resuming worker's id stamps the spans IT completes, so the waterfall hops
    # workers). The effective capture level is resolved once per run (§4). All best-effort —
    # telemetry never fails the run it observes.
    run_trace = await _begin_run_trace(run_id, ir, agent_ref)

    try:
        output, _produced, empty_reason = await _durable_walk(
            ir, input_value, run_id, depth, run_trace, ir_dict
        )
    except (RouterError, TemplateError, TransformError, EngineNameNotAllowed) as exc:
        return await _fail_run(run_id, run_trace, str(exc))
    # A bounded-composition guard tripped (depth/iteration/list/timeout) — an honest, named
    # failure, exactly like the router/template errors above.
    except (SubgraphDepthError, LoopError, MapError, HumanTimeout) as exc:
        return await _fail_run(run_id, run_trace, str(exc))
    except NotImplementedError as exc:
        return await _fail_run(run_id, run_trace, str(exc))
    except Exception as exc:  # inference died mid-walk / unreachable plane: fail cleanly (§1.4)
        return await _fail_run(run_id, run_trace, str(exc))

    out_str = _coerce_output(output)
    await _complete_run_step(run_id, "completed", out_str, empty_reason)
    await _finish_run_trace(run_trace, "ok", empty_reason)
    logger.info("durable.run_completed", extra={"run_id": run_id, "trigger_id": trigger_id})
    return {"runId": run_id, "status": "completed", "output": out_str}


async def _begin_run_trace(run_id: str, ir: IRDocument, agent_ref: dict[str, Any]) -> Any:
    """Open the M17 run trace for a durable run (worker attribution + queue.wait). Best-effort: any
    telemetry failure returns ``None`` and the walk proceeds untraced — observability never fails
    the
    run. ``agent_ref['enqueued_ns']`` (stamped at enqueue) lets us emit the ``queue.wait`` phase
    span
    (enqueue → this worker's pickup — often the biggest gap on the durable path, §2)."""
    tel = _res().telemetry
    if tel is None:
        return None
    try:
        capture = await tel.effective_capture_for(ir.id)
        run_trace = tel.begin_run(run_id, executor_id=DBOS.executor_id, capture_level=capture)
        enqueued_ns = agent_ref.get("enqueued_ns") if isinstance(agent_ref, dict) else None
        if enqueued_ns:
            await run_trace.emit_queue_wait(int(enqueued_ns))
        return run_trace
    except Exception as exc:  # pragma: no cover - telemetry is best-effort
        logger.warning("durable.trace_begin_failed", extra={"run_id": run_id, "error": str(exc)})
        return None


async def _finish_run_trace(run_trace: Any, status: str, error: str | None) -> None:
    if run_trace is not None:
        with contextlib.suppress(Exception):
            await run_trace.finish(status=status, error=error)


# ── the scheduled-fire workflow (M13 §4 — schedules → DBOS dynamic schedules) ────────
# theygent's ``trigger`` table stays the source of truth (the frozen M12 contract); a DBOS dynamic
# schedule per enabled schedule-trigger calls THIS one generic scheduled workflow with the trigger
# id as ``context``. It re-reads the trigger (so a config/pin edit between firings is honoured),
# checks it is still enabled (the schedule may be mid-pause), and fires ``theygent_run`` as a child
# workflow. DBOS schedules dedupe across instances — lifting the M12 single-dispatcher constraint.


@DBOS.step(**_RETRY)
async def _load_trigger_step(trigger_id: str) -> dict[str, Any] | None:
    res = _res()
    async with res.sessionmaker() as session:
        trigger = await res.triggers.get(session, trigger_id)
    if trigger is None:
        return None
    return {
        "agent_id": trigger.agent_id,
        "version": trigger.version,
        "content_hash": trigger.content_hash,
        "enabled": trigger.enabled,
        "input": (trigger.config or {}).get("input"),
    }


@DBOS.step(**_RETRY)
async def _mark_fired_step(trigger_id: str, fired_at: datetime) -> None:
    """Stamp ``trigger.last_fired_at`` for a scheduled fire (idempotent — re-stamping the same
    instant is a no-op write). Without this the trigger row lies (``lastFiredAt`` stays NULL for
    the whole durable stretch) AND a later switch back to the in-process dispatcher immediately
    re-fires every schedule, because its due-ness math falls back to ``created_at``."""
    res = _res()
    async with res.sessionmaker() as session, session.begin():
        await res.triggers.mark_fired(session, trigger_id, fired_at)


@DBOS.workflow(name="theygent_scheduled_fire")
async def theygent_scheduled_fire(scheduled_time: datetime, context: Any) -> None:
    """The one generic scheduled workflow DBOS dynamic schedules drive (M13 §4). ``context`` is the
    trigger id. Re-resolve the trigger (it stays the source of truth); if it vanished or was
    disabled, no-op (a boot-reconcile or pause may lag a beat). Otherwise fire ``theygent_run`` as a
    child workflow with the trigger's pin + ``config.input``."""
    trigger_id = str(context)
    trig = await _load_trigger_step(trigger_id)
    if trig is None or not trig["enabled"]:
        logger.info("durable.schedule_skip", extra={"trigger_id": trigger_id})
        return
    agent_ref = {
        "agent_id": trig["agent_id"],
        "version": trig["version"],
        "content_hash": trig["content_hash"],
    }
    # The trigger row stays the source of truth for "when did this last fire" in BOTH modes —
    # stamped from the schedule's own instant (deterministic across replay), before the run so a
    # crashed fire still advances the window (the no-backfill posture the dispatcher established).
    await _mark_fired_step(trigger_id, scheduled_time)
    await theygent_run(agent_ref, trig["input"], None, trigger_id)
