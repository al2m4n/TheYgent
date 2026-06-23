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

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from dbos import DBOS
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from theygent_ir import (
    IRDocument,
    LlmConfig,
    McpToolConfig,
    RouterConfig,
    ToolConfig,
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
    _bind_outcome,
    _collect_in_ports,
    _is_skipped,
    _RefError,
    _render_messages,
    _resolve_ref,
    _single_in_value,
    _success_handles,
    execute_llm,
    execute_mcp_tool,
    execute_tool,
    finalize_empty_reason,
    llm_models,
    resolve_model,
)

from theygent_control_plane.durable.bus import DeltaBus  # isort: skip

logger = logging.getLogger("theygent.control_plane.durable")

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
    messages: list[dict[str, str]],
) -> dict[str, Any]:
    """The ``llm`` activity as a durable step. Streams tokens to the in-process delta bus as a side
    effect (§6 — non-durable), returns the assembled answer (the only journaled value). On replay
    this body does NOT run, so tokens are not regenerated/re-streamed (D7)."""
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
    )
    return {
        "output": out.output,
        "finish_reason": out.finish_reason,
        "truncated_empty": out.truncated_empty,
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


# ── the deterministic durable walk (orchestration is inline; activities are steps) ──


async def _durable_walk(
    ir: IRDocument, input_value: Any, run_id: str
) -> tuple[Any, bool, str | None]:
    """Walk a validated IR deterministically, awaiting an activity step per ``activity`` node and
    running ``orchestration``/``boundary`` inline (M13 §2). This mirrors ``walker.walk`` exactly —
    same traversal order, same edge-liveness/skip logic, same value threading — but the I/O lives in
    journaled steps so the run resumes from the last completed activity. Returns
    ``(output, output_produced, empty_reason)``. The thread-memory replay is empty on the durable
    ``fire()`` path (un-threaded); prior messages are threaded in by the caller if ever needed."""

    values: dict[tuple[str, str], Any] = {}
    skipped: set[str] = set()
    live_handles: dict[str, set[str]] = {}
    truncated_empty_nodes: list[str] = []
    output: Any = None
    output_produced = False

    for node in topological_order(ir):
        if _is_skipped(node, ir.edges, skipped, live_handles):
            skipped.add(node.id)
            _log_node(run_id, node, skipped=True)
            continue
        _log_node(run_id, node, skipped=False)

        if node.kind == "boundary":
            if node.type == "input":
                live_handles[node.id] = _success_handles(node)
                for handle in live_handles[node.id]:
                    values[(node.id, handle)] = input_value
            elif node.type == "output":
                live_handles[node.id] = set()
                ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                output = _single_in_value(ports, node)
                output_produced = True
            else:  # human / subgraph — deferred (§7); the durable boundary mechanism lands later.
                raise NotImplementedError(
                    f"boundary node {node.id!r} (type {node.type!r}) is not implemented yet"
                )

        elif node.kind == "activity":
            ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
            if node.type == "llm":
                config = LlmConfig.model_validate(node.config)
                model_id, params = resolve_model(ir, config)
                messages = _render_messages(node, config, ports)
                res = await _llm_step(run_id, node.id, model_id, params, messages)
                if res["truncated_empty"]:
                    truncated_empty_nodes.append(node.id)
                live_handles[node.id] = _success_handles(node)
                for handle in live_handles[node.id]:
                    values[(node.id, handle)] = res["output"]
            elif node.type == "tool":
                config = ToolConfig.model_validate(node.config)
                try:
                    args = {k: _resolve_ref(v, ports, node.id) for k, v in config.args.items()}
                except Exception as exc:  # an unresolvable arg ref is a structured err (m6.md §4)
                    outcome = ActivityOutcome(ok=False, value=str(exc))
                else:
                    step_out = await _tool_step(run_id, node.id, config.tool, args)
                    outcome = ActivityOutcome(ok=step_out["ok"], value=step_out["value"])
                _bind_outcome(node, outcome, values, live_handles)
            elif node.type == "mcp_tool":
                config = McpToolConfig.model_validate(node.config)
                server, tool = config.server, config.tool
                try:
                    args = {k: _resolve_ref(v, ports, node.id) for k, v in config.args.items()}
                except Exception as exc:
                    outcome = ActivityOutcome(
                        ok=False, value=f"mcp server {server!r} tool {tool!r} failed: {exc}"
                    )
                else:
                    step_out = await _mcp_step(run_id, node.id, server, tool, args)
                    outcome = ActivityOutcome(ok=step_out["ok"], value=step_out["value"])
                _bind_outcome(node, outcome, values, live_handles)
            else:  # agent / rag / retriever / memory / code — deferred (§7)
                raise NotImplementedError(
                    f"activity node {node.id!r} (type {node.type!r}) is not implemented yet"
                )

        elif node.kind == "orchestration":
            if node.type == "router":
                # Inline, deterministic — NO I/O (the determinism guard §8.1). Mirrors _walk_router.
                config = RouterConfig.model_validate(node.config)
                ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
                try:
                    selected = _resolve_ref(config.select, ports, node.id)
                except _RefError as exc:
                    raise RouterError(f"router {node.id!r}: {exc}") from exc
                out_ids = {p.id for p in node.ports.out}
                if not isinstance(selected, str) or selected not in out_ids:
                    raise RouterError(
                        f"router {node.id!r}: select {config.select!r} resolved to {selected!r}, "
                        f"not one of its out-handles {sorted(out_ids)}"
                    )
                live_handles[node.id] = {selected}
                values[(node.id, selected)] = _single_in_value(ports, node)
            else:  # loop / map — deferred (§7)
                raise NotImplementedError(
                    f"orchestration node {node.id!r} (type {node.type!r}) is not implemented yet"
                )

    empty_reason = finalize_empty_reason(
        ir,
        output=output,
        output_produced=output_produced,
        truncated_empty_nodes=truncated_empty_nodes,
        skipped=skipped,
        live_handles=live_handles,
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

    try:
        output, _produced, empty_reason = await _durable_walk(ir, input_value, run_id)
    except (RouterError, TemplateError, EngineNameNotAllowed) as exc:
        await _complete_run_step(run_id, "failed", None, str(exc))
        return {"runId": run_id, "status": "failed", "error": str(exc)}
    except NotImplementedError as exc:
        await _complete_run_step(run_id, "failed", None, str(exc))
        return {"runId": run_id, "status": "failed", "error": str(exc)}
    except Exception as exc:  # inference died mid-walk / unreachable plane: fail cleanly (§1.4)
        await _complete_run_step(run_id, "failed", None, str(exc))
        return {"runId": run_id, "status": "failed", "error": str(exc)}

    out_str = _coerce_output(output)
    await _complete_run_step(run_id, "completed", out_str, empty_reason)
    logger.info("durable.run_completed", extra={"run_id": run_id, "trigger_id": trigger_id})
    return {"runId": run_id, "status": "completed", "output": out_str}


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
    await theygent_run(agent_ref, trig["input"], None, trigger_id)
