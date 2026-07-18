"""The IR walker — the IR↔runtime seam: walk an :class:`~theygent_ir.IRDocument` node by node and
execute it against the run/session spine.

**This is an interpreter, not a compiler.** The walker lowers each node to in-process Python;
the durable compiler re-targets the same IR to the durable runtime.
The seam — IR in, a stream of deltas out — is identical either way; that is the entire point of
the determinism split. So the walker imports no durable-runtime SDK, wraps no node in
retries/checkpoints, and persists no per-node intermediate state. "Durability" is exactly
``/runs`` today: the ``Run`` row records the final outcome.

Two seam rules it holds:

* **Dispatch is by ``kind``** (``boundary``/``activity``/``orchestration``), then by ``type``
  *within* a kind — so adding a node type is an additive handler, and the durable compiler can lower
  the same IR to a durable target with the same dispatch.
* **No DB calls inside the walker.** It is a pure async function over a :class:`WalkContext`: the
  control-plane loads session memory and persists the ``Run`` through the same ``/runs``
  seams, then hands the walker the prior messages, the gateway client, and the tool registry.

The ``router`` (an orchestration node — handle-name branching) introduces **conditional
execution**: it selects one outgoing handle and a ``tool`` takes its ``ok`` or ``err`` handle, so
edges from the un-taken handle(s) are not traversed and downstream nodes with no live inbound
``data`` edge are *skipped* (logged, not executed).

A node may declare **more than one** named in-port, each fed by a distinct upstream ``data`` edge,
addressed in templates as ``$in.<port>`` (``$in`` stays sugar for the default port ``in``).
In-port *collection* (``_collect_in_ports`` gathers the named producers) and *resolution*
(``_resolve_in`` is port-first) are the only multi-input machinery; dispatch-by-``kind`` and the
determinism boundary are untouched by it — multi-input is binding + addressing, not a new
``kind``, node type, or I/O.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from theygent_gateway_client import GatewayClient
from theygent_ir import (
    BuiltinTool,
    Edge,
    GuardrailConfig,
    GuardrailRule,
    HttpTool,
    ImagineConfig,
    IRDocument,
    LlmConfig,
    McpTool,
    McpToolConfig,
    Node,
    RagConfig,
    RouterConfig,
    SpeakConfig,
    ToolConfig,
    TranscribeConfig,
    TransformConfig,
    topological_order,
)

from theygent_control_plane.mcp import (
    McpConnectionError,
    McpManager,
    McpResult,
    McpServerConfig,
    McpToolNotFound,
)
from theygent_control_plane.observability import RunTrace, SpanScope, now_ns
from theygent_control_plane.reasoning import ThinkSplitter
from theygent_control_plane.tools import DEFAULT_REGISTRY, ToolRegistry

logger = logging.getLogger("theygent.control_plane.walker")

# The managed-engine names. A resolved model id that is an engine name is not a logical id —
# the inference seam invariant: the data plane accepts only logical model ids. Mirrors
# the same constant in ``app.py`` for the ``/runs`` path; both guard the one seam rule.
_ENGINE_NAMES = frozenset({"mlx", "vllm", "llamacpp"})


class EngineNameNotAllowed(ValueError):
    """A node resolved its model to an engine name, not a logical id. Raised at
    resolution time so the control-plane rejects it up front — before a ``Run`` exists and
    before anything reaches the wire — exactly as ``/runs`` rejects an engine name today."""


class RouterError(RuntimeError):
    """A ``router`` node could not select an outgoing handle at runtime: the upstream
    output had no ``handle`` field, or named a handle the router doesn't declare. No fallback
    guessing — the control-plane maps it to a failed run with a clear reason."""


class TemplateError(ValueError):
    """A substitutable field referenced a ``$``-token that couldn't be resolved: an
    unknown token (anything not rooted at ``$in``, e.g. a typo like ``$input`` or ``$nope``)
    or a ``$in.field`` path that doesn't exist in the in-port value. This fails LOUDLY — naming the
    node, the token, and the available fields — instead of passing through as literal text and
    producing a green run with nonsense output. The control-plane maps it to a failed run with a
    clear reason, exactly like :class:`RouterError`."""


class TransformError(ValueError):
    """A ``transform`` node could not reshape its input: its ``expr`` is not valid
    JSON, or a ``$in`` ref in the template doesn't resolve. Like :class:`RouterError` /
    :class:`TemplateError` this is a LOUD failure (→ failed run), never a silent empty reshape —
    the no-silent-nonsense rule applied to the reshaper. (``transform`` has no ``err`` port; a bad
    reshape fails the run, it is not a structured tool error.)"""


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
    bound from the executed ``output`` boundary node's in-port. The canonical output is the output
    node's value, not the accumulated deltas — a ``tool`` result that never streamed is still a
    valid output. The control-plane reads it after the walk to populate the non-stream response and
    the session's assistant turn.

    ``output_produced`` is True iff an ``output`` boundary node actually executed (vs. being skipped
    because its inbound branch was dead) — it disambiguates a legitimately ``None`` output from "no
    output node ran at all". ``empty_reason`` is set when NO output was produced AND an upstream
    node short-circuited to its ``err`` handle: the run reached no output because of a
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
    """Everything the walker needs that it must not fetch itself. ``prior_messages`` is the
    session replay the control-plane already loaded; ``extra_headers`` carries the
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
    # The connection resolver an http tool / connection-backed mcp_tool resolves auth through,
    # server-side at step time. ``None`` when no graph uses a connection (the control-plane injects
    # its DB-backed resolver). The walker never opens a DB session — the resolver owns it.
    tool_auth: ConnectionResolver | None = None
    # The gate backend (``ratelimit`` counter + ``quota`` usage-read). Injected by the control-plane
    # (it owns the DB); ``None`` when no graph uses a gate. Same injection discipline as
    # ``tool_auth``/``run_trace`` — the walker calls it, the backend owns the session.
    gates: Any = None  # gates.GateBackend
    # The artifact store ``transcribe``/``speak`` resolve audio references through (fetch the input
    # audio bytes / store the produced audio). Injected; ``None`` when no audio node runs.
    artifacts: Any = None  # artifacts.LocalArtifactStore
    # The retrieval backend a ``rag`` node queries (embeds over the gateway, hybrid-searches the
    # control-plane store). Injected; ``None`` when no graph retrieves — same discipline as
    # ``tool_auth``/``gates``: the walker calls it, the backend owns its sessions.
    rag: Any = None  # rag.RagRetriever
    # The observability handle for this run (the control-plane opens it before walking). When set,
    # the walker wraps each node in a span and captures per-node I/O through it (the one-wrapper
    # seam); when ``None`` (telemetry not wired) the walk is purely additive — the walker still does
    # no run-state/session DB I/O itself; telemetry is an injected side-channel.
    run_trace: RunTrace | None = None


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _lower_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Lower IR generation params (camelCase, e.g. ``maxTokens``) to the OpenAI-native
    snake_case the data plane speaks (``max_tokens``). This is part of the walker's IR→execution
    lowering, not a contract change: the IR stays camelCase (consistent with the IR envelope),
    while the inference seam stays OpenAI-compatible. A generic camel→snake conversion handles
    every generation param (``topP``→``top_p``, ``responseFormat``→``response_format``, …);
    single-word params (``temperature``, ``stop``, ``seed``) are unchanged."""

    return {_CAMEL_BOUNDARY.sub("_", k).lower(): v for k, v in params.items()}


def resolve_model_key(ir: IRDocument, model_key: str) -> tuple[str, dict[str, Any]]:
    """Resolve a ``models`` key to the (logical id, generation params) forwarded to the inference
    seam. Raises :class:`EngineNameNotAllowed` if the binding's ``model`` is an engine name —
    the logical-id invariant, enforced before anything is sent. Shared by ``llm``
    (``resolve_model``), ``transcribe`` / ``speak``, and the model ``guardrail``."""
    binding = ir.models[model_key]
    if binding.model in _ENGINE_NAMES:
        raise EngineNameNotAllowed(
            f"{binding.model!r} is an engine name, not a logical model id; "
            "the model binding must carry a logical id"
        )
    return binding.model, _lower_params(binding.params)


def resolve_model(ir: IRDocument, config: LlmConfig) -> tuple[str, dict[str, Any]]:
    """Resolve an ``llm`` node's bound model — a thin wrapper over :func:`resolve_model_key`."""
    return resolve_model_key(ir, config.model)


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


def audio_model_nodes(ir: IRDocument) -> list[tuple[Node, str, dict[str, Any]]]:
    """Every media model-call node (``transcribe``/``speak``/``imagine``) with its resolved (model
    id, params) — the control-plane rejects an engine-name binding before the ``Run`` (the
    logical-id-only guard, extended to the non-chat modalities). Resolution raises
    :class:`EngineNameNotAllowed`, mirroring ``llm_models``."""
    out: list[tuple[Node, str, dict[str, Any]]] = []
    for node in ir.nodes:
        if node.type == "transcribe":
            cfg: Any = TranscribeConfig.model_validate(node.config)
        elif node.type == "speak":
            cfg = SpeakConfig.model_validate(node.config)
        elif node.type == "imagine":
            cfg = ImagineConfig.model_validate(node.config)
        else:
            continue
        model_id, params = resolve_model_key(ir, cfg.model)
        out.append((node, model_id, params))
    return out


def tool_nodes(ir: IRDocument) -> list[tuple[Node, str]]:
    """Every ``tool`` node with its referenced tool name, in document order. The control-plane
    checks each name against the registry up front — before creating the ``Run`` → 400
    ``tool_not_found`` — so an unknown tool never reaches the walker. The IR package
    stays pure (validates ``config`` *shape*, not registry membership)."""

    return [
        (node, ToolConfig.model_validate(node.config).tool)
        for node in ir.nodes
        if node.type == "tool"
    ]


def mcp_tool_nodes(ir: IRDocument) -> list[tuple[Node, str, str]]:
    """Every ``server``-based ``mcp_tool`` node with its (server name, tool name) in document order.
    The control-plane checks server membership up front (→ 400 ``mcp_server_not_found``, no Run)
    and — if that server is already connected — the tool against its cached capability list.
    Connection-based ``mcp_tool`` nodes are SKIPPED here — like the http tool, the connection
    resolves at step time (a dangling connection binds ``err``, not a 400)."""

    out: list[tuple[Node, str, str]] = []
    for node in ir.nodes:
        if node.type == "mcp_tool":
            cfg = McpToolConfig.model_validate(node.config)
            if cfg.server:
                out.append((node, cfg.server, cfg.tool))
    return out


# ── value references + inline templating: $in / $in.<port> / $in.<port>.<path> ──
#
# ONE token language across every substituting node, addressed **port-first**. A node declares its
# in-ports (``ports.in``); each is fed by one ``data`` edge. The token selects a port by name:
#   ``$in``                  → the default in-port named ``in`` (sugar for the single-port case)
#   ``$in.<port>``           → the whole value on that named in-port
#   ``$in.<port>.<path>``    → drill into that port's value (field-drilling lives here now)
# The first segment after ``$in.`` is ALWAYS a port name, resolved port-first with a LOUD error on
# a miss — never a silent literal pass-through, never silently reinterpreted as a field of ``in``.
# ``$$`` escapes a literal ``$``. The ``tool``/``mcp_tool``/``router`` nodes use it as a whole-value
# reference (``_resolve_ref``); the ``llm`` node uses it inline in ``messages`` content
# (``_render_template``). Both go through ``_resolve_in`` so a port resolves identically on both.


@dataclass(frozen=True)
class _PortInputs:
    """The named-in-port map a node resolves ``$in`` tokens against. ``values`` holds the value on
    each *live, fed* in-port (keyed by port id); ``declared`` is every in-port the node declares —
    so an addressed-but-absent port resolves to ``None`` (an unfed optional port or a dead branch
    this run), while an addressed-but-**undeclared** port is a loud error."""

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
    the inline ``_render_template`` so the two surfaces resolve ``$in`` identically."""

    cur = input_value
    for key in parts:
        cur = _parse_if_json(cur)
        if not isinstance(cur, dict) or key not in cur:
            raise _RefError(key)
        cur = cur[key]
    return cur


def _resolve_in(parts: list[str], ports: _PortInputs, node_id: str) -> Any:
    """Resolve a ``$in`` token's segments **port-first**. ``parts`` is everything after ``$in``
    (``$in`` → ``[]``, ``$in.file`` → ``["file"]``, ``$in.in.body`` → ``["in", "body"]``). Bare
    ``$in`` is the default port ``in``; otherwise the first segment is a port name and the rest
    drills into that port's value. An undeclared port, or a bare ``$in`` with no ``in`` port, raises
    :class:`_RefError` with a precise message — never a silent ``None`` or literal.

    **Absent / optional ports:** an in-port whose value is *absent* — declared but unfed (a
    ``required: false`` port, or one fed only by a dead branch), OR fed with an explicit ``null`` —
    resolves to the canonical **absent** value (``None``) for BOTH the whole value
    (``$in.<port>``) and any drill into it (``$in.<port>.<path>``). Drilling **short-circuits** to
    absent — it does *not* error on the missing path, because optional absence is author-sanctioned
    (an *undeclared* port still errors above). ``absent`` is rendered per consumption site: ``""``
    inline (``_stringify_token`` of ``None``), JSON ``null`` in structured args (``_resolve_ref``
    returns ``None``). **Fed-with-null collapses to absent**: a port producing ``null``
    is indistinguishable from an unfed optional port. The resolver does not re-check requiredness —
    a *required* unfed port was rejected at validation (``graph.py``), so it trusts the load-time
    guarantee. A *present* (non-null) value drills by field-path semantics, unchanged: a missing
    field on a present value is still a loud error."""

    if not parts:  # bare $in → the default in-port named ``in`` (sugar for the single-port case)
        if "in" not in ports.declared:
            raise _RefError(
                f"node {node_id!r}: bare $in needs a default in-port named 'in', but this node's "
                f"in-ports are {sorted(ports.declared)} — address one as $in.<port>"
            )
        return ports.values.get("in")  # absent (None) if the default port is optional + unfed
    port = parts[0]
    if port not in ports.declared:
        hint = f"; did you mean $in.in.{port}?" if "in" in ports.declared else ""
        raise _RefError(
            f"node {node_id!r}: no in-port named {port!r}; in-ports: {sorted(ports.declared)}{hint}"
        )
    port_value = ports.values.get(port)
    # An absent port value (unfed optional / dead branch, OR fed-with-null — they collapse)
    # resolves to absent for the whole value AND any drill. Never an error — that is the point of an
    # optional port. Only an UNDECLARED port (handled above) errors.
    if port_value is None:
        return None
    if not parts[1:]:  # $in.<port> — the whole, present value
        return port_value
    # A present, non-null value: drilling uses field-path semantics (a missing field on a present
    # value is still a loud error).
    try:
        return _resolve_path(port_value, parts[1:])
    except _RefError as exc:
        raise _RefError(
            f"node {node_id!r}: cannot resolve $in.{'.'.join(parts)}: no field "
            f"{exc.args[0]!r} in in-port {port!r}; {_available_fields(port_value)}"
        ) from exc


def _resolve_ref(ref: Any, ports: _PortInputs, node_id: str) -> Any:
    """Resolve one arg/select value against the node's in-port map. ``$in`` is the default in-port's
    whole value; ``$in.<port>`` selects a named in-port; ``$in.<port>.<path>`` drills into it
    (parsing JSON strings as it descends). Any non-``$in`` value (a literal string, dict, number)
    is returned unchanged — so an arg like ``{"value": {"handle": "yes"}}`` is a literal, not a
    reference. No expression language, no code execution: just a port ref."""

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
    """Render a resolved token value into the surrounding text: ``None`` is empty, a string is
    itself, anything else is JSON (so ``$in.obj`` is legible)."""

    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value)


def _render_template(content: str, ports: _PortInputs, node_id: str) -> str:
    """Substitute ``$in`` / ``$in.<port>`` / ``$in.<port>.<field>`` tokens in one content string
    against the node's in-port map. ``$$`` is a literal ``$``. An unknown root (anything but
    ``$in``, e.g. ``$input``), an undeclared in-port, or an unresolvable drill raises
    :class:`TemplateError` naming the node, the token, and the available ports/fields — no silent
    literal, no green run with nonsense."""

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


def _render_content(content: Any, ports: _PortInputs, node_id: str) -> Any:
    """Render one ``messages`` ``content`` against the port map. A plain string is rendered inline
    through the ``$in`` template. A list is the multimodal form: each ``text`` part's ``text`` and
    each ``image_url`` part's ``url`` are ``$in``-templatable, so the image can come from a graph
    in-port (``$in.image``). The parts are emitted as plain OpenAI-shaped dicts (gateway/LiteLLM
    forward them verbatim); ``detail`` is included only when set, so a clean OpenAI ``image_url``
    reaches the wire (no ``detail: null`` for a server)."""
    if isinstance(content, str):
        return _render_template(content, ports, node_id)
    rendered: list[dict[str, Any]] = []
    for part in content:
        if part.type == "text":
            rendered.append({"type": "text", "text": _render_template(part.text, ports, node_id)})
        else:  # image_url part (the discriminated union has only these two members)
            image_url: dict[str, Any] = {
                "url": _render_template(part.image_url.url, ports, node_id)
            }
            if part.image_url.detail is not None:
                image_url["detail"] = part.image_url.detail
            rendered.append({"type": "image_url", "image_url": image_url})
    return rendered


def _render_messages(node: Node, config: LlmConfig, ports: _PortInputs) -> list[dict[str, Any]]:
    """Render every ``messages`` content field through the port-addressed ``$in`` template. One llm
    node can compose several upstreams in one prompt — ``$in.file`` AND ``$in.question`` — because
    the token addresses the node's named in-ports, not a single value. A ``content`` may also be a
    list of multimodal parts (text + image_url) — see ``_render_content`` — so a graph drives a
    vision model; the template applies in each part."""

    return [
        {"role": msg.role, "content": _render_content(msg.content, ports, node.id)}
        for msg in config.messages
    ]


# ── runtime-agnostic activity executors ──────────────────────────────────────
#
# An ``activity`` node (``llm``/``tool``/``mcp_tool``) is a single **durable step** whose body is
# runtime-agnostic — free of any runtime SDK. These functions ARE that body: the durable compiler
# (``durable/compiler.py``) wraps them in ``@DBOS.step`` and the interactive walker below delegates
# to them, so the two execution targets run the *same* activity code and walker/compiler parity is
# structural. The ``dbos`` import never reaches this module.
#
# Edges/ports are NOT passed in (they are walk state): the caller resolves ``$in`` args/messages
# deterministically (the in-port map is plain data) and hands the executor the resolved, fully
# serializable inputs + a value back. This lets a durable step checkpoint serializable I/O.


@dataclass(frozen=True)
class ToolCall:
    """One assembled tool call the model emitted — the OpenAI ``function`` shape after the streamed
    fragments are merged. ``name`` is a key in ``ir.tools`` (or a directly-registered builtin);
    ``arguments`` is the parsed function arguments — the ``$in`` substitution dict for an http tool,
    the call args for a builtin/mcp tool."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChunkDelta:
    """One parsed streaming chunk. The reasoning/content split is extended with ``tool_calls`` —
    the raw per-chunk ``delta.tool_calls`` fragment list (each {index, id?, function:{name?,
    arguments?}}). A non-tool chunk has ``tool_calls=None``.
    ``usage`` is the normalized token accounting a stream's final chunk carries when the request
    asked for it (``stream_options.include_usage``); ``None`` on every other chunk."""

    reasoning: str | None
    content: str | None
    finish_reason: str | None
    tool_calls: list[Any] | None = None
    usage: dict[str, int] | None = None


def read_usage(response: Any) -> dict[str, int] | None:
    """Normalize a chunk's (or a completion's) ``usage`` object to plain int fields. Two real
    wire shapes for the usage chunk: engines emit it with ``choices: []``, while a proxying
    router re-emits it with a non-empty choices list whose delta is all-null — so usage is read
    off the top-level object, never gated on the choices shape. A missing ``total_tokens`` is
    derived from the two parts when both are present; a server that reports nothing → ``None``
    (recorded as "not reported", never faked)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    out: dict[str, int] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, (int, float)):
            out[key] = int(value)
    if "total_tokens" not in out and "prompt_tokens" in out and "completion_tokens" in out:
        out["total_tokens"] = out["prompt_tokens"] + out["completion_tokens"]
    return out or None


def merge_usage(
    acc: dict[str, int] | None, turn: Mapping[str, int] | None
) -> dict[str, int] | None:
    """Sum one model turn's token usage into the accumulator — a multi-turn tool loop is one llm
    node, so its span carries the node's TOTAL usage across turns. ``None`` in, ``acc`` out (a
    turn without usage never zeroes what earlier turns reported)."""
    if not turn:
        return acc
    merged = dict(acc or {})
    for key, value in turn.items():
        merged[key] = merged.get(key, 0) + int(value)
    return merged


def usage_attributes(usage: Mapping[str, int] | None) -> dict[str, Any]:
    """GenAI-semconv span attributes for accumulated token usage. ``total_tokens`` is the key the
    quota gate sums over spans (read, not re-metered); input/output are the semconv splits the
    waterfall reads (tok/s). Empty when the server reported nothing — an absent attribute is an
    honest 'unreported', a zero would be a fake measurement."""
    if not usage:
        return {}
    attrs: dict[str, Any] = {}
    if "prompt_tokens" in usage:
        attrs["gen_ai.usage.input_tokens"] = usage["prompt_tokens"]
    if "completion_tokens" in usage:
        attrs["gen_ai.usage.output_tokens"] = usage["completion_tokens"]
    if "total_tokens" in usage:
        attrs["gen_ai.usage.total_tokens"] = usage["total_tokens"]
    return attrs


def parse_chunk(chunk: Any) -> ChunkDelta:
    """The one streaming-chunk parser the ``execute_llm`` body uses (and, via it, both runtimes):
    a reasoning model emits ``reasoning_content`` (thinking) before ``content`` (the answer), kept
    distinct (reasoning and answer are streamed separately); ``tool_calls`` are also surfaced for
    the tool loop. Token usage is read before the choices guard — the usage chunk's choices list is
    empty (or all-null through a proxy), so gating on choices would drop it."""

    usage = read_usage(chunk)
    if not chunk.choices:
        return ChunkDelta(None, None, None, usage=usage)
    choice = chunk.choices[0]
    finish_reason = choice.finish_reason
    delta = choice.delta
    if delta is None:
        return ChunkDelta(None, None, finish_reason, usage=usage)
    return ChunkDelta(
        reasoning=getattr(delta, "reasoning_content", None),
        content=getattr(delta, "content", None),
        finish_reason=finish_reason,
        tool_calls=getattr(delta, "tool_calls", None),
        usage=usage,
    )


def _accumulate_tool_calls(acc: dict[int, dict[str, Any]], fragments: list[Any]) -> None:
    """Merge a chunk's streamed ``tool_calls`` fragments into ``acc`` keyed by index: the id +
    function name arrive once, the JSON ``arguments`` string streams in pieces and is concatenated.
    Tolerates BOTH the streamed-fragment shape AND a whole tool_calls list on a terminal message
    (then each 'fragment' already carries the full call) — [[prefer-what-real-servers-return]]."""

    for frag in fragments or []:
        idx = getattr(frag, "index", None)
        if idx is None:
            idx = len(acc)  # a terminal whole-list shape may omit index → append in arrival order
        slot = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
        fid = getattr(frag, "id", None)
        if fid:
            slot["id"] = fid
        fn = getattr(frag, "function", None)
        if fn is not None:
            name = getattr(fn, "name", None)
            if name:
                slot["name"] = name
            args = getattr(fn, "arguments", None)
            if args:
                slot["arguments"] += args


def _finalize_tool_calls(acc: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """Turn the merged fragment map into ``ToolCall``s, parsing each function ``arguments`` JSON
    string into a dict (an empty/unparseable arguments → ``{}``, so a no-arg tool still calls)."""

    calls: list[ToolCall] = []
    for idx, slot in sorted(acc.items()):
        if not slot["name"]:
            continue  # a fragment with no resolved name is not a usable call
        try:
            parsed = json.loads(slot["arguments"]) if slot["arguments"].strip() else {}
        except (ValueError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        calls.append(ToolCall(id=slot["id"] or f"call_{idx}", name=slot["name"], arguments=parsed))
    return calls


@dataclass(frozen=True)
class LlmActivityResult:
    """The serializable result of an ``llm`` activity: the assembled answer (the only thing
    journaled), the finish reason, whether the model truncated to empty, and any ``tool_calls`` the
    model emitted this turn (empty when none). Tokens are a non-durable side-channel that already
    streamed; this return value is the durable record, so a replayed step re-derives the same answer
    + tool calls without re-executing."""

    output: str
    finish_reason: str | None
    truncated_empty: bool
    tool_calls: tuple[ToolCall, ...] = ()
    # This turn's token usage as the server reported it ({prompt,completion,total}_tokens), or
    # None when the upstream didn't report any — the caller aggregates turns onto the node span.
    usage: dict[str, int] | None = None
    # The think-stripped reasoning the model produced this turn (the separate delta field and/or
    # the inline think block), or None when there was none — so callers can persist the thinking
    # into the node's captured outputs after the live stream is gone. Never part of ``output``.
    reasoning: str | None = None


async def execute_llm(
    gateway: GatewayClient,
    *,
    model_id: str,
    params: dict[str, Any],
    messages: list[dict[str, Any]],
    extra_headers: Mapping[str, str],
    on_delta: Any | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: Any = None,
) -> LlmActivityResult:
    """Run ONE ``llm`` model turn to completion, assembling the answer + any tool calls.
    ``on_delta`` (``(piece, kind)``) streams tokens as a side effect (the SSE relay / durable delta
    bus) while only ``content`` accumulates into the output. ``tools`` / ``tool_choice`` offer the
    model functions to call; streamed ``tool_calls`` fragments are merged into the returned
    ``tool_calls``. This is ONE turn — it does NOT loop; the loop (execute tools → feed results back
    → call again) lives in the caller (the walker / durable compiler) so each turn stays a
    journalable step. A pre-stream 503/404 raises from ``open_stream``; a mid-stream death raises
    (→ failed run).

    Reasoning is split engine-agnostically: a separate ``reasoning_content`` delta field is
    forwarded as-is, and inline ``<think>…</think>`` spans left in ``delta.content`` by
    raw-template servers are routed through a :class:`~reasoning.ThinkSplitter` — so the
    thinking reaches ``on_delta`` as ``"reasoning"`` either way and NEVER accumulates into
    the returned output (see ``reasoning.py`` for the engine variance). It IS accumulated onto
    the result's ``reasoning`` field, so callers can persist it into the node's captured
    outputs — without it the thinking would exist only on the live stream."""

    upstream = await gateway.open_stream(
        model=model_id,
        messages=messages,
        params=params,
        extra_headers=extra_headers,
        tools=tools,
        tool_choice=tool_choice,
        include_usage=True,  # the final chunk carries token usage → the node span / quota gate
    )
    output = ""
    reasoning_acc = ""
    finish_reason: str | None = None
    usage: dict[str, int] | None = None
    tool_acc: dict[int, dict[str, Any]] = {}
    splitter = ThinkSplitter()  # one per turn — inline think state is per-stream
    async for chunk in upstream:
        parsed = parse_chunk(chunk)
        if parsed.finish_reason:
            finish_reason = parsed.finish_reason
        if parsed.usage:
            usage = parsed.usage
        if parsed.reasoning:
            reasoning_acc += parsed.reasoning
            if on_delta is not None:
                on_delta(parsed.reasoning, "reasoning")
        if parsed.content:
            answer, thinking = splitter.push(parsed.content)
            if thinking:
                reasoning_acc += thinking
                if on_delta is not None:
                    on_delta(thinking, "reasoning")
            if answer:
                output += answer
                if on_delta is not None:
                    on_delta(answer, "content")
        if parsed.tool_calls:
            _accumulate_tool_calls(tool_acc, parsed.tool_calls)
    # Stream end: release anything the splitter held back (a false-partial tag is answer
    # text; an unclosed think block stays reasoning — the all-thinking case then leaves the
    # output honestly blank for the empty-output reason).
    answer, thinking = splitter.flush()
    if thinking:
        reasoning_acc += thinking
        if on_delta is not None:
            on_delta(thinking, "reasoning")
    if answer:
        output += answer
        if on_delta is not None:
            on_delta(answer, "content")
    truncated_empty = finish_reason == "length" and not output.strip() and not tool_acc
    return LlmActivityResult(
        output=output,
        finish_reason=finish_reason,
        truncated_empty=truncated_empty,
        tool_calls=tuple(_finalize_tool_calls(tool_acc)),
        usage=usage,
        reasoning=reasoning_acc if reasoning_acc.strip() else None,
    )


@dataclass(frozen=True)
class ActivityOutcome:
    """The serializable result of a ``tool`` / ``mcp_tool`` activity: ``ok`` → the value binds to
    the success handle(s); ``not ok`` → ``value`` is an actionable error message bound to the
    ``err`` handle and the run continues (the tool ok-err contract, here as a structured return
    rather than handle-mutation so a durable step can checkpoint it)."""

    ok: bool
    value: Any


async def execute_tool(
    tools: ToolRegistry, tool_name: str, args: dict[str, Any]
) -> ActivityOutcome:
    """Resolve ``tool_name`` against the registry and ``await`` it with the already-resolved
    ``args``. A raised exception is a *structured error*, not a run failure: it is returned as
    ``ActivityOutcome(ok=False, message)`` and the caller binds ``err``. (A non-200 HTTP status is
    a normal return value bound to ``ok``; only transport failures raise.)"""

    try:
        fn = tools.get(tool_name)
        value = await fn(**args)
    except Exception as exc:  # tool failed: structured err, run continues.
        return ActivityOutcome(ok=False, value=str(exc))
    return ActivityOutcome(ok=True, value=value)


def rag_top_k(node_config: Mapping[str, Any] | None, config: Any) -> int | None:
    """The node's AUTHORED top_k, or ``None`` when the raw config never set one — validation
    fills the schema default, so only the raw document can tell "explicitly 5" from "unset".
    ``None`` lets the retriever apply the platform default without touching explicit choices."""
    raw = node_config or {}
    return config.top_k if ("topK" in raw or "top_k" in raw) else None


async def execute_rag(
    retriever: Any,
    *,
    source: str,
    query: str,
    top_k: int | None,
    min_similarity: float | None,
) -> ActivityOutcome:
    """Run one retrieval against the injected backend (``WalkContext.rag`` /
    ``DurableResources.rag``). Same ok/err contract as ``execute_tool``: every failure shape —
    no backend wired, unknown source, nothing ingested, an embedding failure — is a *structured
    error* with an actionable message, never a run failure. The success value is the serializable
    ``{source_id, source_name, query, matches: […]}`` retrieval result (matches carry text,
    heading path, source uri/title, and scores — enough for the model to cite)."""

    if retriever is None:
        return ActivityOutcome(
            ok=False, value="rag: no retrieval backend is configured on this runtime"
        )
    try:
        value = await retriever.retrieve(
            source_id=source, query=query, top_k=top_k, min_similarity=min_similarity
        )
    except Exception as exc:  # retrieval failed: structured err, run continues.
        return ActivityOutcome(ok=False, value=str(exc))
    return ActivityOutcome(ok=True, value=value)


async def execute_mcp_tool(
    mcp: McpManager, server: str, tool: str, args: dict[str, Any]
) -> ActivityOutcome:
    """Invoke an external MCP server's tool with already-resolved ``args``. The same ok/err contract
    as ``execute_tool``, only the transport differs. A tool-level error (``McpResult.is_error``), a
    transport failure after one reconnect-retry, or an unknown tool all return ``ok=False`` with an
    **actionable** message naming the *server* and the *kind* of failure (connection vs the server's
    own tool error vs unknown tool) so the agent reading ``err`` can decide its next move. Goes
    through the ``invoke_mcp_tool`` governance chokepoint."""

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


async def mcp_config_from_connection(
    conn: ResolvedConnection, *, connection_id: str | None = None
) -> McpServerConfig:
    """Build the MCP server config from an ``mcp_server`` connection, server-side. Auth is
    resolved here — the secret exists only for this call, never in the IR:

    - ``http``/``sse``: the connection's auth scheme becomes request headers via the same
      ``_auth_headers`` the http tool uses (bearer / api-key / basic / header-map / oauth2
      client-credentials — ONE auth implementation, not a second MCP-flavoured one). A legacy
      connection with a secret but no ``auth`` block keeps the original default: bearer.
      The interactive ``oauth`` scheme instead stamps ``oauth_connection`` so the app-built
      client factory attaches the token flow — the token never rides this config.
    - ``stdio``: the secret enters the subprocess env — one var (``secretEnv``) or a whole
      JSON map (``auth.type == "env"``, several secrets under one rotatable ref).
    - ``openapi``/``graphql``: the spec/endpoint pass through and the SAME auth schemes apply
      to the generated server's *upstream* calls.
    """
    cfg = conn.config or {}
    transport = cfg.get("transport", "stdio")
    if transport in ("http", "sse", "openapi", "graphql"):
        headers: dict[str, str] = dict(cfg.get("headers") or {})
        auth = cfg.get("auth") or {}
        if conn.secret and not auth.get("type"):
            headers["Authorization"] = f"Bearer {conn.secret}"  # pre-auth-block default
        else:
            headers.update(await _auth_headers(conn))
        oauth_ref = connection_id if auth.get("type") == "oauth" else None
        # Generated-server knobs may sit flat on the connection config (the form writes them
        # that way) or pre-nested under ``options`` — normalize to one place.
        options = dict(cfg.get("options") or {})
        for knob in ("allowMutations", "include", "exclude", "timeoutSeconds"):
            if knob in cfg:
                options.setdefault(knob, cfg[knob])
        return McpServerConfig(
            transport=transport,
            url=cfg.get("url") or cfg.get("baseUrl"),
            headers=headers or None,
            spec=cfg.get("spec"),
            options=options,
            oauth_connection=oauth_ref,
        )
    env: dict[str, str] = dict(cfg.get("env") or {})
    if conn.secret:
        secret_env = cfg.get("secretEnv")
        if isinstance(secret_env, str) and secret_env:
            env[secret_env] = conn.secret
        elif (cfg.get("auth") or {}).get("type") == "env":
            env.update(_secret_map(conn.secret, what="env"))
    return McpServerConfig(
        transport="stdio",
        command=cfg.get("command"),
        args=list(cfg.get("args") or []),
        env=env or None,
        cwd=cfg.get("cwd"),
    )


async def execute_mcp_connection_tool(
    mcp: McpManager, name: str, config: McpServerConfig, tool: str, args: dict[str, Any]
) -> ActivityOutcome:
    """Invoke a CONNECTION-BACKED MCP tool — same ok/err contract as ``execute_mcp_tool`` but the
    server is reached via the connection's resolved ``config`` (transport + server-side auth) keyed
    by ``name``. The chokepoint + retry live in ``call_connection_tool``."""
    try:
        result = await mcp.call_connection_tool(name, config, tool, args)
        if result.is_error:
            return ActivityOutcome(
                ok=False,
                value=f"mcp connection {name!r} tool {tool!r} error: {_stringify(result.value)}",
            )
        return ActivityOutcome(ok=True, value=result.value)
    except McpConnectionError as exc:
        return ActivityOutcome(ok=False, value=f"mcp connection {name!r} unavailable: {exc}")
    except McpToolNotFound as exc:
        return ActivityOutcome(ok=False, value=str(exc))
    except Exception as exc:
        return ActivityOutcome(
            ok=False, value=f"mcp connection {name!r} tool {tool!r} failed: {exc}"
        )


# ── audio model-call activities (transcribe · speak) ─────────────────────────
#
# Both node types map 1:1 to an OpenAI audio endpoint and route to a binding whose
# ``capabilities.modalities`` includes the matching key (the inference plane enforces
# logical-id-only — an engine name is rejected at the seam). The audio bytes go to the
# **inference base URL** (the gateway), in the user's trust domain — never a control-plane route.
# Non-text payloads are REFERENCES (artifacts), not journaled.

_AUDIO_FORMAT_MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


async def execute_transcribe(
    gateway: GatewayClient,
    artifacts: Any,
    *,
    model_id: str,
    params: dict[str, Any],
    audio_ref: Any,
    extra_headers: Mapping[str, str],
) -> ActivityOutcome:
    """Run a ``transcribe`` activity — audio-ref in → text out. Fetches the audio bytes from the
    reference (artifact id / url / path) and streams them to ``POST /v1/audio/transcriptions``
    (logical-id). The runtime-agnostic body both runtimes wrap. A fetch/transport failure binds an
    honest ``err`` (the tool ok/err contract); the bytes never touch a control-plane route."""
    if artifacts is None:
        return ActivityOutcome(ok=False, value="transcribe: no artifact store configured")
    try:
        data, content_type = await artifacts.fetch(audio_ref)
    except Exception as exc:
        return ActivityOutcome(ok=False, value=f"transcribe: cannot read audio ref: {exc}")
    try:
        file_tuple = ("audio", data, content_type)
        result = await gateway.transcribe(
            model=model_id, file=file_tuple, params=params, extra_headers=extra_headers
        )
    except Exception as exc:
        return ActivityOutcome(ok=False, value=f"transcribe failed: {exc}")
    # The gateway returns the OpenAI transcription shape — a dict OR the SDK's ``Transcription``
    # object (which carries ``.text``). Extract the text either way (a serializable string), never
    # bind the raw SDK object (it isn't JSON-serializable for the run output).
    if isinstance(result, dict):
        text = result.get("text", "")
    else:
        text = getattr(result, "text", None) or str(result)
    return ActivityOutcome(ok=True, value=text)


async def execute_speak(
    gateway: GatewayClient,
    artifacts: Any,
    *,
    model_id: str,
    params: dict[str, Any],
    text: str,
    extra_headers: Mapping[str, str],
) -> ActivityOutcome:
    """Run a ``speak`` activity — text in → audio-REFERENCE out. Calls ``POST /v1/audio/speech``
    (logical-id), then stores the produced bytes as an artifact and returns the REFERENCE (the bytes
    are an artifact, not journaled — a resumed durable run replays the ref, not the audio). The
    ``format`` param maps to the data plane's ``response_format``."""
    if artifacts is None:
        return ActivityOutcome(ok=False, value="speak: no artifact store configured")
    voice = str(params.get("voice", "alloy"))
    fmt = str(params.get("format", "mp3"))
    gw_params = {k: v for k, v in params.items() if k not in ("voice", "format")}
    gw_params["response_format"] = fmt
    try:
        audio = await gateway.speak(
            model=model_id, text=text, voice=voice, params=gw_params, extra_headers=extra_headers
        )
    except Exception as exc:
        return ActivityOutcome(ok=False, value=f"speak failed: {exc}")
    ref = await artifacts.put(audio, _AUDIO_FORMAT_MIME.get(fmt, "application/octet-stream"))
    return ActivityOutcome(ok=True, value=ref)


# ── image-generation model-call activity (imagine) ───────────────────────────
#
# The image analog of ``speak``: text in → an image REFERENCE out, mapping 1:1 to
# ``POST /v1/images/generations``. It routes to a binding whose ``capabilities.modalities`` includes
# ``images.generation``; the produced bytes are an artifact (never journaled), so a resumed durable
# run replays the ref, not the image. The bytes go to the inference base URL (the user's trust
# domain), never a control-plane route.

_IMAGE_MIME = "image/png"


async def execute_imagine(
    gateway: GatewayClient,
    artifacts: Any,
    *,
    model_id: str,
    params: dict[str, Any],
    prompt: str,
    extra_headers: Mapping[str, str],
) -> ActivityOutcome:
    """Run an ``imagine`` activity — text in → image-REFERENCE out. Calls
    ``POST /v1/images/generations`` (logical-id), then stores the produced bytes as an artifact and
    returns the REFERENCE. ``size``/``n``/``steps`` ride through as generation params."""
    if artifacts is None:
        return ActivityOutcome(ok=False, value="imagine: no artifact store configured")
    try:
        image = await gateway.generate_image(
            model=model_id, prompt=prompt, params=params, extra_headers=extra_headers
        )
    except Exception as exc:
        return ActivityOutcome(ok=False, value=f"imagine failed: {exc}")
    ref = await artifacts.put(image, _IMAGE_MIME)
    return ActivityOutcome(ok=True, value=ref)


# ── the generic http tool (REST + GraphQL) with connection-injected auth ─────
#
# A ``tool`` node whose ``config.tool`` resolves to an ``http`` binding in ``ir.tools`` becomes a
# real outbound HTTP call. The auth lives in a server-side ``connection``: the binding names the
# connection id, the handler resolves connection → secret_ref → secret AT STEP TIME and injects the
# auth header — never the IR, the canvas, a span, or the journal. The same ok/err contract as the
# ``tool`` node (a transport failure binds ``err``; a non-2xx is a normal return value bound to
# ``ok``). The durable runtime owns retry (provider-client retry is off): this body does NOT retry.
# Substitution of url/headers/body uses the unified ``$in.field`` grammar, resolved by the caller
# before the call (deterministic, journaled inputs) — only the auth is added here.


@dataclass(frozen=True)
class ResolvedConnection:
    """A connection resolved to the data the http handler needs AT STEP TIME: its non-secret
    ``config`` (auth scheme, base url, static headers) and the decrypted ``secret`` (or ``None``
    for an auth-less / secret-missing connection). The plaintext exists only here, inside the step —
    never journaled. ``enabled`` lets the resolver signal a disabled connection (→ honest
    ``err``)."""

    config: dict[str, Any]
    secret: str | None
    enabled: bool = True


#: The seam the handler resolves a connection through — a callable injected by the control-plane
#: (it closes over the ConnectionStore + SecretStore + sessionmaker). The walker stays free of a DB
#: session: it calls this, the resolver owns the DB read (the same pattern as the telemetry
#: side-channel). ``None`` result = unknown/disabled connection → the handler binds ``err``.
ConnectionResolver = Callable[[str], Awaitable[ResolvedConnection | None]]


async def _resolve_oauth2_token(auth: Mapping[str, Any], secret: str) -> str:
    """Fetch an OAuth2 client-credentials access token — POST the token endpoint with
    ``grant_type=client_credentials`` + client id/secret, return ``access_token``. The client secret
    is the connection's resolved secret; it never appears in the IR. This is the one auth scheme
    that needs its own I/O (a token fetch) before the main call."""
    token_url = auth.get("tokenUrl") or auth.get("token_url")
    if not token_url:
        raise ValueError(
            "oauth2_client_credentials auth requires a 'tokenUrl' in connection config"
        )
    data = {"grant_type": "client_credentials"}
    client_id = auth.get("clientId") or auth.get("client_id")
    if client_id:
        data["client_id"] = client_id
        data["client_secret"] = secret
    scope = auth.get("scope")
    if scope:
        data["scope"] = scope
    async with httpx.AsyncClient() as client:
        resp = await client.post(token_url, data=data, timeout=30.0)
    resp.raise_for_status()
    token = resp.json().get("access_token")
    if not token:
        raise ValueError("oauth2 token endpoint returned no access_token")
    return str(token)


def _secret_map(secret: str | None, *, what: str) -> dict[str, str]:
    """Parse a JSON-object secret (a map of names → values under ONE secret ref, so rotation
    stays hash-stable) into a plain string dict. A malformed blob is a loud error naming the
    scheme — silently sending no auth would produce baffling 401s downstream."""
    try:
        parsed = json.loads(secret or "")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} auth requires the secret to be a JSON object map") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{what} auth requires the secret to be a JSON object map")
    return {str(k): str(v) for k, v in parsed.items()}


async def _auth_headers(conn: ResolvedConnection) -> dict[str, str]:
    """Build the auth header(s) for a resolved connection, server-side. Supports bearer, api-key
    header, basic, a whole header map (``headers`` — the secret is a JSON object, for servers
    that want several/custom auth headers), and oauth2 client-credentials. An auth-less
    connection (no scheme) or a missing secret adds nothing. The interactive ``oauth`` scheme
    adds nothing HERE — its token rides an httpx auth hook attached by the client factory, not a
    static header. Raw secret values never leave this function."""
    auth = (conn.config or {}).get("auth") or {}
    atype = auth.get("type")
    if not atype or atype == "oauth":
        return {}
    if conn.secret is None and atype != "oauth2_client_credentials":
        return {}  # the scheme needs a secret that isn't set — caller's call proceeds auth-less
    if atype == "bearer":
        return {"Authorization": f"Bearer {conn.secret}"}
    if atype == "api_key":
        return {str(auth.get("header", "X-API-Key")): str(conn.secret)}
    if atype == "basic":
        user = str(auth.get("username", ""))
        token = base64.b64encode(f"{user}:{conn.secret}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}"}
    if atype == "headers":
        return _secret_map(conn.secret, what="headers")
    if atype == "oauth2_client_credentials":
        token = await _resolve_oauth2_token(auth, conn.secret or "")
        return {"Authorization": f"Bearer {token}"}
    raise ValueError(f"unsupported connection auth type {atype!r}")


#: Public name for building a connection's auth headers outside a step (e.g. previewing a
#: generated server against its upstream) — the SAME implementation the step-time paths use,
#: so preview and execution can never disagree about what auth is sent.
connection_auth_headers = _auth_headers


def _apply_response_map(value: Any, path: str) -> Any:
    """A minimal JSONPath shaper (``responseMap``) — ``$.a.b`` / ``$.a[0].b`` into the parsed
    response body. No dependency; supports dotted keys + ``[index]``. If the path can't be walked
    (a typo / missing field), returns the value unchanged (graceful — a 2xx is not failed by a map
    miss; the data is still there to inspect)."""
    expr = path.lstrip("$").lstrip(".")
    if not expr:
        return value
    cur = value
    for seg in expr.split("."):
        key, _, idx = seg.partition("[")
        if key:
            if not isinstance(cur, dict) or key not in cur:
                return value
            cur = cur[key]
        if idx:
            try:
                i = int(idx.rstrip("]"))
                cur = cur[i]
            except (ValueError, IndexError, TypeError, KeyError):
                return value
    return cur


async def execute_http_tool(
    conn: ResolvedConnection | None,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any,
    response_map: str | None,
    idempotency_key: str | None,
    timeout_s: float | None,
) -> ActivityOutcome:
    """Run one http-tool activity — the runtime-agnostic body both the interactive walker and the
    durable step wrap. ``conn`` is the connection resolved AT STEP TIME (auth injected here,
    server-side); a missing/disabled connection (``conn is None``) binds an honest ``err``. The
    non-2xx status is a normal return value (bound ``ok`` as ``{status, body, headers}``); only a
    transport failure (timeout/DNS/refused) or an auth-setup error binds ``err``. The
    ``idempotency_key`` (set for unsafe methods) adds an ``Idempotency-Key`` header so a
    partial-failure re-run is a provider no-op. Provider-client retry is OFF (the durable runtime
    owns retry). The ``timeout_s`` is a leaf knob, not a caller-cancellation handle (ASYNC109)."""
    if conn is None:
        return ActivityOutcome(ok=False, value="http tool: connection not found or disabled")
    final_url = url
    base = (conn.config or {}).get("baseUrl")
    if base and not url.lower().startswith(("http://", "https://")):
        final_url = base.rstrip("/") + "/" + url.lstrip("/")
    final_headers: dict[str, str] = {}
    final_headers.update((conn.config or {}).get("headers") or {})  # connection static headers
    final_headers.update(headers)  # node headers
    if idempotency_key:
        final_headers["Idempotency-Key"] = idempotency_key
    try:
        final_headers.update(await _auth_headers(conn))  # server-side auth injection
    except Exception as exc:  # auth setup (e.g. oauth token fetch) failed — honest err, no hang
        return ActivityOutcome(ok=False, value=f"http tool auth failed: {exc}")
    kwargs: dict[str, Any] = {"headers": final_headers, "timeout": timeout_s or 30.0}
    if body is not None:
        kwargs["json"] = body  # JSON request (REST or GraphQL {query, variables})
    try:
        # Redirects are NOT followed: the request carries connection-injected auth (bearer /
        # api-key / basic headers), and auto-following a redirect would replay those headers at
        # whatever host the response names — only `Authorization` is stripped cross-host by the
        # client, an `X-API-Key` would leak. A 3xx comes back as a normal {status, …} value,
        # consistent with "a non-2xx status is a normal return value".
        async with httpx.AsyncClient(follow_redirects=False) as client:
            resp = await client.request(method.upper(), final_url, **kwargs)
    except Exception as exc:  # transport failure → err (structured, run continues), never a hang
        return ActivityOutcome(ok=False, value=f"http tool request failed: {exc}")
    try:
        parsed: Any = resp.json()
    except (ValueError, TypeError):
        parsed = resp.text
    if response_map:
        # responseMap shapes the STEP OUTPUT: return just the mapped value, not the
        # {status, body, headers} envelope — the author opted into the shaped data.
        mapped = (
            _apply_response_map(parsed, response_map) if isinstance(parsed, dict | list) else parsed
        )
        return ActivityOutcome(ok=True, value=mapped)
    return ActivityOutcome(
        ok=True,
        value={"status": resp.status_code, "body": parsed, "headers": dict(resp.headers)},
    )


def is_http_tool(ir: IRDocument, tool_key: str) -> HttpTool | None:
    """The ``http`` binding for ``tool_key`` in ``ir.tools``, or ``None`` if the key is a builtin /
    absent (a builtin tool key). Lets the ``tool`` handler route http-vs-builtin."""
    binding = ir.tools.get(tool_key)
    return binding if isinstance(binding, HttpTool) else None


_PURE_REF = re.compile(r"\$in(?:\.[A-Za-z0-9_]+)*$")


def _resolve_template_value(value: Any, ports: _PortInputs, node_id: str) -> Any:
    """Substitute ``$in`` refs inside a body-template value. Only a string that is
    EXACTLY a ``$in`` ref is resolved (to the typed value — so a GraphQL variable stays a
    number/object); every OTHER string is left LITERAL, so a GraphQL query's own ``$var`` syntax is
    never mistaken for a theygent token. dict/list leaves recurse; non-strings pass through.
    (url/headers use embedded ``_render_template`` interpolation; bodies are ref-or-literal.)"""
    if isinstance(value, str):
        if _PURE_REF.fullmatch(value):
            return _resolve_ref(value, ports, node_id)
        return value  # literal — protects GraphQL ``$c``, ``$amount``, JSON-with-$, etc.
    if isinstance(value, dict):
        return {k: _resolve_template_value(v, ports, node_id) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_template_value(v, ports, node_id) for v in value]
    return value


@dataclass(frozen=True)
class HttpCall:
    """A fully-resolved http request — the deterministic part (templates substituted from journaled
    inputs) the caller hands to :func:`execute_http_tool`. The connection (auth) is resolved
    separately, inside the step. Shared by the interactive walker and the durable step so both build
    the same request (parity)."""

    connection_id: str
    method: str
    url: str
    headers: dict[str, str]
    body: Any
    response_map: str | None
    idempotency_key: str | None
    timeout: float | None


def build_http_call(
    binding: HttpTool, config: ToolConfig, ports: _PortInputs, *, node_id: str, run_id: str
) -> HttpCall:
    """Resolve an http-tool node's config into a concrete request, substituting the unified
    ``$in.field`` grammar in url / headers / body. ``idempotency_key`` additionally expands
    ``{runId}`` / ``{nodeId}`` so retries are idempotent. Auth is NOT added here — it is injected
    server-side from the connection inside the step."""
    url = _render_template(config.url_template or "", ports, node_id)
    headers = {k: _render_template(v, ports, node_id) for k, v in (config.headers or {}).items()}
    body = (
        _resolve_template_value(config.body_template, ports, node_id)
        if config.body_template is not None
        else None
    )
    idem = config.idempotency_key
    if idem:
        idem = _render_template(
            idem.replace("{runId}", run_id).replace("{nodeId}", node_id), ports, node_id
        )
    return HttpCall(
        connection_id=binding.connection,
        method=config.method or "GET",
        url=url,
        headers=headers,
        body=body,
        response_map=config.response_map,
        idempotency_key=idem,
        timeout=config.timeout_seconds,
    )


async def run_http_tool(
    binding: HttpTool,
    config: ToolConfig,
    ports: _PortInputs,
    *,
    node_id: str,
    run_id: str,
    resolver: ConnectionResolver | None,
) -> ActivityOutcome:
    """Build + execute an http-tool call — the one body both runtimes call so the http path is
    identical interactive vs durable. Builds the request (deterministic), resolves the connection
    through ``resolver`` (server-side auth, at step time), then runs it. A template error or a
    missing resolver binds ``err`` honestly (never a hang)."""
    try:
        call = build_http_call(binding, config, ports, node_id=node_id, run_id=run_id)
    except Exception as exc:  # unresolvable $in in url/headers/body → structured err
        return ActivityOutcome(ok=False, value=f"http tool {config.tool!r}: {exc}")
    try:
        conn = await resolver(call.connection_id) if resolver is not None else None
    except Exception as exc:  # e.g. an undecryptable secret — an honest err, never a run failure
        return ActivityOutcome(ok=False, value=f"http tool {config.tool!r}: {exc}")
    return await execute_http_tool(
        conn,
        method=call.method,
        url=call.url,
        headers=call.headers,
        body=call.body,
        response_map=call.response_map,
        idempotency_key=call.idempotency_key,
        timeout_s=call.timeout,
    )


# ── autonomous tool-calling: function schemas + per-call dispatch ─────────────


def _to_openai_tool_choice(tool_choice: Any) -> Any:
    """Lower the IR ``tool_choice`` to the OpenAI wire value: ``auto``/``none``/``required`` pass
    through; a named choice (a ``_NamedToolChoice`` model) becomes ``{type, function:{name}}``."""
    if isinstance(tool_choice, str):
        return tool_choice
    if hasattr(tool_choice, "model_dump"):
        return tool_choice.model_dump(by_alias=True)
    return tool_choice


def _fn_schema(
    name: str, description: str | None, parameters: dict[str, Any] | None
) -> dict[str, Any]:
    """One OpenAI function-tool schema. ``name`` is the ir.tools key the model calls (so a tool_call
    maps straight back to its binding); description + JSON-Schema ``parameters`` steer the model."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or "",
            "parameters": parameters or {"type": "object", "properties": {}},
        },
    }


async def _schema_for_binding(
    name: str,
    binding: Any,
    *,
    registry: ToolRegistry,
    mcp: McpManager,
    tool_auth: ConnectionResolver | None = None,
) -> dict[str, Any] | None:
    """One OpenAI function schema from a tool BINDING object — the shared core of the ir.tools path
    and the capability-node path. ``name`` is the function name the model echoes back: an ir.tools
    key or a tool node id. None when no schema resolves (skip it)."""
    if isinstance(binding, HttpTool):
        return _fn_schema(name, binding.description, binding.parameter_schema)
    if isinstance(binding, BuiltinTool):
        meta = registry.schema(binding.ref)
        return _fn_schema(name, meta[0], meta[1]) if meta is not None else None
    if isinstance(binding, McpTool) and binding.server:
        with contextlib.suppress(Exception):  # server unreachable → no schema (best-effort)
            for d in await mcp.list_tools(binding.server):
                if d.name == binding.tool:
                    return _fn_schema(name, d.description, d.input_schema)
    if isinstance(binding, McpTool) and binding.connection and tool_auth is not None:
        # A CONNECTION-backed server describes its tools too — without this lookup the binding
        # was silently absent from the model's schemas, making the tool uncallable via ir.tools
        # while the dispatch path fully supported it.
        with contextlib.suppress(Exception):  # unresolvable/unreachable → no schema (best-effort)
            conn = await tool_auth(binding.connection)
            if conn is not None:
                cfg = await mcp_config_from_connection(conn, connection_id=binding.connection)
                for d in await mcp.list_connection_tools(binding.connection, cfg):
                    if d.name == binding.tool:
                        return _fn_schema(name, d.description, d.input_schema)
    return None


async def build_tool_schemas(
    ir: IRDocument,
    tool_keys: list[str],
    *,
    registry: ToolRegistry,
    mcp: McpManager,
    tool_auth: ConnectionResolver | None = None,
) -> list[dict[str, Any]]:
    """Build the OpenAI ``tools`` array from an llm node's ``tools`` keys (the ir.tools path). The
    function NAME is the ir.tools key (so a tool_call maps back to its binding); a key with no
    resolvable schema is skipped. (Capability edges go through ``build_capability_schemas``.)"""

    schemas: list[dict[str, Any]] = []
    for key in tool_keys:
        binding = ir.tools.get(key)
        if binding is None and key in registry:  # a directly-registered builtin (echo/http_fetch)
            meta = registry.schema(key)
            if meta is not None:
                schemas.append(_fn_schema(key, meta[0], meta[1]))
            continue
        schema = await _schema_for_binding(
            key, binding, registry=registry, mcp=mcp, tool_auth=tool_auth
        )
        if schema is not None:
            schemas.append(schema)
    return schemas


def _capability_tool_nodes(ir: IRDocument, llm_node_id: str) -> list[Node]:
    """The tool/mcp_tool/rag nodes wired to this llm's ``tool``-role port (capability edges) — the
    source of every inbound ``channel:"tool"`` edge. These are the tools the model may CALL; they
    are NOT run in topological order (a capability executes lazily inside the llm loop, args from
    the model). Deduped, source order preserved."""
    by_id = {n.id: n for n in ir.nodes}
    nodes: list[Node] = []
    seen: set[str] = set()
    for edge in ir.edges:
        if edge.channel != "tool" or edge.target != llm_node_id or edge.source in seen:
            continue
        src = by_id.get(edge.source)
        if src is not None and src.type in ("tool", "mcp_tool", "rag"):
            nodes.append(src)
            seen.add(edge.source)
    return nodes


def _capability_binding(ir: IRDocument, name: str) -> Any | None:
    """Resolve a model tool_call ``name`` (a tool/mcp_tool NODE id) to an ir.tools-style BINDING
    built from the node's INLINE config, so the existing ``execute_tool_call`` dispatch (and the
    durable ``_durable_tool_call``) handle it unchanged. Returns None if ``name`` is not a tool node
    id — the caller then falls back to the ir.tools / registry lookup."""
    node = next((n for n in ir.nodes if n.id == name and n.type in ("tool", "mcp_tool")), None)
    if node is None:
        return None
    if node.type == "mcp_tool":
        c = McpToolConfig.model_validate(node.config)
        return McpTool(kind="mcp", server=c.server, connection=c.connection, tool=c.tool)
    c = ToolConfig.model_validate(node.config)
    if c.connection or c.url_template:  # an http tool (vs a builtin ref held in c.tool)
        return HttpTool(
            kind="http",
            connection=c.connection or "",
            description=c.description,
            parameter_schema=c.parameter_schema,
            method=c.method,
            url_template=c.url_template,
            body_template=c.body_template,
            headers=c.headers,
            response_map=c.response_map,
            idempotency_key=c.idempotency_key,
            timeout_seconds=c.timeout_seconds,
        )
    return BuiltinTool(kind="builtin", ref=c.tool)


async def build_capability_schemas(
    nodes: list[Node],
    *,
    registry: ToolRegistry,
    mcp: McpManager,
    tool_auth: ConnectionResolver | None = None,
) -> list[dict[str, Any]]:
    """OpenAI tool schemas for capability nodes — the function NAME is the NODE id (collision-safe:
    two llms may wire different nodes that share a builtin/server). Description + parameters come
    from each node's inline config: builtin from the registry, http from ``description`` +
    ``parameterSchema``, mcp from the server's ``list_tools`` (an unreachable/unresolvable mcp
    falls back to its authored ``description`` with no parameter schema)."""
    schemas: list[dict[str, Any]] = []
    for node in nodes:
        if node.type == "mcp_tool":
            cfg = McpToolConfig.model_validate(node.config)
            schema: dict[str, Any] | None = None
            if cfg.server:
                with contextlib.suppress(Exception):
                    for d in await mcp.list_tools(cfg.server):
                        if d.name == cfg.tool:
                            schema = _fn_schema(
                                node.id, cfg.description or d.description, d.input_schema
                            )
                            break
            elif cfg.connection and tool_auth is not None:
                with contextlib.suppress(Exception):  # unreachable → the authored fallback below
                    conn = await tool_auth(cfg.connection)
                    if conn is not None:
                        mcfg = await mcp_config_from_connection(conn, connection_id=cfg.connection)
                        for d in await mcp.list_connection_tools(cfg.connection, mcfg):
                            if d.name == cfg.tool:
                                schema = _fn_schema(
                                    node.id, cfg.description or d.description, d.input_schema
                                )
                                break
            schemas.append(schema or _fn_schema(node.id, cfg.description, None))
        elif node.type == "rag":
            # A retrieval capability self-describes statically: one required ``query`` string.
            # topK/minSimilarity are authored knobs on the node, NOT model-settable.
            rcfg = RagConfig.model_validate(node.config)
            schemas.append(
                _fn_schema(
                    node.id,
                    rcfg.description
                    or "Search the connected knowledge base for passages relevant to a query. "
                    "Returns the best-matching excerpts with their source documents.",
                    {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "What to search for (natural language).",
                            }
                        },
                        "required": ["query"],
                    },
                )
            )
        else:  # a tool node — builtin or http
            tcfg = ToolConfig.model_validate(node.config)
            if tcfg.connection or tcfg.url_template:
                schemas.append(_fn_schema(node.id, tcfg.description, tcfg.parameter_schema))
            else:
                meta = registry.schema(tcfg.tool)
                if meta is not None:
                    schemas.append(_fn_schema(node.id, meta[0], meta[1]))
    return schemas


def tool_result_message(call: ToolCall, outcome: ActivityOutcome) -> dict[str, Any]:
    """The ``{role: tool}`` message fed back to the model after a tool call: the tool's result on
    success, or its actionable error on failure (a failed tool is NOT a run failure; the model sees
    the error and can recover)."""
    return {"role": "tool", "tool_call_id": call.id, "content": _stringify(outcome.value)}


def assistant_tool_calls_message(calls: list[ToolCall]) -> dict[str, Any]:
    """The assistant turn that requested the tool calls — appended to the transcript before the tool
    results so the next model turn sees its own request + the answers (the OpenAI loop shape)."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in calls
        ],
    }


async def execute_tool_call(
    ir: IRDocument,
    call: ToolCall,
    *,
    registry: ToolRegistry,
    mcp: McpManager,
    tool_auth: ConnectionResolver | None,
    run_id: str,
    node_id: str,
    idempotency_suffix: str = "",
    rag: Any = None,
) -> ActivityOutcome:
    """Execute ONE model-emitted tool call by dispatching to the EXISTING executor for its binding —
    no new tool machinery. ``call.name`` is an ir.tools key (or a directly-registered builtin);
    ``call.arguments`` is the model's args. For an http tool the args ARE the ``$in`` source: the
    binding's url/body slots are filled from them. ok/err is the standard tool contract — a tool
    error is RETURNED, not raised, so the loop feeds it back to the model. The
    ``idempotency_suffix`` (the call id / iteration) is appended to the http idempotency key so a
    write tool called twice in one loop under at-least-once doesn't collide."""

    # A rag capability dispatches by NODE, not by an ir.tools binding — retrieval has no
    # tools-block shape (the source id + knobs live in the node's own config; the model
    # supplies only the query).
    rag_node = next((n for n in ir.nodes if n.id == call.name and n.type == "rag"), None)
    if rag_node is not None:
        rcfg = RagConfig.model_validate(rag_node.config)
        return await execute_rag(
            rag,
            source=rcfg.source,
            query=str(call.arguments.get("query") or ""),
            top_k=rag_top_k(rag_node.config, rcfg),
            min_similarity=rcfg.min_similarity,
        )

    # ``call.name`` is an ir.tools key. If it isn't, it may be a tool/mcp_tool NODE id wired as a
    # capability — resolve the binding from that node's inline config. Either way the same dispatch
    # below runs. ir.tools keys are tried first.
    binding = ir.tools.get(call.name) or _capability_binding(ir, call.name)
    if binding is None:
        if call.name in registry:  # a directly-registered builtin (echo / http_fetch)
            return await execute_tool(registry, call.name, call.arguments)
        return ActivityOutcome(ok=False, value=f"unknown tool {call.name!r}")
    if isinstance(binding, BuiltinTool):
        return await execute_tool(registry, binding.ref, call.arguments)
    if isinstance(binding, McpTool):
        if binding.server:
            return await execute_mcp_tool(mcp, binding.server, binding.tool, call.arguments)
        try:
            conn = await tool_auth(binding.connection or "") if tool_auth is not None else None
        except Exception as exc:  # e.g. an undecryptable secret — honest err, never a run failure
            return ActivityOutcome(ok=False, value=f"mcp connection {binding.connection!r}: {exc}")
        if conn is None:
            return ActivityOutcome(
                ok=False, value=f"mcp connection {binding.connection!r} not found or disabled"
            )
        cfg = await mcp_config_from_connection(conn, connection_id=binding.connection)
        return await execute_mcp_connection_tool(
            mcp, binding.connection or "", cfg, binding.tool, call.arguments
        )
    if isinstance(binding, HttpTool):
        props = frozenset((binding.parameter_schema or {}).get("properties", {}).keys())
        ports = _PortInputs(
            values=call.arguments, declared=props | frozenset(call.arguments.keys())
        )
        idem = binding.idempotency_key
        if idem and idempotency_suffix:
            idem = f"{idem}-{idempotency_suffix}"
        config = ToolConfig(
            tool=call.name,
            method=binding.method,
            url_template=binding.url_template,
            body_template=binding.body_template,
            headers=binding.headers,
            response_map=binding.response_map,
            idempotency_key=idem,
            timeout_seconds=getattr(binding, "timeout_seconds", None),
        )
        return await run_http_tool(
            binding, config, ports, node_id=node_id, run_id=run_id, resolver=tool_auth
        )
    return ActivityOutcome(ok=False, value=f"tool {call.name!r}: unsupported binding")


# ── the transform node (deterministic reshape — NOT a code sandbox) ──────────


def execute_transform(expr: str, ports: _PortInputs, node_id: str) -> Any:
    """Reshape the in-port value(s) by a deterministic JSON template. ``expr`` is a JSON document
    whose string leaves may be ``$in`` refs (``$in.in.location.city``) — the handler parses it and
    resolves each ref over the journaled inputs (reusing ``_resolve_template_value``). No I/O, no
    sandbox (``kind: orchestration``), so it runs inline in both runtimes and replays
    deterministically. A non-JSON ``expr`` or an unresolvable ref is a LOUD
    :class:`TransformError` (no ``err`` port — a bad reshape fails the run). A full JSONata/jq DSL
    is the deferred richer form; the JSON-template form is the cheap, dep-free reshape."""
    try:
        template = json.loads(expr)
    except (ValueError, TypeError) as exc:
        raise TransformError(
            f"transform {node_id!r}: expr is not valid JSON (it is a JSON template whose string "
            f"leaves may be $in refs): {exc}"
        ) from exc
    try:
        return _resolve_template_value(template, ports, node_id)
    except _RefError as exc:
        raise TransformError(f"transform {node_id!r}: {exc}") from exc


# ── the guardrail node (rule⇒orchestration · model⇒activity) ────────────────
#
# A guardrail short-circuits before downstream work: ``pass`` carries the input through; ``block``
# carries the ``on_block`` payload (or the violation) — wire it to an ``output`` to refuse BEFORE
# the expensive node. A RULE check is deterministic + inline (no I/O → orchestration); a MODEL check
# is a classifier call (→ activity). The ``kind`` follows the backend (validated in packages/ir).

#: Default PII patterns for the ``pii`` rule (deterministic — emails, SSN-ish, card-ish digits). The
#: author can override with ``spec.patterns``. A guardrail PASSES when NO pattern matches (it blocks
#: input containing PII), the safe default for a "don't leak PII" gate.
_PII_PATTERNS = [
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",  # email
    r"\b\d{3}-\d{2}-\d{4}\b",  # US SSN
    r"\b(?:\d[ -]*?){13,16}\b",  # card-ish run of digits
]


def evaluate_guardrail_rule(rule: GuardrailRule, value: Any) -> bool:
    """Evaluate a deterministic guardrail rule — return True = PASS. No I/O. ``value``
    is the in-port value; string-shaped rules stringify it (``_stringify``). The predicates:
    ``regex`` (``spec.pattern`` [+ ``mode`` allow|deny]); ``length`` (``spec.min``/``max`` over the
    string); ``allow`` / ``deny`` (membership of the stringified value in ``spec.values``);
    ``json_schema`` (minimal: the value is an object with all of ``spec.required`` keys); ``pii``
    (PASS iff NO PII pattern matches — blocks leaks)."""
    spec = rule.spec or {}
    text = value if isinstance(value, str) else _stringify(value)
    if rule.kind == "regex":
        pattern = str(spec.get("pattern", ""))
        matched = bool(re.search(pattern, text)) if pattern else False
        return matched if spec.get("mode", "allow") == "allow" else not matched
    if rule.kind == "length":
        n = len(text)
        lo = spec.get("min")
        hi = spec.get("max")
        if lo is not None and n < int(lo):
            return False
        return not (hi is not None and n > int(hi))
    if rule.kind == "allow":
        return text in [str(v) for v in spec.get("values", [])]
    if rule.kind == "deny":
        return text not in [str(v) for v in spec.get("values", [])]
    if rule.kind == "json_schema":
        parsed = _parse_if_json(value)
        if not isinstance(parsed, dict):
            return False
        return all(k in parsed for k in spec.get("required", []))
    if rule.kind == "pii":
        patterns = spec.get("patterns") or _PII_PATTERNS
        return not any(re.search(p, text) for p in patterns)
    return False  # unknown rule kind (validated upstream) — fail closed


def _bind_guardrail(
    node: Node,
    passed: bool,
    input_value: Any,
    on_block: Any,
    values: dict[tuple[str, str], Any],
    live_handles: dict[str, set[str]],
) -> None:
    """Activate ``pass`` (carrying the input through) or ``block`` (carrying ``on_block`` or the
    input) — exactly one handle, so the un-taken branch's edges are dead (edge-skipping). The
    canonical ``control``/``data`` example: guardrail ``pass`` → llm; guardrail ``block`` →
    output."""
    handle = "pass" if passed else "block"
    live_handles[node.id] = {handle}
    values[(node.id, handle)] = input_value if passed else (on_block or input_value)


async def execute_guardrail_model(
    gateway: GatewayClient,
    *,
    model_id: str,
    params: dict[str, Any],
    prompt: str,
    input_value: Any,
    extra_headers: Mapping[str, str],
) -> LlmActivityResult:
    """Run a model guardrail's classifier call — the runtime-agnostic body both runtimes wrap.
    Builds one user message (the judge prompt + the input) and runs one model turn via
    ``execute_llm``. Returns the FULL turn result: the caller compares ``.output`` to ``pass_on``
    and lands ``.usage`` on the call's generate span — the judge call spends real tokens, so the
    quota gate must see them like any other model call."""
    messages = [{"role": "user", "content": f"{prompt}\n\nInput:\n{_stringify(input_value)}"}]
    return await execute_llm(
        gateway, model_id=model_id, params=params, messages=messages, extra_headers=extra_headers
    )


def guardrail_model_passed(answer: str, pass_on: str) -> bool:
    """A model guardrail passes when the classifier's answer begins with ``pass_on`` (case/space
    insensitive) — e.g. a "yes/no, is this in scope?" judge with ``pass_on='yes'``."""
    return answer.strip().lower().startswith(pass_on.strip().lower())


# ── the gate nodes (ratelimit · quota — per-key, per-user deferred) ──────────


def resolve_gate_key(key_expr: str, ports: _PortInputs, node_id: str) -> str:
    """Resolve a gate's ``key_expr`` to the caller key. A ``$in`` ref resolves over the input
    (per-input-field key, e.g. ``$in.in.userId``); anything else (a literal, or ``$caller.token``)
    is the key verbatim — a single per-key bucket. Per-USER attribution (refining ``$caller.*`` to
    a real principal) is deferred until identity lands, not faked here."""
    if key_expr.startswith("$in"):
        try:
            return _stringify(_resolve_ref(key_expr, ports, node_id))
        except _RefError:
            return key_expr  # unresolvable → fall back to the literal bucket (no silent crash)
    return key_expr


async def execute_ratelimit(gates: Any, *, scope: str, limit: int, window_seconds: int) -> bool:
    """Count one hit and return True = ALLOW. Denies once the current window's count exceeds
    ``limit``. ``gates`` is the injected backend (the counter lives in Postgres). With no backend
    wired (no gate in the graph normally), allow — the gate is inert, never a hard fail."""
    if gates is None:
        return True
    count = await gates.hit(scope, window_seconds)
    return count <= limit


async def execute_quota(
    gates: Any, *, agent_id: str | None, budget_tokens: int, window_seconds: int
) -> bool:
    """Read accumulated token usage and return True = ALLOW (read, don't re-meter). Denies once
    usage reaches ``budget_tokens`` in the window. No backend → allow."""
    if gates is None:
        return True
    used = await gates.usage_tokens(agent_id, window_seconds)
    return used < budget_tokens


def _bind_gate(
    node: Node,
    allowed: bool,
    input_value: Any,
    reason: str,
    values: dict[tuple[str, str], Any],
    live_handles: dict[str, set[str]],
) -> None:
    """Activate ``allow`` (input through) or ``deny`` (a clean 429-style result — never a hang,
    gate). Exactly one handle, so the un-taken branch is dead (edge-skipping)."""
    if allowed:
        live_handles[node.id] = {"allow"}
        values[(node.id, "allow")] = input_value
    else:
        live_handles[node.id] = {"deny"}
        values[(node.id, "deny")] = {"denied": True, "reason": reason}


# ── conditional dataflow: edge liveness, node skipping ───────────────────────


def _success_handles(node: Node) -> set[str]:
    """The node's non-error out-handles — where a normal result is bound (input/llm/tool-ok)."""
    return {p.id for p in node.ports.out if p.type != "error"}


def _error_handles(node: Node) -> set[str]:
    """The node's ``error``-typed out-handles — where a tool's structured error is bound."""
    return {p.id for p in node.ports.out if p.type == "error"}


# The reserved key a failed node's error is recorded under when it declares NO error-typed
# out-port. Never a declared handle, so no edge can read it — it exists solely so the honest
# empty-output reason can name the CAUSE instead of letting the error message vanish.
_DROPPED_ERROR_KEY = "__dropped_error__"

# The reserved key an llm node's accumulated thinking is recorded under, surfaced in the node's
# captured outputs as the ``reasoning`` entry. Never a declared handle, so no edge can read it —
# reasoning is observability payload (persisted via node_io, same capture gating/caps as every
# other port), never graph-visible dataflow.
_NODE_REASONING_KEY = "__reasoning__"

# The reserved key an llm node's autonomous tool-calling loop records its calls under, surfaced in
# the node's captured outputs as the ``tool_calls`` entry: one record per call {name, arguments, ok,
# result, iteration, index}. Like ``reasoning`` it is observability payload only (same node_io
# gating/caps), never a dataflow handle — it lets the run inspector show WHAT each tool returned,
# which otherwise lives only in the transient conversation the model never surfaces. ``iteration``
# + ``index`` mirror the ``tool.<name>#<iter>.<idx>`` phase-span id so the inspector can scope a
# single tool row to its own record.
_NODE_TOOL_CALLS_KEY = "__tool_calls__"


def _tool_call_record(
    call: ToolCall, outcome: ActivityOutcome, iteration: int, index: int
) -> dict[str, Any]:
    """One captured record of an autonomous tool call + its result, for the node's ``tool_calls``
    observability entry. ``result`` is the raw ok/err value the tool returned (the same value fed
    back to the model), rendered by the inspector; capping/truncation rides the node_io cap."""
    return {
        "name": call.name,
        "arguments": call.arguments,
        "ok": outcome.ok,
        "result": outcome.value,
        "iteration": iteration,
        "index": index,
    }


def _bind_outcome(
    node: Node,
    outcome: ActivityOutcome,
    values: dict[tuple[str, str], Any],
    live_handles: dict[str, set[str]],
) -> None:
    """Bind a ``tool``/``mcp_tool`` :class:`ActivityOutcome` to the node's handles: the success
    value to the non-error handles on ``ok``, the error message to the ``err`` handle(s)
    otherwise — activating exactly the taken handle set so the un-taken branch's edges are dead.
    Shared by the interactive walker and (conceptually) the durable compiler's value threading."""

    handles = _success_handles(node) if outcome.ok else _error_handles(node)
    live_handles[node.id] = handles
    for handle in handles:
        values[(node.id, handle)] = outcome.value
    if not outcome.ok and not handles:
        # The node failed but declares no error-typed out-port: the error has nowhere to bind.
        # Record it under the reserved key so finalize_empty_reason can surface the cause.
        values[(node.id, _DROPPED_ERROR_KEY)] = outcome.value


def _edge_live(edge: Edge, skipped: set[str], live_handles: dict[str, set[str]]) -> bool:
    """An edge carries a value this run iff its source executed (not ``skipped``) AND the source
    activated that out-handle. A ``router`` activates only its selected handle; a ``tool``
    activates ``ok`` xor ``err``; everything else activates all its (non-error) handles. So edges
    from an un-taken branch are dead — the substrate of router/tool branching."""

    if edge.source in skipped:
        return False
    live = live_handles.get(edge.source)
    if live is None:  # source not (yet) executed — topo order makes this only the unreached case
        return False
    return edge.source_handle in live


def _is_skipped(
    node: Node, edges: list[Edge], skipped: set[str], live_handles: dict[str, set[str]]
) -> bool:
    """A node is skipped iff it declares inbound ``data`` edges and *every* one is dead. The
    ``input`` boundary has no inbound edges and is never skipped; a node with only ``control``
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
    ``targetHandle`` (in-port collection). Collects **several** named producers instead of assuming
    one. Dead edges (an un-taken branch this run) are ignored, so a port fed only by a dead branch
    is absent (resolves to ``None``). Validation already rejected two ``data`` edges to one port, so
    no override ambiguity. ``declared`` carries every in-port the node declares, so ``$in.<port>``
    can tell an undeclared port (loud error) from a declared-but-absent one (``None``)."""

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
    the output / the forwarded value? — and is a **loud** error, the same no-silent-nonsense rule
    as the unknown-port path, never a silent pick of the default port.

    Multi-input (``$in.<port>``) is for the value-**producing** nodes — ``llm``/``tool``/
    ``mcp_tool`` — which READ several ports into their own output; ``output``/``router`` consume a
    single value. (A ``router`` that needs to branch on one of several inputs is the deferred
    multi-port-router fork, not a silent default-port pick.)"""

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
    """Walk ``ir`` node by node, yielding ``llm`` deltas as they stream.

    Assumes a *validated* IR (the control-plane runs ``validate_graph`` + the up-front engine/tool
    checks before creating the ``Run``, so an invalid graph never persists). Threads ``data`` edges
    as values through a ``(node_id, out_handle) -> value`` map; tracks per-node *live* out-handles
    so router/tool branching skips un-taken edges. Each node's ``id`` is stamped on a structured
    log keyed by ``run_id`` (``skipped: true|false``) — the OTel attach-point, not OTel itself.
    The executed ``output`` node's value is written to ``result`` (the run's canonical output)."""

    values: dict[tuple[str, str], Any] = {}
    skipped: set[str] = set()
    live_handles: dict[str, set[str]] = {}

    # tool/mcp_tool nodes wired as CAPABILITIES (the source of a `tool`-channel edge) are not run
    # as standalone steps — the model calls them lazily inside the llm loop (build_capability_
    # schemas + execute_tool_call). Skip them in the topo walk. (topological_order already drops
    # `tool` edges, so they don't sequence anything; this stops the node body from executing.)
    capability_nodes = {e.source for e in ir.edges if e.channel == "tool"}

    for node in topological_order(ir):
        if node.id in capability_nodes:
            continue
        if _is_skipped(node, ir.edges, skipped, live_handles):
            skipped.add(node.id)
            _log_node(ctx, node, skipped=True)
            # A skipped (dead-branch) node is a zero-width grey bar on the waterfall.
            if ctx.run_trace is not None:
                await ctx.run_trace.skipped(node)
            continue
        _log_node(ctx, node, skipped=False)

        # Capture the resolved in-port values BEFORE running the node (upstream values are already
        # bound — topo order) so the span's node_io shows what each node RECEIVED.
        io_inputs = _io_input_snapshot(node, ir.edges, values, skipped, live_handles)
        # The one wrapper: each executed node runs inside a span; on close it writes the span row +
        # node_io per the effective capture policy. A no-op CM when telemetry is unwired.
        async with _open_node_span(ctx, node) as scope:
            if node.kind == "boundary":
                _walk_boundary(node, ir, input_value, values, skipped, live_handles, result)
            elif node.kind == "activity":
                if node.type == "llm":
                    gen = _walk_llm(node, ir, ctx, values, skipped, live_handles, result, scope)
                    async for delta in gen:
                        yield delta
                elif node.type == "tool":
                    await _walk_tool(node, ir, ctx, values, skipped, live_handles)
                elif node.type == "mcp_tool":
                    await _walk_mcp_tool(node, ir, ctx, values, skipped, live_handles)
                elif node.type == "guardrail":  # model guardrail (rule⇒orchestration branch below)
                    await _walk_guardrail_model(node, ir, ctx, values, skipped, live_handles, scope)
                elif node.type in ("ratelimit", "quota"):
                    await _walk_gate(node, ir, ctx, values, skipped, live_handles)
                elif node.type == "transcribe":
                    await _walk_transcribe(node, ir, ctx, values, skipped, live_handles)
                elif node.type == "speak":
                    await _walk_speak(node, ir, ctx, values, skipped, live_handles)
                elif node.type == "imagine":
                    await _walk_imagine(node, ir, ctx, values, skipped, live_handles)
                elif node.type == "rag":
                    await _walk_rag(node, ir, ctx, values, skipped, live_handles)
                else:
                    # agent / retriever / memory / code — deferred.
                    raise NotImplementedError(
                        f"activity node {node.id!r} (type {node.type!r}) is not implemented yet"
                    )
            elif node.kind == "orchestration":
                if node.type == "router":
                    _walk_router(node, ir, values, skipped, live_handles)
                elif node.type == "transform":
                    _walk_transform(node, ir, values, skipped, live_handles)
                elif node.type == "guardrail":  # rule guardrail (model⇒activity branch above)
                    _walk_guardrail_rule(node, ir, values, skipped, live_handles)
                elif node.type in ("loop", "map"):
                    # Durable-only lowerings (bounded repetition / durable fan-out) — rejected up
                    # front by the run endpoints; this raise is the direct-caller backstop.
                    raise NotImplementedError(
                        f"orchestration node {node.id!r} (type {node.type!r}) is durable-only — "
                        "run it on the durable runtime (a durable run or trigger), not the "
                        "interactive path"
                    )
                else:
                    # condition / iterator — declared in the IR schema but not executable yet.
                    raise NotImplementedError(
                        f"orchestration node {node.id!r} (type {node.type!r}) "
                        "is not implemented yet"
                    )
            if scope is not None:  # record what the node received + emitted, and its ok/err status
                scope.set_io(
                    inputs=io_inputs, outputs=_io_output_snapshot(node, values, live_handles)
                )
                scope.set_status(_node_span_status(node, live_handles))

    if result is not None and result.empty_reason is None:
        result.empty_reason = finalize_empty_reason(
            ir,
            output=result.output,
            output_produced=result.output_produced,
            truncated_empty_nodes=result.truncated_empty_nodes,
            skipped=skipped,
            live_handles=live_handles,
            values=values,
        )


def _node_error_cause(
    ir: IRDocument, node_id: str, values: dict[tuple[str, str], Any] | None
) -> str | None:
    """The error message a failed node bound (to its ``err`` handle, or to the reserved
    dropped-error key when it declares none) — so the honest empty-output reason can carry the
    CAUSE, not just the node id. ``None`` when unavailable (older call sites pass no values)."""
    if values is None:
        return None
    node = next((n for n in ir.nodes if n.id == node_id), None)
    if node is None:
        return None
    for handle in (_DROPPED_ERROR_KEY, *sorted(_error_handles(node))):
        if (node_id, handle) in values:
            value = values[(node_id, handle)]
            text = value if isinstance(value, str) else json.dumps(value, default=str)
            return text if len(text) <= 300 else text[:300] + "…"
    return None


def finalize_empty_reason(
    ir: IRDocument,
    *,
    output: Any,
    output_produced: bool,
    truncated_empty_nodes: list[str],
    skipped: set[str],
    live_handles: dict[str, set[str]],
    values: dict[tuple[str, str], Any] | None = None,
) -> str | None:
    """The honest-empty-output reason, single-sourced so the interactive walk AND the durable
    compiler compute it identically. If the walk finished with NO output node having run and some
    node short-circuited to its ``err`` handle, the run reached no output *because* of that
    handled-but-unconsumed error (its ``ok`` path to the output was skipped) — name it (and the
    error message, when ``values`` is provided) so the control-plane doesn't report a green
    ``completed`` masquerading as success. A graph that declares no output node, or whose taken
    branch reaches none, gets an honest note too — never a silent blank. If the output node ran
    but is blank because an ``llm`` spent its whole budget (a reasoning model thinking, or
    ``maxTokens`` too low), name that instead of a green blank. Returns ``None`` when the output
    is legitimately present. Does NOT change the error-as-structured-output contract."""

    if not output_produced:

        def _errored(node: Node) -> bool:
            live = live_handles.get(node.id)
            if live is None or node.id in skipped:
                return False  # never executed — not the cause
            if live:
                return live <= _error_handles(node)  # only its error handle(s) activated
            # An EMPTY live set on an executed activity (or a composed subgraph) means its
            # failure had nowhere to bind (no error-typed out-port declared) — the error would
            # otherwise vanish and the run would read as a green success with an empty output.
            # The output boundary is the one node that legitimately activates nothing.
            return node.kind == "activity" or node.type == "subgraph"

        errored = [node.id for node in ir.nodes if _errored(node)]
        if errored:
            reason = f"output empty: upstream error on node {errored[0]!r}"
            cause = _node_error_cause(ir, errored[0], values)
            return f"{reason}: {cause}" if cause else reason
        if not any(n.type == "output" for n in ir.nodes):
            return "output empty: the graph declares no output node"
        return (
            "output empty: no output node executed this run "
            "(the taken branch reaches no output node)"
        )
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


# ── observability glue (the one-wrapper seam) ────────────────────────────────
# Thin, additive helpers so the walk wraps each node in a span and captures its I/O WITHOUT changing
# any handler signature or the dispatch. When ``ctx.run_trace`` is unset the span CM is a no-op.


@contextlib.asynccontextmanager
async def _null_acm() -> AsyncIterator[None]:
    """A no-op async CM yielding ``None`` — used when no :class:`RunTrace` is wired."""
    yield None


def _open_node_span(ctx: WalkContext, node: Node) -> Any:
    """The node-span context manager (a real one when telemetry is wired, else a no-op)."""
    if ctx.run_trace is not None:
        return ctx.run_trace.node_span(node)
    return _null_acm()


def _io_input_snapshot(
    node: Node,
    edges: list[Edge],
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> dict[str, Any]:
    """The node's RESOLVED per-in-port input values (post-``$in`` — they ARE the upstream outputs
    the
    node consumes), for ``node_io.inputs``. Multi-input → one entry per named in-port."""
    return dict(_collect_in_ports(node, edges, values, skipped, live_handles).values)


def _io_output_snapshot(
    node: Node, values: dict[tuple[str, str], Any], live_handles: dict[str, set[str]]
) -> dict[str, Any]:
    """The node's emitted per-out-handle values, for ``node_io.outputs``. A router/tool emits on
    exactly the taken handle(s); an llm/input on its success handle(s); an output node emits nothing
    (its input IS the run output, captured as the input snapshot). An llm node that produced
    thinking also carries it under the reserved ``reasoning`` entry — capture-only (it rides the
    same per-port gating/caps), never a handle a downstream edge can read."""
    out = {handle: values.get((node.id, handle)) for handle in live_handles.get(node.id, set())}
    reasoning = values.get((node.id, _NODE_REASONING_KEY))
    if reasoning is not None:
        out["reasoning"] = reasoning
    # An llm node's autonomous tool calls + their results ride the same reserved-entry channel as
    # reasoning — capture-only, so the inspector can show what each tool returned (never a handle).
    tool_calls = values.get((node.id, _NODE_TOOL_CALLS_KEY))
    if tool_calls:
        out["tool_calls"] = tool_calls
    return out


def _node_span_status(node: Node, live_handles: dict[str, set[str]]) -> str:
    """``err`` if the node bound ONLY its error handle(s) (a tool/mcp_tool structured error — the
    run continues but the node itself errored, so the bar is red), else ``ok``. A node that raised
    is marked ``err`` by the span CM's exception path instead."""
    live = live_handles.get(node.id) or set()
    if live and live <= _error_handles(node):
        return "err"
    return "ok"


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
        # Bind the run's input to the input node's out-handles.
        live_handles[node.id] = _success_handles(node)
        for handle in live_handles[node.id]:
            values[(node.id, handle)] = input_value
    elif node.type == "output":
        # The output node's in-port value IS the run's canonical output — read from a live edge,
        # so a routed run returns whichever branch actually produced it.
        live_handles[node.id] = set()
        ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
        value = _single_in_value(ports, node)
        if result is not None:
            if result.output_produced:
                # Two output nodes both executed: the run's canonical output would silently be
                # whichever topo order visits last. Ambiguity is a loud failure, never a silent
                # pick — same rule as _single_in_value.
                raise TemplateError(
                    f"node {node.id!r}: a second output node executed — the run output would be "
                    "ambiguous. Route exclusive branches (router/guardrail) so at most one "
                    "output node is live per run."
                )
            result.output = value
            result.output_produced = True  # an output node ran
    else:
        # human / subgraph lower onto the durable runtime (a durable wait / a child workflow) —
        # this interactive walker cannot run them. The run endpoints reject these up front
        # (durable_required); this raise is the backstop for direct walk() callers.
        raise NotImplementedError(
            f"boundary node {node.id!r} (type {node.type!r}) is durable-only — run it on the "
            "durable runtime (a durable run or trigger), not the interactive path"
        )


async def _walk_llm(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
    result: WalkResult | None = None,
    scope: SpanScope | None = None,
) -> AsyncIterator[Delta]:
    config = LlmConfig.model_validate(node.config)
    model_id, params = resolve_model(ir, config)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # Naive full replay: prior session turns verbatim, then this turn's rendered prompt. The prompt
    # may compose several in-ports — $in.file AND $in.question — via the port map.
    messages = [*ctx.prior_messages, *_render_messages(node, config, ports)]

    # Autonomous tool-calling. If the node offers tools, build the OpenAI function schemas
    # (function names == ir.tools keys, so a tool_call maps straight back to a binding) + the
    # tool_choice ONCE, then run a BOUNDED loop: stream one model turn (the shared `execute_llm`
    # body), and if it emits tool_calls, execute each via the EXISTING executors, append the
    # assistant request + each {role:tool} result, and call again — until the model answers or
    # `maxToolIterations` is hit. With no tools this is exactly ONE turn, byte-identical to the
    # pre-tool-calling path. The streaming/parse body lives ONCE in `execute_llm`, wrapped here
    # (interactive) and in the durable compiler (parity). Each turn streams inside a
    # `model.generate` phase span. The available-tool set is the UNION of the ir.tools keys
    # (config.tools) and the tool/mcp_tool nodes wired to this llm's `tools` port (capability
    # edges). Capability schemas use the NODE id as the function name; ir.tools schemas use the key.
    # Both dispatch through the same execute_tool_call. Empty union → single-shot.
    cap_nodes = _capability_tool_nodes(ir, node.id)
    schemas: list[dict[str, Any]] = []
    if config.tools:
        schemas += await build_tool_schemas(
            ir, config.tools, registry=ctx.tools, mcp=ctx.mcp, tool_auth=ctx.tool_auth
        )
    if cap_nodes:
        schemas += await build_capability_schemas(
            cap_nodes, registry=ctx.tools, mcp=ctx.mcp, tool_auth=ctx.tool_auth
        )
    has_tools = bool(schemas)
    tool_schemas: list[dict[str, Any]] | None = schemas if has_tools else None
    tool_choice: Any = _to_openai_tool_choice(config.tool_choice) if has_tools else None

    gen_cm = scope.child_phase("model.generate") if scope is not None else _null_acm()
    first_token_ns: int | None = None
    final_output = ""
    final_finish: str | None = None
    usage_acc: dict[str, int] | None = None
    reasoning_parts: list[str] = []  # per-turn thinking → the node's captured `reasoning` entry
    tool_call_records: list[dict[str, Any]] = []  # calls + results → the `tool_calls` entry
    truncated = False
    capped = False
    async with gen_cm as gen_scope:
        iteration = 0
        while True:
            # One streaming model turn, bridged from `execute_llm`'s on_delta into this generator's
            # yields via a cooperative queue/task (so the streaming body is shared, not duplicated).
            deltas: asyncio.Queue[Delta | None] = asyncio.Queue()

            def _on_delta(content: str, kind: str, q: asyncio.Queue[Delta | None] = deltas) -> None:
                q.put_nowait(
                    Delta(
                        node_id=node.id,
                        content=content,
                        kind="reasoning" if kind == "reasoning" else "content",
                    )
                )

            async def _turn(
                q: asyncio.Queue[Delta | None] = deltas,
                tc: Any = tool_choice,  # bound at definition — the loop rebinds tool_choice
            ) -> LlmActivityResult:
                # A pre-stream 503/404 or a mid-stream death raises here and re-raises at
                # `await task` → failed run, as the inline loop did.
                try:
                    return await execute_llm(
                        ctx.gateway,
                        model_id=model_id,
                        params=params,
                        messages=messages,
                        extra_headers=ctx.extra_headers,
                        on_delta=_on_delta,
                        tools=tool_schemas,
                        tool_choice=tc,
                    )
                finally:
                    q.put_nowait(None)  # sentinel: producer done (success OR error)

            task = asyncio.create_task(_turn())
            try:
                while True:
                    d = await deltas.get()
                    if d is None:
                        break
                    if d.kind != "reasoning" and first_token_ns is None:
                        first_token_ns = now_ns()  # ttft = first ANSWER token (not thinking)
                    yield d
                turn = await task
            finally:
                # Consumer stopped early (client disconnect → GeneratorExit) or an error propagated:
                # cancel the in-flight turn so the upstream stream isn't leaked.
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task

            final_finish = turn.finish_reason
            usage_acc = merge_usage(usage_acc, turn.usage)  # node total across tool-loop turns
            if turn.reasoning:
                reasoning_parts.append(turn.reasoning)
            # No tool calls (or the node has no tools — legacy or capability) → answer is final.
            if not (turn.tool_calls and has_tools):
                final_output = turn.output
                truncated = turn.truncated_empty
                break
            # Hit the cap with the model still wanting tools → stop honestly (no blank-success).
            if iteration >= config.max_tool_iterations:
                capped = True
                final_output = turn.output
                break
            # Execute the requested tools, feeding the assistant request + each result back so the
            # next turn sees them (the OpenAI loop shape). A tool error is fed back, not fatal
            # (a tool error is fed back, not fatal — structured ok/err contract).
            messages.append(assistant_tool_calls_message(list(turn.tool_calls)))
            for ci, call in enumerate(turn.tool_calls):
                yield Delta(
                    node_id=node.id, content=call.name, kind="tool_call"
                )  # visible progress
                # Each tool call is a child span under the llm node span, so the waterfall shows
                # the autonomous loop's tool steps inside the model node. The phase id carries
                # iteration + call index — the span PK is deterministic per (run, node, phase), so
                # repeat calls to ONE tool need a discriminator or every row after the first is
                # dropped by the idempotent insert. The display name stays `tool.<name>`.
                tool_cm = (
                    scope.child_phase(f"tool.{call.name}#{iteration}.{ci}", f"tool.{call.name}")
                    if scope is not None
                    else _null_acm()
                )
                async with tool_cm as tool_scope:
                    outcome = await execute_tool_call(
                        ir,
                        call,
                        registry=ctx.tools,
                        mcp=ctx.mcp,
                        tool_auth=ctx.tool_auth,
                        run_id=ctx.run_id,
                        node_id=node.id,
                        idempotency_suffix=f"{node.id}-{iteration}-{call.id}",
                        rag=ctx.rag,
                    )
                    if tool_scope is not None:
                        tool_scope.set_attributes({"tool.name": call.name, "tool.ok": outcome.ok})
                tool_call_records.append(_tool_call_record(call, outcome, iteration, ci))
                messages.append(tool_result_message(call, outcome))
            # A FORCED tool_choice ("required" / a named function) applies to the FIRST turn
            # only — re-sending it would force a tool call on every turn, so the model could
            # never produce a final answer and a write tool would re-execute until the cap.
            if tool_choice is not None and tool_choice != "auto":
                tool_choice = "auto"
            iteration += 1

        # GenAI-semconv scalars on the generate span (time-to-first-token, finish reason, model,
        # token usage). Usage lands on THIS span only (never mirrored onto the node span) — the
        # quota gate SUMS the attribute across a run's spans, so a mirror would double-count.
        if gen_scope is not None:
            attrs: dict[str, Any] = {"gen_ai.request.model": model_id}
            if final_finish is not None:
                attrs["gen_ai.response.finish_reason"] = final_finish
            if first_token_ns is not None:
                attrs["ttft_ms"] = round((first_token_ns - gen_scope.span.start_ns) / 1e6, 1)
            attrs.update(usage_attributes(usage_acc))
            gen_scope.set_attributes(attrs)

    # Mirror the model id onto the node span too, so the waterfall's left rail can label the llm
    # node without opening the phase child.
    if scope is not None:
        scope.set_attributes({"gen_ai.request.model": model_id})

    # Honest legibility: a truncated-empty answer, OR a tool loop that hit its cap without a final
    # answer, must not bind a green blank.
    if result is not None and (truncated or (capped and _is_blank(final_output))):
        result.truncated_empty_nodes.append(node.id)

    # The model's thinking, joined across tool-loop turns, recorded under the reserved key so the
    # node_io capture persists it alongside the answer — never a dataflow handle.
    if reasoning_parts:
        values[(node.id, _NODE_REASONING_KEY)] = "\n\n".join(reasoning_parts)

    # The tool calls this node made + what each returned, recorded under the reserved key so the run
    # inspector can show the tool RESULTS (they otherwise vanish into the transient conversation).
    if tool_call_records:
        values[(node.id, _NODE_TOOL_CALLS_KEY)] = tool_call_records

    # Activate the success handles and bind the final text, so downstream data edges pick it by
    # ``sourceHandle`` (e.g. "ok"). An llm error raises mid-stream (failed run) — it never binds.
    live_handles[node.id] = _success_handles(node)
    for handle in live_handles[node.id]:
        values[(node.id, handle)] = final_output


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
    is a *structured error*, not a run failure: bind the message to the ``err`` handle
    and continue — downstream ``err`` edges run, ``ok`` edges are skipped. (A non-200 HTTP status
    is a normal return value, bound to ``ok``; only transport failures raise.)"""

    config = ToolConfig.model_validate(node.config)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # Route the step by what ``config.tool`` resolves to: an ``http`` binding in ``ir.tools`` → a
    # real outbound call with connection-injected auth (server-side, at step time); a ``builtin``
    # binding → its registered ``ref``; a directly-registered builtin name → itself. An unresolvable
    # ``$in`` ref is a structured err either way: bind ``err``.
    binding = is_http_tool(ir, config.tool)
    if binding is not None:
        outcome = await run_http_tool(
            binding, config, ports, node_id=node.id, run_id=ctx.run_id, resolver=ctx.tool_auth
        )
    else:
        # A ``builtin`` binding names its callable via ``ref`` (the ir.tools key is a logical name);
        # a bare ``config.tool`` is itself a registered builtin. An ``mcp`` binding is the wrong
        # shape for a step-mode tool node (that is the ``mcp_tool`` node's job) — the up-front check
        # rejects it, so it never reaches here.
        ir_binding = ir.tools.get(config.tool)
        builtin_name = ir_binding.ref if isinstance(ir_binding, BuiltinTool) else config.tool
        try:
            args = {k: _resolve_ref(v, ports, node.id) for k, v in config.args.items()}
        except Exception as exc:
            outcome = ActivityOutcome(ok=False, value=str(exc))
        else:
            outcome = await execute_tool(ctx.tools, builtin_name, args)
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


def resolve_rag_query(config: RagConfig, ports: _PortInputs, node_id: str) -> str:
    """The step-mode query: ``config.query`` rendered through the shared ``$in`` grammar when
    authored; otherwise the default in-port's whole value. A non-string upstream value is
    JSON-serialized (retrieval wants text). Shared by both runtimes so the durable lowering
    resolves queries identically."""
    if config.query is not None:
        return _render_template(config.query, ports, node_id)
    value = _resolve_ref("$in", ports, node_id)
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


async def _walk_rag(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Step-mode ``rag`` node: resolve the query (config template or the default in-port), run
    the retrieval through the injected backend, and bind matches to the ok handle(s). Every
    failure — unresolvable query ref, unknown source, nothing ingested yet — is a *structured
    error* bound to ``err``; the run continues (the tool ok/err contract)."""

    config = RagConfig.model_validate(node.config)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    try:
        query = resolve_rag_query(config, ports, node.id)
    except Exception as exc:  # an unresolvable $in ref is a structured err, like a tool arg
        outcome = ActivityOutcome(ok=False, value=str(exc))
    else:
        outcome = await execute_rag(
            ctx.rag,
            source=config.source,
            query=query,
            top_k=rag_top_k(node.config, config),
            min_similarity=config.min_similarity,
        )
    _bind_outcome(node, outcome, values, live_handles)
    if not outcome.ok:
        logger.info(
            "graph.rag_error",
            extra={
                "run_id": ctx.run_id,
                "node_id": node.id,
                "source": config.source,
                "error": outcome.value,
            },
        )


async def _invoke_mcp(mcp: McpManager, server: str, tool: str, args: dict[str, Any]) -> McpResult:
    """THE single chokepoint every ``mcp_tool`` invocation passes through. This is the future
    governance attach-point — policy-as-code and audit logging hook in HERE, observing the
    *invocation* (which server, which tool, the arg shape), never the contents the server returns
    (the redaction posture). A policy layer is a separate, future addition; today this
    simply forwards to the MCP manager. Both the interactive walker (via ``invoke_mcp_tool``)
    and the durable step (via ``execute_mcp_tool``) pass through this one function, so governance
    lands once for both runtimes."""

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
    """Invoke an external MCP server's tool — the same ok/err contract as a local ``tool``, only the
    transport differs. Template ``args`` with the ``$in`` resolver, call through the
    ``invoke_mcp_tool`` chokepoint, bind the result to ``ok``. A tool-level error
    (``McpResult.is_error``), a transport failure after one reconnect-retry, an unknown tool, or an
    unresolvable arg ref all bind ``err`` and the run continues.

    **The err payload must be actionable**, not just present: the message names the *server* and the
    *kind* of failure (connection vs the server's own tool error vs unknown tool), so the agent
    reading ``err`` can decide its next move ("the file server is down" vs "that tool doesn't
    exist"). A bare OS error or stack blob would pass "err bound" but fail that bar."""

    config = McpToolConfig.model_validate(node.config)
    tool = config.tool
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # Resolve args deterministically, then run the shared activity body (the same body the durable
    # step wraps). A ``connection``-based node resolves its connection server-side (auth never in
    # the IR) and uses the http/stdio transport; a ``server``-based node is the registered-name
    # path. A bad arg ref is itself a structured err.
    try:
        args = {k: _resolve_ref(v, ports, node.id) for k, v in config.args.items()}
    except Exception as exc:
        target = config.connection or config.server
        outcome = ActivityOutcome(ok=False, value=f"mcp {target!r} tool {tool!r} failed: {exc}")
    else:
        outcome = await _mcp_tool_outcome(ctx, config, tool, args)
    _bind_outcome(node, outcome, values, live_handles)
    if not outcome.ok:
        logger.info(
            "graph.mcp_error",
            extra={
                "run_id": ctx.run_id,
                "node_id": node.id,
                "target": config.connection or config.server,
                "tool": tool,
                "error": outcome.value,
            },
        )


async def _mcp_tool_outcome(
    ctx: WalkContext, config: McpToolConfig, tool: str, args: dict[str, Any]
) -> ActivityOutcome:
    """Dispatch an mcp_tool call by binding kind: ``connection`` → resolve the connection
    server-side + use its transport; ``server`` → the registered-name path. Shared by the
    interactive + durable mcp handlers."""
    if config.connection:
        if ctx.tool_auth is None:
            return ActivityOutcome(ok=False, value="mcp_tool: no connection resolver configured")
        try:
            conn = await ctx.tool_auth(config.connection)
        except Exception as exc:  # e.g. an undecryptable secret — honest err, never a run failure
            return ActivityOutcome(ok=False, value=f"mcp connection {config.connection!r}: {exc}")
        if conn is None:
            return ActivityOutcome(
                ok=False, value=f"mcp connection {config.connection!r} not found or disabled"
            )
        try:
            mcp_config = await mcp_config_from_connection(conn, connection_id=config.connection)
        except Exception as exc:  # e.g. a malformed secret map / token fetch failure
            return ActivityOutcome(ok=False, value=f"mcp connection {config.connection!r}: {exc}")
        return await execute_mcp_connection_tool(ctx.mcp, config.connection, mcp_config, tool, args)
    return await execute_mcp_tool(ctx.mcp, config.server or "", tool, args)


async def _walk_transcribe(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Run a ``transcribe`` node: the audio-ref in-port value → text. Binds text to the success
    handle(s), an err message to ``err`` (the tool ok/err contract). The bytes go to the inference
    base URL (gateway), never a control-plane route."""
    config = TranscribeConfig.model_validate(node.config)
    model_id, binding_params = resolve_model_key(ir, config.model)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    audio_ref = _single_in_value(ports, node)
    outcome = await execute_transcribe(
        ctx.gateway,
        ctx.artifacts,
        model_id=model_id,
        params={**binding_params, **config.params},
        audio_ref=audio_ref,
        extra_headers=ctx.extra_headers,
    )
    _bind_outcome(node, outcome, values, live_handles)


async def _walk_speak(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Run a ``speak`` node: the text in-port value → an audio REFERENCE (the bytes are a stored
    artifact, not journaled). Binds the ref to the success handle(s), an err to err."""
    config = SpeakConfig.model_validate(node.config)
    model_id, binding_params = resolve_model_key(ir, config.model)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    text = _single_in_value(ports, node)
    outcome = await execute_speak(
        ctx.gateway,
        ctx.artifacts,
        model_id=model_id,
        params={**binding_params, **config.params},
        text=text if isinstance(text, str) else _stringify(text),
        extra_headers=ctx.extra_headers,
    )
    _bind_outcome(node, outcome, values, live_handles)


async def _walk_imagine(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Run an ``imagine`` node: the text in-port value → an image REFERENCE (the bytes are a stored
    artifact, not journaled). Binds the ref to the success handle(s), an err to err."""
    config = ImagineConfig.model_validate(node.config)
    model_id, binding_params = resolve_model_key(ir, config.model)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    prompt = _single_in_value(ports, node)
    outcome = await execute_imagine(
        ctx.gateway,
        ctx.artifacts,
        model_id=model_id,
        params={**binding_params, **config.params},
        prompt=prompt if isinstance(prompt, str) else _stringify(prompt),
        extra_headers=ctx.extra_headers,
    )
    _bind_outcome(node, outcome, values, live_handles)


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
    activate only that handle and forward the in-port value along it. Handle-name routing: the
    upstream node chose the branch; the router executes the choice. A select that can't resolve, or
    names a handle the router doesn't declare, is a :class:`RouterError` (failed run, clear reason)
    — no fallback guessing."""

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


def _walk_transform(
    node: Node,
    ir: IRDocument,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Run a ``transform`` node: reshape the in-port value(s) by the JSON template ``config.expr``
    (deterministic, inline — no I/O) and bind the result to the success handle(s). A bad expr /
    unresolvable ref raises :class:`TransformError` (failed run), like the router."""
    config = TransformConfig.model_validate(node.config)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    value = execute_transform(config.expr, ports, node.id)
    live_handles[node.id] = _success_handles(node)
    for handle in live_handles[node.id]:
        values[(node.id, handle)] = value


def _walk_guardrail_rule(
    node: Node,
    ir: IRDocument,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Run a RULE guardrail (orchestration kind): evaluate the deterministic predicate over the
    in-port value (no I/O) and activate ``pass`` (input through) or ``block`` (the onBlock
    payload). The rule sub-config is guaranteed present (validated in packages/ir)."""
    config = GuardrailConfig.model_validate(node.config)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    input_value = _single_in_value(ports, node)
    assert config.check.rule is not None  # validate_graph guarantees this for a rule check
    passed = evaluate_guardrail_rule(config.check.rule, input_value)
    _bind_guardrail(node, passed, input_value, config.on_block, values, live_handles)


async def _walk_guardrail_model(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
    scope: SpanScope | None = None,
) -> None:
    """Run a MODEL guardrail (activity): a cheap classifier call decides pass/block.
    The model sub-config is guaranteed present (validated in packages/ir); the model id resolves
    like an ``llm`` (engine-name rejected). Then the same pass/block binding as the rule path."""
    config = GuardrailConfig.model_validate(node.config)
    mcfg = config.check.model
    assert mcfg is not None  # validate_graph guarantees this for a model check
    model_id, params = resolve_model_key(ir, mcfg.model)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    input_value = _single_in_value(ports, node)
    # The classifier call runs inside a `model.generate` phase span, like an llm node's turn.
    # Its token usage lands on THAT span only (never mirrored onto the node span) — the quota
    # gate SUMS the attribute across a run's spans, so a mirror would double-count.
    gen_cm = scope.child_phase("model.generate") if scope is not None else _null_acm()
    async with gen_cm as gen_scope:
        result = await execute_guardrail_model(
            ctx.gateway,
            model_id=model_id,
            params=params,
            prompt=mcfg.prompt,
            input_value=input_value,
            extra_headers=ctx.extra_headers,
        )
        if gen_scope is not None:
            gen_scope.set_attributes(
                {"gen_ai.request.model": model_id, **usage_attributes(result.usage)}
            )
    _bind_guardrail(
        node,
        guardrail_model_passed(result.output, mcfg.pass_on),
        input_value,
        config.on_block,
        values,
        live_handles,
    )


async def _walk_gate(
    node: Node,
    ir: IRDocument,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Run a ``ratelimit`` or ``quota`` gate: resolve the caller key, check the backend, and
    activate ``allow`` (input through) or ``deny`` (a clean result). Both are activities (counter
    / usage reads = I/O). Per-key now; per-user attribution is deferred."""
    from theygent_ir import QuotaConfig, RateLimitConfig

    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    input_value = _single_in_value(ports, node)
    if node.type == "ratelimit":
        cfg = RateLimitConfig.model_validate(node.config)
        key = resolve_gate_key(cfg.key_expr, ports, node.id)
        scope = f"{ir.id}:{node.id}:{key}"
        allowed = await execute_ratelimit(
            ctx.gates, scope=scope, limit=cfg.limit, window_seconds=cfg.window_seconds
        )
        reason = f"rate limit exceeded ({cfg.limit}/{cfg.window_seconds}s)"
    else:  # quota
        qcfg = QuotaConfig.model_validate(node.config)
        allowed = await execute_quota(
            ctx.gates,
            agent_id=ir.id,
            budget_tokens=qcfg.budget_tokens,
            window_seconds=qcfg.window_seconds,
        )
        reason = f"token budget exceeded ({qcfg.budget_tokens}/{qcfg.window_seconds}s)"
    _bind_gate(node, allowed, input_value, reason, values, live_handles)
