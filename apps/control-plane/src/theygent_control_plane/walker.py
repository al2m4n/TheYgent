"""The IR walker — the IR↔runtime seam: walk an :class:`~theygent_ir.IRDocument` node by node and
execute it against the existing M3/M4 spine (docs/private/m5.md §5, m6.md §4).

**This is an interpreter, not a compiler.** The walker lowers each node to in-process Python now;
the §8.7 compiler will later re-target the same IR to a durable runtime (Temporal/Restate/DBOS).
The seam — IR in, a stream of deltas out — is identical either way; that is the entire point of
§8.1's determinism split. So the walker imports no durable-runtime SDK, wraps no node in
retries/checkpoints, and persists no per-node intermediate state. "Durability" is exactly
``/runs`` today: the ``Run`` row records the final outcome.

Two seam rules it holds (M5 §3.2 / §5):

* **Dispatch is by ``kind``** (``boundary``/``activity``/``orchestration``), then by ``type``
  *within* a kind — so adding a node type is an additive handler, and the §8.7 compiler can lower
  the same IR to a durable target with the same dispatch.
* **No DB calls inside the walker.** It is a pure async function over a :class:`WalkContext`: the
  control-plane loads thread memory and persists the ``Run`` through the same M4 seams ``/runs``
  uses, then hands the walker the prior messages, the gateway client, and the tool registry.

Node types executed: M5 shipped ``input``/``output``/``llm``; M6 adds ``tool`` (the first non-llm
activity — a built-in registered callable) and ``router`` (the first orchestration node —
handle-name branching). M6 introduces **conditional execution**: a ``router`` selects one outgoing
handle and a ``tool`` takes its ``ok`` or ``err`` handle, so edges from the un-taken handle(s) are
not traversed and downstream nodes with no live inbound ``data`` edge are *skipped* (logged, not
executed). See ``apps/control-plane/CLAUDE.md`` for the two recorded M6 forks.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from theygent_gateway_client import GatewayClient
from theygent_ir import (
    Edge,
    IRDocument,
    LlmConfig,
    McpToolConfig,
    Node,
    RouterConfig,
    ToolConfig,
    topological_order,
)

from theygent_control_plane.mcp import (
    McpConnectionError,
    McpManager,
    McpResult,
    McpToolNotFound,
)
from theygent_control_plane.tools import DEFAULT_REGISTRY, ToolRegistry

logger = logging.getLogger("theygent.control_plane.walker")

# The managed-engine names (theygent-graph-schema.md §8.4). A resolved model id that is an
# engine name is not a logical id — the §9.1.1 invariant the inference seam enforces. Mirrors
# the same constant in ``app.py`` for the ``/runs`` path; both guard the one seam rule.
_ENGINE_NAMES = frozenset({"mlx", "vllm", "llamacpp"})


class EngineNameNotAllowed(ValueError):
    """A node resolved its model to an engine name, not a logical id (§8.4 / §3.2). Raised at
    resolution time so the control-plane rejects it up front — before a ``Run`` exists and
    before anything reaches the wire — exactly as ``/runs`` rejects an engine name today."""


class RouterError(RuntimeError):
    """A ``router`` node could not select an outgoing handle at runtime (m6.md §3.2): the upstream
    output had no ``handle`` field, or named a handle the router doesn't declare. No fallback
    guessing — the control-plane maps it to a failed run with a clear reason."""


class TemplateError(ValueError):
    """A substitutable field referenced a ``$``-token that couldn't be resolved (m9.md §1.2): an
    unknown token (anything not rooted at ``$in``, e.g. the removed ``$input`` or a typo ``$nope``)
    or a ``$in.field`` path that doesn't exist in the in-port value. The whole point of M9 Part A is
    that this fails LOUDLY — naming the node, the token, and the available fields — instead of
    passing through as literal text and producing a green run with nonsense output. The
    control-plane maps it to a failed run with a clear reason, exactly like :class:`RouterError`."""


@dataclass(frozen=True)
class Delta:
    """One streamed token piece, tagged with the node that produced it. The control-plane's SSE
    relay turns a ``content`` ``Delta`` into ``event: delta`` / ``data: {runId, delta}`` — the
    *same* shape ``/runs`` emits, so a graph run is indistinguishable on the wire from a prompt run.

    ``kind`` separates the model's *answer* (``content``) from its *thinking* (``reasoning`` — a
    reasoning model's ``reasoning_content``). Only ``content`` accumulates into the run's output; a
    ``reasoning`` delta is streamed as visible progress (``event: reasoning``) so a thinking model
    that spends many tokens reasoning before answering doesn't look frozen, and is never mistaken
    for the answer."""

    node_id: str
    content: str
    kind: str = "content"


@dataclass
class WalkResult:
    """The walker's out-of-band result the delta stream can't carry: the run's final ``output``,
    bound from the executed ``output`` boundary node's in-port (m6.md §4). M5's output equalled the
    streamed ``llm`` text; M6's may be a ``tool`` result that never streamed, so the canonical
    output is the output node's value, not the accumulated deltas. The control-plane reads it after
    the walk to populate the non-stream response and the thread's assistant turn.

    ``output_produced`` is True iff an ``output`` boundary node actually executed (vs. being skipped
    because its inbound branch was dead) — it disambiguates a legitimately ``None`` output from "no
    output node ran at all". ``empty_reason`` is set (m9.md §2.4) when NO output was produced AND an
    upstream node short-circuited to its ``err`` handle: the run reached no output because of a
    handled-but-unconsumed error, so the control-plane surfaces an honest reason instead of a green
    ``completed`` with ``output: ""`` and ``error: null``. ``truncated_empty_nodes`` records ``llm``
    nodes that ended with empty ``content`` and ``finish_reason == 'length'`` — the model spent its
    whole token budget (often a reasoning model thinking) and produced no answer; if that empties
    the final output, ``empty_reason`` names it so the run is legible rather than a green blank."""

    output: Any = None
    output_produced: bool = False
    empty_reason: str | None = None
    truncated_empty_nodes: list[str] = field(default_factory=list)


@dataclass
class WalkContext:
    """Everything the walker needs that it must not fetch itself (M5 §5). ``prior_messages`` is the
    thread replay the control-plane already loaded via the M4 store; ``extra_headers`` carries the
    ``x-theygent-run-id`` correlation header; ``tools`` is the registry ``tool`` nodes dispatch
    against. The walker does no I/O beyond the gateway call and the tool callables."""

    gateway: GatewayClient
    run_id: str
    prior_messages: list[dict[str, Any]] = field(default_factory=list)
    extra_headers: Mapping[str, str] = field(default_factory=dict)
    tools: ToolRegistry = field(default_factory=lambda: DEFAULT_REGISTRY)
    # The MCP server registry/connections (app-scoped; the control-plane passes its instance). A
    # fresh empty manager by default — fine for graphs with no mcp_tool node.
    mcp: McpManager = field(default_factory=McpManager)


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _lower_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Lower IR generation params (camelCase per §8.4, e.g. ``maxTokens``) to the OpenAI-native
    snake_case the data plane speaks (``max_tokens``). This is part of the walker's IR→execution
    lowering, not a contract change: the IR stays camelCase (consistent with the §8.2 envelope),
    while the §9.1 seam stays OpenAI-compatible. A generic camel→snake conversion handles every
    generation param (``topP``→``top_p``, ``responseFormat``→``response_format``, …); single-word
    params (``temperature``, ``stop``, ``seed``) are unchanged."""

    return {_CAMEL_BOUNDARY.sub("_", k).lower(): v for k, v in params.items()}


def resolve_model(ir: IRDocument, config: LlmConfig) -> tuple[str, dict[str, Any]]:
    """Resolve an ``llm`` node's bound model to the (logical id, generation params) forwarded to
    the inference seam (§8.4). Raises :class:`EngineNameNotAllowed` if the binding's ``model`` is
    an engine name — the logical-id invariant, enforced before anything is created or sent."""

    binding = ir.models[config.model]
    if binding.model in _ENGINE_NAMES:
        raise EngineNameNotAllowed(
            f"{binding.model!r} is an engine name, not a logical model id; "
            "the model binding must carry a logical id"
        )
    return binding.model, _lower_params(binding.params)


def llm_models(ir: IRDocument) -> list[tuple[Node, str, dict[str, Any]]]:
    """Every ``llm`` node with its resolved (model id, params), in document order. The
    control-plane uses this to (a) reject an engine-name binding before creating the ``Run`` and
    (b) record the resolved logical id on the ``Run`` (the trivial graph has exactly one)."""

    out: list[tuple[Node, str, dict[str, Any]]] = []
    for node in ir.nodes:
        if node.type == "llm":
            model_id, params = resolve_model(ir, LlmConfig.model_validate(node.config))
            out.append((node, model_id, params))
    return out


def tool_nodes(ir: IRDocument) -> list[tuple[Node, str]]:
    """Every ``tool`` node with its referenced tool name, in document order. The control-plane
    checks each name against the registry up front — before creating the ``Run`` → 400
    ``tool_not_found`` — so an unknown tool never reaches the walker (m6.md §5). The IR package
    stays pure (validates ``config`` *shape*, not registry membership)."""

    return [
        (node, ToolConfig.model_validate(node.config).tool)
        for node in ir.nodes
        if node.type == "tool"
    ]


def mcp_tool_nodes(ir: IRDocument) -> list[tuple[Node, str, str]]:
    """Every ``mcp_tool`` node with its (server name, tool name), in document order. The
    control-plane checks server membership up front (→ 400 ``mcp_server_not_found``, no Run) and —
    if that server is already connected — the tool against its cached capability list (m7.md §4)."""

    out: list[tuple[Node, str, str]] = []
    for node in ir.nodes:
        if node.type == "mcp_tool":
            cfg = McpToolConfig.model_validate(node.config)
            out.append((node, cfg.server, cfg.tool))
    return out


# ── value references + inline templating: $in / $in.a.b (every substituting node) ──
#
# M9 Part A (m9.md §1): ONE token language across every node type that substitutes —
# ``$in`` (the whole in-port value) and ``$in.a.b`` (a path into it). The ``tool``/``mcp_tool``/
# ``router`` nodes use it as a whole-value reference (``_resolve_ref``); the ``llm`` node uses it
# inline inside ``messages`` content (``_render_template``). ``$in`` is the exact semantic of the
# llm node's old ``$input``, renamed — and now field-aware too. An unresolved token is a loud
# :class:`TemplateError`, never a silent literal pass-through; ``$$`` escapes a literal ``$``.


class _RefError(ValueError):
    """A ``$in``-reference couldn't be resolved against the upstream value (missing key / not a
    dict). Internal — the router maps it to :class:`RouterError`, a tool maps it to an ``err``
    binding, the llm template maps it to :class:`TemplateError`."""


def _parse_if_json(value: Any) -> Any:
    """Parse a JSON string into structured data so a path step can traverse it. An ``llm`` node
    emits text, so a downstream ``router``/``tool`` reading ``$in.handle`` needs the JSON the model
    produced parsed first. A non-string (already-structured) value passes through unchanged."""

    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def _resolve_path(input_value: Any, parts: list[str]) -> Any:
    """Walk a ``$in.a.b`` path (``parts == ["a", "b"]``) into the in-port value, parsing JSON
    strings as it descends. An empty path is the whole value. Raises :class:`_RefError` naming the
    missing key when a step can't be taken — the shared core of the whole-value ``_resolve_ref`` and
    the inline ``_render_template`` so the two surfaces resolve ``$in`` identically (m9.md §1.1)."""

    cur = input_value
    for key in parts:
        cur = _parse_if_json(cur)
        if not isinstance(cur, dict) or key not in cur:
            raise _RefError(key)
        cur = cur[key]
    return cur


def _resolve_ref(ref: Any, input_value: Any) -> Any:
    """Resolve one arg/select value against the node's in-port value (m6.md §2/§3.2). ``$in`` is
    the whole value; ``$in.a.b`` traverses a path into it (parsing JSON strings as it goes). Any
    non-``$in`` value (a literal string, dict, number) is returned unchanged — so an arg like
    ``{"value": {"handle": "yes"}}`` is a literal, not a reference. No expression language, no
    code execution (§7): just a value reference."""

    if not isinstance(ref, str):
        return ref
    if ref == "$in":
        return input_value
    if not ref.startswith("$in."):
        return ref
    try:
        return _resolve_path(input_value, ref[len("$in.") :].split("."))
    except _RefError as exc:
        raise _RefError(
            f"cannot resolve {ref!r}: no key {exc.args[0]!r} in upstream value"
        ) from exc


#: A substitution token in a content string: ``$$`` (escapes a literal ``$``) or ``$<expr>`` where
#: ``<expr>`` is a dotted identifier path (``in``, ``in.body``, but also a bare ``input``/``nope``
#: so the renderer can reject an unknown root with a precise message rather than skip past it).
_TEMPLATE_TOKEN = re.compile(r"\$\$|\$([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)")


def _available_fields(input_value: Any) -> str:
    """A human hint for a TemplateError: the top-level fields of the in-port value if it's an
    object, else a note that it isn't one (so ``$in.body`` against a bare string is legible)."""

    parsed = _parse_if_json(input_value)
    if isinstance(parsed, dict):
        return f"available fields: {', '.join(sorted(parsed))}" if parsed else "the object is empty"
    return f"the in-port value is a {type(parsed).__name__}, not an object with fields"


def _stringify_token(value: Any) -> str:
    """Render a resolved token value into the surrounding text: ``None`` is empty (parity with the
    old ``$input`` swap), a string is itself, anything else is JSON (so ``$in.obj`` is legible)."""

    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def _render_template(content: str, input_value: Any, node_id: str) -> str:
    """Substitute ``$in`` / ``$in.field`` tokens in one content string against the node's in-port
    value (m9.md §1.1). ``$$`` is a literal ``$``. An unknown root (anything but ``$in``, e.g. the
    removed ``$input``) or an unresolvable ``$in.field`` raises :class:`TemplateError` naming the
    node, the token, and the available fields — the F1.1-class fix: no silent literal, no green run
    with nonsense (m9.md §1.2)."""

    def _sub(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        expr = match.group(1)
        parts = expr.split(".")
        if parts[0] != "in":
            raise TemplateError(
                f"node {node_id!r}: unknown template token '${expr}' — the substitution token is "
                f"$in (the whole in-port value) or $in.field (a field of it); "
                f"{_available_fields(input_value)}"
            )
        try:
            value = _resolve_path(input_value, parts[1:])
        except _RefError as exc:
            raise TemplateError(
                f"node {node_id!r}: cannot resolve '${expr}': no field {exc.args[0]!r} in the "
                f"in-port value; {_available_fields(input_value)}"
            ) from exc
        return _stringify_token(value)

    return _TEMPLATE_TOKEN.sub(_sub, content)


def _render_messages(node: Node, config: LlmConfig, input_value: Any) -> list[dict[str, str]]:
    """Render every ``messages`` content field through the unified ``$in`` template (m9.md §1.1).
    Replaces M5's literal ``$input`` swap: ``$in`` is the whole in-port value (exact parity), and
    ``$in.field`` now drills into it when the upstream value is an object."""

    return [
        {"role": msg.role, "content": _render_template(msg.content, input_value, node.id)}
        for msg in config.messages
    ]


# ── conditional dataflow: edge liveness, node skipping (m6.md §4) ─────────────


def _success_handles(node: Node) -> set[str]:
    """The node's non-error out-handles — where a normal result is bound (input/llm/tool-ok)."""
    return {p.id for p in node.ports.out if p.type != "error"}


def _error_handles(node: Node) -> set[str]:
    """The node's ``error``-typed out-handles — where a tool's structured error is bound."""
    return {p.id for p in node.ports.out if p.type == "error"}


def _edge_live(edge: Edge, skipped: set[str], live_handles: dict[str, set[str]]) -> bool:
    """An edge carries a value this run iff its source executed (not ``skipped``) AND the source
    activated that out-handle. A ``router`` activates only its selected handle; a ``tool``
    activates ``ok`` xor ``err``; everything else activates all its (non-error) handles. So edges
    from an un-taken branch are dead — the substrate of router/tool branching (m6.md §4)."""

    if edge.source in skipped:
        return False
    live = live_handles.get(edge.source)
    if live is None:  # source not (yet) executed — topo order makes this only the unreached case
        return False
    return edge.source_handle in live


def _is_skipped(
    node: Node, edges: list[Edge], skipped: set[str], live_handles: dict[str, set[str]]
) -> bool:
    """A node is skipped iff it declares inbound ``data`` edges and *every* one is dead (m6.md §4).
    The ``input`` boundary has no inbound edges and is never skipped; a node with only ``control``
    edges (no data deps) runs."""

    if node.type == "input":
        return False
    inbound = [e for e in edges if e.target == node.id and e.channel == "data"]
    if not inbound:
        return False
    return not any(_edge_live(e, skipped, live_handles) for e in inbound)


def _incoming_value(
    node: Node,
    edges: list[Edge],
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> Any:
    """The value on a node's in-port: the value carried by its first *live* inbound ``data`` edge
    (§8.7 step 3). Dead edges (from an un-taken branch) are ignored, so a node fed by two branches
    reads whichever one actually ran."""

    for edge in edges:
        if (
            edge.target == node.id
            and edge.channel == "data"
            and _edge_live(edge, skipped, live_handles)
        ):
            return values.get((edge.source, edge.source_handle))
    return None


async def walk(
    ir: IRDocument, input_value: Any, ctx: WalkContext, result: WalkResult | None = None
) -> AsyncIterator[Delta]:
    """Walk ``ir`` node by node, yielding ``llm`` deltas as they stream (m5.md §5, m6.md §4).

    Assumes a *validated* IR (the control-plane runs ``validate_graph`` + the up-front engine/tool
    checks before creating the ``Run``, so an invalid graph never persists). Threads ``data`` edges
    as values through a ``(node_id, out_handle) -> value`` map; tracks per-node *live* out-handles
    so router/tool branching skips un-taken edges. Each node's ``id`` is stamped on a structured
    log keyed by ``run_id`` (``skipped: true|false``) — the OTel attach-point, not OTel itself.
    The executed ``output`` node's value is written to ``result`` (the run's canonical output)."""

    values: dict[tuple[str, str], Any] = {}
    skipped: set[str] = set()
    live_handles: dict[str, set[str]] = {}

    for node in topological_order(ir):
        if _is_skipped(node, ir.edges, skipped, live_handles):
            skipped.add(node.id)
            _log_node(ctx, node, skipped=True)
            continue
        _log_node(ctx, node, skipped=False)

        if node.kind == "boundary":
            _walk_boundary(node, ir, input_value, values, skipped, live_handles, result)
        elif node.kind == "activity":
            if node.type == "llm":
                async for delta in _walk_llm(node, ir, ctx, values, skipped, live_handles, result):
                    yield delta
            elif node.type == "tool":
                await _walk_tool(node, ir, ctx, values, skipped, live_handles)
            elif node.type == "mcp_tool":
                await _walk_mcp_tool(node, ir, ctx, values, skipped, live_handles)
            else:
                # agent / rag / retriever / memory / code — deferred (§7).
                raise NotImplementedError(
                    f"activity node {node.id!r} (type {node.type!r}) is not implemented yet"
                )
        elif node.kind == "orchestration":
            if node.type == "router":
                _walk_router(node, ir, values, skipped, live_handles)
            else:
                # loop / map — deferred (§7).
                raise NotImplementedError(
                    f"orchestration node {node.id!r} (type {node.type!r}) is not implemented yet"
                )

    # m9.md §2.4: if the walk finished with NO output node having run, and some node short-circuited
    # to its `err` handle, the run reached no output *because* of that handled-but-unconsumed error
    # (its `ok` path to the output was skipped). Surface an honest reason so the control-plane
    # doesn't report a green `completed` masquerading as success. This does NOT change the M6
    # error-as-structured-output contract: wiring `err` somewhere is still how you handle an error.
    if result is not None and result.empty_reason is None:
        if not result.output_produced:
            errored = [
                node.id
                for node in ir.nodes
                if node.id not in skipped
                and live_handles.get(node.id)
                and live_handles[node.id] <= _error_handles(node)
            ]
            if errored:
                result.empty_reason = f"output empty: upstream error on node {errored[0]!r}"
        elif _is_blank(result.output) and result.truncated_empty_nodes:
            # Output ran but is blank because an llm spent its whole budget (reasoning model
            # thinking, or maxTokens too low) and produced no content. Name it, not a green blank.
            result.empty_reason = (
                f"output empty: node {result.truncated_empty_nodes[0]!r} hit the token limit "
                "(finish_reason=length) before producing content — raise maxTokens (a reasoning "
                "model can spend the whole budget thinking)"
            )


def _log_node(ctx: WalkContext, node: Node, *, skipped: bool) -> None:
    logger.info(
        "graph.node",
        extra={
            "run_id": ctx.run_id,
            "node_id": node.id,
            "node_kind": node.kind,
            "node_type": node.type,
            "skipped": skipped,
        },
    )


def _walk_boundary(
    node: Node,
    ir: IRDocument,
    input_value: Any,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
    result: WalkResult | None,
) -> None:
    if node.type == "input":
        # Bind the run's input to the input node's out-handles (§5: in/out plumbing).
        live_handles[node.id] = _success_handles(node)
        for handle in live_handles[node.id]:
            values[(node.id, handle)] = input_value
    elif node.type == "output":
        # The output node's in-port value IS the run's canonical output (m6.md §4) — read from a
        # live edge, so a routed run returns whichever branch actually produced it.
        live_handles[node.id] = set()
        value = _incoming_value(node, ir.edges, values, skipped, live_handles)
        if result is not None:
            result.output = value
            result.output_produced = True  # an output node ran (m9.md §2.4)
    else:
        # human / subgraph — deferred (§7).
        raise NotImplementedError(
            f"boundary node {node.id!r} (type {node.type!r}) is not implemented yet"
        )


async def _walk_llm(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
    result: WalkResult | None = None,
) -> AsyncIterator[Delta]:
    config = LlmConfig.model_validate(node.config)
    model_id, params = resolve_model(ir, config)
    input_value = _incoming_value(node, ir.edges, values, skipped, live_handles)
    # Naive full replay (M4 §4): prior thread turns verbatim, then this turn's rendered prompt.
    messages = [*ctx.prior_messages, *_render_messages(node, config, input_value)]

    # open_stream sends the request and validates status, so a pre-stream 503/404 raises here —
    # before any delta — letting the control-plane surface a clean status (mirrors /runs).
    upstream = await ctx.gateway.open_stream(
        model=model_id, messages=messages, params=params, extra_headers=ctx.extra_headers
    )
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
        # A reasoning model emits `reasoning_content` (its thinking) before `content` (its answer).
        # Stream the thinking as visible progress so the model doesn't look frozen, but NEVER fold
        # it into the output — only `content` is the answer (the binding downstream nodes read).
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            yield Delta(node_id=node.id, content=reasoning, kind="reasoning")
        content = getattr(delta, "content", None)
        if content:
            output += content
            yield Delta(node_id=node.id, content=content)

    # An empty answer with finish_reason=length means the budget ran out before any content (often
    # a reasoning model that spent it all thinking). Record it so the run is legible, not a blank.
    if result is not None and finish_reason == "length" and not output.strip():
        result.truncated_empty_nodes.append(node.id)

    # Activate the success handles and bind the full text, so downstream data edges pick it by
    # ``sourceHandle`` (e.g. "ok"). An llm error raises mid-stream (failed run) — it never binds.
    live_handles[node.id] = _success_handles(node)
    for handle in live_handles[node.id]:
        values[(node.id, handle)] = output


async def _walk_tool(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Resolve ``config.tool`` against the registry, template ``config.args`` against the in-port
    value, ``await`` the callable, and bind the result to the ``ok`` handle(s). A raised exception
    is a *structured error*, not a run failure (m6.md §4): bind the message to the ``err`` handle
    and continue — downstream ``err`` edges run, ``ok`` edges are skipped. (A non-200 HTTP status
    is a normal return value, bound to ``ok``; only transport failures raise.)"""

    config = ToolConfig.model_validate(node.config)
    input_value = _incoming_value(node, ir.edges, values, skipped, live_handles)
    try:
        fn = ctx.tools.get(config.tool)
        args = {k: _resolve_ref(v, input_value) for k, v in config.args.items()}
        value = await fn(**args)
    except Exception as exc:  # tool failed: structured err output, run continues (§4).
        live_handles[node.id] = _error_handles(node)
        for handle in live_handles[node.id]:
            values[(node.id, handle)] = str(exc)
        logger.info(
            "graph.tool_error",
            extra={
                "run_id": ctx.run_id,
                "node_id": node.id,
                "tool": config.tool,
                "error": str(exc),
            },
        )
        return

    live_handles[node.id] = _success_handles(node)
    for handle in live_handles[node.id]:
        values[(node.id, handle)] = value


async def invoke_mcp_tool(
    server: str, tool: str, args: dict[str, Any], ctx: WalkContext
) -> McpResult:
    """THE single chokepoint every ``mcp_tool`` invocation passes through (m7.md §3 governance
    note). This is the future governance attach-point — policy-as-code and audit logging hook in
    HERE, observing the *invocation* (which server, which tool, the arg shape), never the contents
    the server returns (the §10 redaction posture). M7 records the seam; the L2 policy layer is a
    separate, evidence-driven milestone. For now it simply forwards to the MCP manager."""

    return await ctx.mcp.call_tool(server, tool, args)


async def _walk_mcp_tool(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Invoke an external MCP server's tool — the same ok/err contract as a local ``tool`` (m7.md
    §4), only the transport differs. Template ``args`` with the M6 ``$in`` resolver, call through
    the ``invoke_mcp_tool`` chokepoint, bind the result to ``ok``. A tool-level error
    (``McpResult.is_error``), a transport failure after one reconnect-retry, an unknown tool, or an
    unresolvable arg ref all bind ``err`` and the run continues.

    **The err payload must be actionable**, not just present: the message names the *server* and the
    *kind* of failure (connection vs the server's own tool error vs unknown tool), so the agent
    reading ``err`` can decide its next move ("the file server is down" vs "that tool doesn't
    exist"). A bare OS error or stack blob would pass "err bound" but fail that bar."""

    config = McpToolConfig.model_validate(node.config)
    server, tool = config.server, config.tool
    input_value = _incoming_value(node, ir.edges, values, skipped, live_handles)
    result: McpResult | None = None
    try:
        args = {k: _resolve_ref(v, input_value) for k, v in config.args.items()}
        result = await invoke_mcp_tool(server, tool, args, ctx)
        if result.is_error:  # the server reported a tool-level error (its own message)
            error_message = f"mcp server {server!r} tool {tool!r} error: {_stringify(result.value)}"
        else:
            error_message = None
    except McpConnectionError as exc:  # the server is unreachable, even after the reconnect-retry
        error_message = f"mcp server {server!r} unavailable (connection failed): {exc}"
    except McpToolNotFound as exc:  # the connected server doesn't expose this tool (already named)
        error_message = str(exc)
    except Exception as exc:  # a bad arg ref or other invocation failure
        error_message = f"mcp server {server!r} tool {tool!r} failed: {exc}"

    if error_message is not None:
        live_handles[node.id] = _error_handles(node)
        for handle in live_handles[node.id]:
            values[(node.id, handle)] = error_message
        logger.info(
            "graph.mcp_error",
            extra={
                "run_id": ctx.run_id,
                "node_id": node.id,
                "server": server,
                "tool": tool,
                "error": error_message,
            },
        )
        return

    assert result is not None
    live_handles[node.id] = _success_handles(node)
    for handle in live_handles[node.id]:
        values[(node.id, handle)] = result.value


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value)


def _is_blank(value: Any) -> bool:
    """True if the run's final output carries nothing meaningful — ``None`` or whitespace-only
    text. Used to decide whether a truncated-empty llm should surface an honest reason (a non-string
    value like a tool dict is never blank)."""
    return value is None or (isinstance(value, str) and not value.strip())


def _walk_router(
    node: Node,
    ir: IRDocument,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Resolve ``config.select`` against the in-port value to the name of an outgoing handle, then
    activate only that handle and forward the in-port value along it (m6.md §3.2). Handle-name
    routing: the upstream node chose the branch; the router executes the choice. A select that
    can't resolve, or names a handle the router doesn't declare, is a :class:`RouterError` (failed
    run, clear reason) — no fallback guessing."""

    config = RouterConfig.model_validate(node.config)
    input_value = _incoming_value(node, ir.edges, values, skipped, live_handles)
    try:
        selected = _resolve_ref(config.select, input_value)
    except _RefError as exc:
        raise RouterError(f"router {node.id!r}: {exc}") from exc

    out_ids = {p.id for p in node.ports.out}
    if not isinstance(selected, str) or selected not in out_ids:
        raise RouterError(
            f"router {node.id!r}: select {config.select!r} resolved to {selected!r}, "
            f"not one of its out-handles {sorted(out_ids)}"
        )
    live_handles[node.id] = {selected}
    values[(node.id, selected)] = input_value
