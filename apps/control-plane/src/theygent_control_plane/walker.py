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

M10 closes the dominant expressiveness gap (F2.1): a node may now declare **more than one** named
in-port, each fed by a distinct upstream ``data`` edge, addressed in templates as ``$in.<port>``
(``$in`` stays sugar for the default port ``in``). The only walker change is in-port *collection*
(``_collect_in_ports`` gathers the named producers instead of assuming one) and *resolution*
(``_resolve_in`` is port-first); dispatch-by-``kind`` and every handler are otherwise untouched,
and the determinism boundary (§8.1) is unmoved — multi-input is binding + addressing, not a new
``kind``, node type, or I/O (m10.md §1.2/§3).
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


# ── value references + inline templating: $in / $in.<port> / $in.<port>.<path> ──
#
# M10 (m10.md §1.2): ONE token language across every substituting node, addressed **port-first**.
# A node declares its in-ports (``ports.in``); each is fed by one ``data`` edge. The token selects
# a port by name:
#   ``$in``                  → the default in-port named ``in`` (M9 parity sugar)
#   ``$in.<port>``           → the whole value on that named in-port
#   ``$in.<port>.<path>``    → drill into that port's value (M9's field-drilling lives here now)
# The first segment after ``$in.`` is ALWAYS a port name, resolved port-first with a LOUD error on
# a miss — never a silent literal pass-through, never silently reinterpreted as a field of ``in``.
# ``$$`` escapes a literal ``$``. The ``tool``/``mcp_tool``/``router`` nodes use it as a whole-value
# reference (``_resolve_ref``); the ``llm`` node uses it inline in ``messages`` content
# (``_render_template``). Both go through ``_resolve_in`` so a port resolves identically on both.


@dataclass(frozen=True)
class _PortInputs:
    """The named-in-port map a node resolves ``$in`` tokens against (m10.md §3). ``values`` holds
    the value on each *live, fed* in-port (keyed by port id); ``declared`` is every in-port the node
    declares — so an addressed-but-absent port resolves to ``None`` (an unfed optional port or a
    dead branch this run), while an addressed-but-**undeclared** port is a loud error."""

    values: Mapping[str, Any]
    declared: frozenset[str]


class _RefError(ValueError):
    """A ``$in``-reference couldn't be resolved: an unknown in-port, a bare ``$in`` on a node with
    no default ``in`` port, or a ``$in.<port>.<path>`` drill into a field that doesn't exist.
    Carries a full, user-facing message (the node, the token, the available ports/fields). Internal
    — the router maps it to :class:`RouterError`, a tool/mcp_tool maps it to an ``err`` binding, the
    llm template maps it to :class:`TemplateError`."""


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


def _resolve_in(parts: list[str], ports: _PortInputs, node_id: str) -> Any:
    """Resolve a ``$in`` token's segments **port-first** (m10.md §1.2). ``parts`` is everything
    after ``$in`` (``$in`` → ``[]``, ``$in.file`` → ``["file"]``, ``$in.in.body`` → ``["in",
    "body"]``). Bare ``$in`` is the default port ``in``; otherwise the first segment is a port
    name and the rest drills into that port's value. An undeclared port, or a bare ``$in`` with no
    ``in`` port, raises :class:`_RefError` with a precise message — never a silent ``None`` or
    literal.

    **Absent / optional ports (decision D1 — theygent-m10-decisions.md):** an in-port whose value
    is *absent* — declared but unfed (a ``required: false`` port, or one fed only by a dead branch),
    OR fed with an explicit ``null`` — resolves to the canonical **absent** value (``None``) for
    BOTH the whole value (``$in.<port>``) and any drill into it (``$in.<port>.<path>``). Drilling
    **short-circuits** to absent — it does *not* error on the missing path, because optional absence
    is author-sanctioned, not the silent-nonsense M10 kills (an *undeclared* port still errors
    above). ``absent`` is rendered per consumption site: ``""`` inline (``_stringify_token`` of
    ``None``), JSON ``null`` in structured args (``_resolve_ref`` returns ``None``). **Fed-with-null
    collapses to absent deliberately** (D1): a port producing ``null`` is indistinguishable from an
    unfed optional port. The resolver does not re-check requiredness — a *required* unfed port was
    rejected at validation (``graph.py``), so it trusts the load-time guarantee. A *present*
    (non-null) value drills per M9's field-path semantics, unchanged: a missing field on a present
    value is still a loud error."""

    if not parts:  # bare $in → the default in-port named ``in`` (M9 parity sugar)
        if "in" not in ports.declared:
            raise _RefError(
                f"node {node_id!r}: bare $in needs a default in-port named 'in', but this node's "
                f"in-ports are {sorted(ports.declared)} — address one as $in.<port>"
            )
        return ports.values.get("in")  # absent (None) if the default port is optional + unfed (D1)
    port = parts[0]
    if port not in ports.declared:
        hint = f"; did you mean $in.in.{port}?" if "in" in ports.declared else ""
        raise _RefError(
            f"node {node_id!r}: no in-port named {port!r}; in-ports: {sorted(ports.declared)}{hint}"
        )
    port_value = ports.values.get(port)
    # D1: an absent port value (unfed optional / dead branch, OR fed-with-null — they collapse)
    # resolves to absent for the whole value AND any drill. Never an error — that is the point of an
    # optional port. Only an UNDECLARED port (handled above) errors.
    if port_value is None:
        return None
    if not parts[1:]:  # $in.<port> — the whole, present value
        return port_value
    # A present, non-null value: drilling defers to M9's field-path semantics (a missing field on a
    # present value is still a loud error — M10 does not redefine drill-miss-on-a-present-value).
    try:
        return _resolve_path(port_value, parts[1:])
    except _RefError as exc:
        raise _RefError(
            f"node {node_id!r}: cannot resolve $in.{'.'.join(parts)}: no field "
            f"{exc.args[0]!r} in in-port {port!r}; {_available_fields(port_value)}"
        ) from exc


def _resolve_ref(ref: Any, ports: _PortInputs, node_id: str) -> Any:
    """Resolve one arg/select value against the node's in-port map (m10.md §1.2). ``$in`` is the
    default in-port's whole value; ``$in.<port>`` selects a named in-port; ``$in.<port>.<path>``
    drills into it (parsing JSON strings as it descends). Any non-``$in`` value (a literal string,
    dict, number) is returned unchanged — so an arg like ``{"value": {"handle": "yes"}}`` is a
    literal, not a reference. No expression language, no code execution (§7): just a port ref."""

    if not isinstance(ref, str):
        return ref
    if ref == "$in":
        return _resolve_in([], ports, node_id)
    if not ref.startswith("$in."):
        return ref
    return _resolve_in(ref[len("$in.") :].split("."), ports, node_id)


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


def _render_template(content: str, ports: _PortInputs, node_id: str) -> str:
    """Substitute ``$in`` / ``$in.<port>`` / ``$in.<port>.<field>`` tokens in one content string
    against the node's in-port map (m10.md §1.2). ``$$`` is a literal ``$``. An unknown root
    (anything but ``$in``, e.g. the removed ``$input``), an undeclared in-port, or an unresolvable
    drill raises :class:`TemplateError` naming the node, the token, and the available ports/fields —
    the F1.1-class fix extended to ports: no silent literal, no green run with nonsense
    (m10.md §1.2, §6)."""

    def _sub(match: re.Match[str]) -> str:
        if match.group(0) == "$$":
            return "$"
        expr = match.group(1)
        parts = expr.split(".")
        if parts[0] != "in":
            raise TemplateError(
                f"node {node_id!r}: unknown template token '${expr}' — the substitution token is "
                f"$in (the default in-port), $in.<port> (a named in-port), or $in.<port>.<field> "
                f"(a field of it); this node's in-ports are {sorted(ports.declared)}"
            )
        try:
            value = _resolve_in(parts[1:], ports, node_id)
        except _RefError as exc:
            raise TemplateError(str(exc)) from exc
        return _stringify_token(value)

    return _TEMPLATE_TOKEN.sub(_sub, content)


def _render_messages(node: Node, config: LlmConfig, ports: _PortInputs) -> list[dict[str, str]]:
    """Render every ``messages`` content field through the port-addressed ``$in`` template (m10.md
    §1.2). One llm node can now compose several upstreams in one prompt — ``$in.file`` AND
    ``$in.question`` — because the token addresses the node's named in-ports, not a single value."""

    return [
        {"role": msg.role, "content": _render_template(msg.content, ports, node.id)}
        for msg in config.messages
    ]


# ── runtime-agnostic activity executors (M13 §2) ─────────────────────────────
#
# The §8.7 compile contract: an ``activity`` node (``llm``/``tool``/``mcp_tool``) is a single
# **durable step** whose body is runtime-agnostic — the M5 walker handler logic, free of any
# runtime SDK (m13-dbos.md §2 / decisions D4). These three functions ARE that body: the durable
# compiler (``durable/compiler.py``) wraps them in ``@DBOS.step`` and the interactive M5 walker
# below delegates to them, so the two execution targets run the *same* activity code and
# walker/compiler parity is structural. The ``dbos`` import never reaches this module.
#
# Edges/ports are NOT passed in (they are walk state): the caller resolves ``$in`` args/messages
# deterministically (the in-port map is plain data) and hands the executor the resolved, fully
# serializable inputs + a value back. This is what lets a durable step checkpoint serializable
# I/O (m13-dbos.md §3 payload discipline).


def parse_chunk(chunk: Any) -> tuple[str | None, str | None, str | None]:
    """The one streaming-chunk parser shared by the interactive ``_walk_llm`` generator and the
    durable ``execute_llm`` body: ``(reasoning, content, finish_reason)`` for one upstream chunk
    (any may be ``None``). A reasoning model emits ``reasoning_content`` (thinking) before
    ``content`` (the answer); they are kept distinct everywhere (m9.md reasoning split)."""

    if not chunk.choices:
        return None, None, None
    choice = chunk.choices[0]
    finish_reason = choice.finish_reason
    delta = choice.delta
    if delta is None:
        return None, None, finish_reason
    return (
        getattr(delta, "reasoning_content", None),
        getattr(delta, "content", None),
        finish_reason,
    )


@dataclass(frozen=True)
class LlmActivityResult:
    """The serializable result of an ``llm`` activity (m13-dbos.md §2/§6): the assembled answer
    (the only thing journaled), the finish reason, and whether the model truncated to empty (spent
    its whole budget thinking — the m9.md §2.4 reason). Tokens are a non-durable side-channel that
    already streamed during execution; this return value is the durable record."""

    output: str
    finish_reason: str | None
    truncated_empty: bool


async def execute_llm(
    gateway: GatewayClient,
    *,
    model_id: str,
    params: dict[str, Any],
    messages: list[dict[str, str]],
    extra_headers: Mapping[str, str],
    on_delta: Any | None = None,
) -> LlmActivityResult:
    """Run one ``llm`` activity to completion, assembling the answer (m13-dbos.md §2). ``on_delta``
    (``(content: str, kind: str) -> None``, optional) receives each token as a **side effect** —
    the SSE relay / durable delta bus (§6) — while only ``content`` accumulates into the returned
    output. A pre-stream 503/404 raises from ``open_stream`` (the caller surfaces a clean status);
    a mid-stream inference death raises (→ failed run). The body is identical to the M5 walker's
    ``llm`` lowering; the durable step wraps THIS, so a journaled+replayed step is not re-executed
    and tokens are not regenerated/re-streamed (§6)."""

    upstream = await gateway.open_stream(
        model=model_id, messages=messages, params=params, extra_headers=extra_headers
    )
    output = ""
    finish_reason: str | None = None
    async for chunk in upstream:
        reasoning, content, fr = parse_chunk(chunk)
        if fr:
            finish_reason = fr
        if reasoning and on_delta is not None:
            on_delta(reasoning, "reasoning")
        if content:
            output += content
            if on_delta is not None:
                on_delta(content, "content")
    truncated_empty = finish_reason == "length" and not output.strip()
    return LlmActivityResult(
        output=output, finish_reason=finish_reason, truncated_empty=truncated_empty
    )


@dataclass(frozen=True)
class ActivityOutcome:
    """The serializable result of a ``tool`` / ``mcp_tool`` activity (m13-dbos.md §2): ``ok`` →
    the value binds to the success handle(s); ``not ok`` → ``value`` is an actionable error message
    bound to the ``err`` handle and the run continues (the m6.md §4 / m7.md §4 ok-err contract,
    here as a structured return rather than handle-mutation so a durable step can checkpoint it)."""

    ok: bool
    value: Any


async def execute_tool(
    tools: ToolRegistry, tool_name: str, args: dict[str, Any]
) -> ActivityOutcome:
    """Resolve ``tool_name`` against the registry and ``await`` it with the already-resolved
    ``args`` (m6.md §4). A raised exception is a *structured error*, not a run failure: it is
    returned as ``ActivityOutcome(ok=False, message)`` and the caller binds ``err``. (A non-200
    HTTP status is a normal return value bound to ``ok``; only transport failures raise.)"""

    try:
        fn = tools.get(tool_name)
        value = await fn(**args)
    except Exception as exc:  # tool failed: structured err, run continues (§4).
        return ActivityOutcome(ok=False, value=str(exc))
    return ActivityOutcome(ok=True, value=value)


async def execute_mcp_tool(
    mcp: McpManager, server: str, tool: str, args: dict[str, Any]
) -> ActivityOutcome:
    """Invoke an external MCP server's tool with already-resolved ``args`` (m7.md §4). The same
    ok/err contract as ``execute_tool``, only the transport differs. A tool-level error
    (``McpResult.is_error``), a transport failure after one reconnect-retry, or an unknown tool all
    return ``ok=False`` with an **actionable** message naming the *server* and the *kind* of failure
    (connection vs the server's own tool error vs unknown tool) so the agent reading ``err`` can
    decide its next move. Goes through the ``invoke_mcp_tool`` governance chokepoint (m7.md §3)."""

    try:
        result = await _invoke_mcp(mcp, server, tool, args)
        if result.is_error:
            return ActivityOutcome(
                ok=False,
                value=f"mcp server {server!r} tool {tool!r} error: {_stringify(result.value)}",
            )
        return ActivityOutcome(ok=True, value=result.value)
    except McpConnectionError as exc:  # unreachable even after the reconnect-retry
        return ActivityOutcome(
            ok=False, value=f"mcp server {server!r} unavailable (connection failed): {exc}"
        )
    except McpToolNotFound as exc:  # the connected server doesn't expose this tool (already named)
        return ActivityOutcome(ok=False, value=str(exc))
    except Exception as exc:  # a bad arg or other invocation failure
        return ActivityOutcome(ok=False, value=f"mcp server {server!r} tool {tool!r} failed: {exc}")


# ── conditional dataflow: edge liveness, node skipping (m6.md §4) ─────────────


def _success_handles(node: Node) -> set[str]:
    """The node's non-error out-handles — where a normal result is bound (input/llm/tool-ok)."""
    return {p.id for p in node.ports.out if p.type != "error"}


def _error_handles(node: Node) -> set[str]:
    """The node's ``error``-typed out-handles — where a tool's structured error is bound."""
    return {p.id for p in node.ports.out if p.type == "error"}


def _bind_outcome(
    node: Node,
    outcome: ActivityOutcome,
    values: dict[tuple[str, str], Any],
    live_handles: dict[str, set[str]],
) -> None:
    """Bind a ``tool``/``mcp_tool`` :class:`ActivityOutcome` to the node's handles (m6.md §4): the
    success value to the non-error handles on ``ok``, the error message to the ``err`` handle(s)
    otherwise — activating exactly the taken handle set so the un-taken branch's edges are dead.
    Shared by the interactive walker and (conceptually) the durable compiler's value threading."""

    handles = _success_handles(node) if outcome.ok else _error_handles(node)
    live_handles[node.id] = handles
    for handle in handles:
        values[(node.id, handle)] = outcome.value


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


def _collect_in_ports(
    node: Node,
    edges: list[Edge],
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> _PortInputs:
    """Gather a node's in-port map: every *live* inbound ``data`` edge keyed by its
    ``targetHandle`` (m10.md §3 in-port collection). This is the only M10 generalisation of the
    walker — collecting **several** named producers instead of assuming one. Dead edges (an
    un-taken branch this run) are ignored, so a port fed only by a dead branch is absent (resolves
    to ``None``). Validation already rejected two ``data`` edges to one port, so no override
    ambiguity. ``declared`` carries every in-port the node declares, so ``$in.<port>`` can tell an
    undeclared port (loud error) from a declared-but-absent one (``None``)."""

    collected: dict[str, Any] = {}
    for edge in edges:
        if (
            edge.target == node.id
            and edge.channel == "data"
            and _edge_live(edge, skipped, live_handles)
        ):
            collected[edge.target_handle] = values.get((edge.source, edge.source_handle))
    return _PortInputs(values=collected, declared=frozenset(p.id for p in node.ports.in_))


def _single_in_value(ports: _PortInputs, node: Node) -> Any:
    """The one value a **single-value consumer** takes: the run output (``output`` boundary) or the
    value a ``router`` forwards along its chosen branch. These nodes consume ONE upstream value, so
    they must declare exactly one in-port. More than one is an ambiguous binding — *which* port is
    the output / the forwarded value? — and is a **loud** error (m10.md §1.3), the same
    no-silent-nonsense rule as the unknown-port path, never a silent pick of the default port.

    Multi-input (``$in.<port>``) is for the value-**producing** nodes — ``llm``/``tool``/
    ``mcp_tool`` — which READ several ports into their own output; ``output``/``router`` consume a
    single value. (A ``router`` that needs to branch on one of several inputs is the deferred
    multi-port-router fork, not a silent default-port pick — m10.md §1.3/§4.)"""

    declared = node.ports.in_
    if len(declared) > 1:
        raise TemplateError(
            f"node {node.id!r}: a single-value consumer ({node.type}) takes exactly one in-port "
            f"value, but declares {len(declared)} in-ports "
            f"{sorted(p.id for p in declared)} — wire exactly one, or compose multiple upstreams "
            f"in a value-producing node (llm/tool/mcp_tool) via $in.<port>"
        )
    return ports.values.get(declared[0].id) if declared else None


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

    if result is not None and result.empty_reason is None:
        result.empty_reason = finalize_empty_reason(
            ir,
            output=result.output,
            output_produced=result.output_produced,
            truncated_empty_nodes=result.truncated_empty_nodes,
            skipped=skipped,
            live_handles=live_handles,
        )


def finalize_empty_reason(
    ir: IRDocument,
    *,
    output: Any,
    output_produced: bool,
    truncated_empty_nodes: list[str],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> str | None:
    """The m9.md §2.4 honest-empty-output reason, single-sourced so the interactive walk AND the
    durable compiler (M13) compute it identically. If the walk finished with NO output node having
    run and some node short-circuited to its ``err`` handle, the run reached no output *because* of
    that handled-but-unconsumed error (its ``ok`` path to the output was skipped) — name it so the
    control-plane doesn't report a green ``completed`` masquerading as success. If the output node
    ran but is blank because an ``llm`` spent its whole budget (a reasoning model thinking, or
    ``maxTokens`` too low), name that instead of a green blank. Returns ``None`` when the output is
    legitimately present. Does NOT change the M6 error-as-structured-output contract."""

    if not output_produced:
        errored = [
            node.id
            for node in ir.nodes
            if node.id not in skipped
            and live_handles.get(node.id)
            and live_handles[node.id] <= _error_handles(node)
        ]
        if errored:
            return f"output empty: upstream error on node {errored[0]!r}"
        return None
    if _is_blank(output) and truncated_empty_nodes:
        return (
            f"output empty: node {truncated_empty_nodes[0]!r} hit the token limit "
            "(finish_reason=length) before producing content — raise maxTokens (a reasoning "
            "model can spend the whole budget thinking)"
        )
    return None


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
        ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
        value = _single_in_value(ports, node)
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
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # Naive full replay (M4 §4): prior thread turns verbatim, then this turn's rendered prompt. The
    # prompt may compose several in-ports (M10) — $in.file AND $in.question — via the port map.
    messages = [*ctx.prior_messages, *_render_messages(node, config, ports)]

    # open_stream sends the request and validates status, so a pre-stream 503/404 raises here —
    # before any delta — letting the control-plane surface a clean status (mirrors /runs).
    upstream = await ctx.gateway.open_stream(
        model=model_id, messages=messages, params=params, extra_headers=ctx.extra_headers
    )
    output = ""
    finish_reason: str | None = None
    async for chunk in upstream:
        # `parse_chunk` is the one chunk parser shared with the durable `execute_llm` body (M13 §2):
        # a reasoning model emits `reasoning_content` (its thinking) before `content` (its answer).
        # Stream the thinking as visible progress so the model doesn't look frozen, but NEVER fold
        # it into the output — only `content` is the answer (the binding downstream nodes read).
        reasoning, content, fr = parse_chunk(chunk)
        if fr:
            finish_reason = fr
        if reasoning:
            yield Delta(node_id=node.id, content=reasoning, kind="reasoning")
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
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # Resolve args deterministically, then run the shared ``execute_tool`` activity body (M13 §2 —
    # the same body the durable step wraps). An unresolvable ``$in`` arg ref is a structured err,
    # exactly as a raised tool error is (m6.md §4): bind ``err``, continue.
    try:
        args = {k: _resolve_ref(v, ports, node.id) for k, v in config.args.items()}
    except Exception as exc:
        outcome = ActivityOutcome(ok=False, value=str(exc))
    else:
        outcome = await execute_tool(ctx.tools, config.tool, args)
    _bind_outcome(node, outcome, values, live_handles)
    if not outcome.ok:
        logger.info(
            "graph.tool_error",
            extra={
                "run_id": ctx.run_id,
                "node_id": node.id,
                "tool": config.tool,
                "error": outcome.value,
            },
        )


async def _invoke_mcp(mcp: McpManager, server: str, tool: str, args: dict[str, Any]) -> McpResult:
    """THE single chokepoint every ``mcp_tool`` invocation passes through (m7.md §3 governance
    note). This is the future governance attach-point — policy-as-code and audit logging hook in
    HERE, observing the *invocation* (which server, which tool, the arg shape), never the contents
    the server returns (the §10 redaction posture). M7 records the seam; the L2 policy layer is a
    separate, evidence-driven milestone. For now it simply forwards to the MCP manager. Both the
    interactive walker (via ``invoke_mcp_tool``) and the durable step (via ``execute_mcp_tool``)
    pass through this one function, so governance lands once for both runtimes (M13 D4)."""

    return await mcp.call_tool(server, tool, args)


async def invoke_mcp_tool(
    server: str, tool: str, args: dict[str, Any], ctx: WalkContext
) -> McpResult:
    """The context-bound MCP chokepoint the interactive walker uses — delegates to ``_invoke_mcp``
    (the governance attach-point), reading the manager off the ``WalkContext``."""

    return await _invoke_mcp(ctx.mcp, server, tool, args)


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
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # Resolve args deterministically, then run the shared ``execute_mcp_tool`` activity body (M13
    # §2 — the same body the durable step wraps; it classifies connection/tool-not-found/tool-error
    # into actionable ``err`` messages). A bad arg ref is itself a structured err (m7.md §4).
    try:
        args = {k: _resolve_ref(v, ports, node.id) for k, v in config.args.items()}
    except Exception as exc:
        outcome = ActivityOutcome(
            ok=False, value=f"mcp server {server!r} tool {tool!r} failed: {exc}"
        )
    else:
        outcome = await execute_mcp_tool(ctx.mcp, server, tool, args)
    _bind_outcome(node, outcome, values, live_handles)
    if not outcome.ok:
        logger.info(
            "graph.mcp_error",
            extra={
                "run_id": ctx.run_id,
                "node_id": node.id,
                "server": server,
                "tool": tool,
                "error": outcome.value,
            },
        )


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
