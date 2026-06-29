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
    IRDocument,
    LlmConfig,
    McpTool,
    McpToolConfig,
    Node,
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


class TransformError(ValueError):
    """A ``transform`` node (m19.md §2.9) could not reshape its input: its ``expr`` is not valid
    JSON, or a ``$in`` ref in the template doesn't resolve. Like :class:`RouterError` /
    :class:`TemplateError` this is a LOUD failure (→ failed run), never a silent empty reshape — the
    M9 no-silent-nonsense rule applied to the reshaper. (``transform`` has no ``err`` port; a bad
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
    # M19 §1.1: the connection resolver an http tool / connection-backed mcp_tool resolves auth
    # through, server-side at step time. ``None`` when no graph uses a connection (the control-plane
    # injects its DB-backed resolver). The walker never opens a DB session — the resolver owns it.
    tool_auth: ConnectionResolver | None = None
    # M19 §2.8: the gate backend (``ratelimit`` counter + ``quota`` usage-read). Injected by the
    # control-plane (it owns the DB); ``None`` when no graph uses a gate. Same injection discipline
    # as ``tool_auth``/``run_trace`` — the walker calls it, the backend owns the session.
    gates: Any = None  # gates.GateBackend
    # M19 §2.2: the artifact store ``transcribe``/``speak`` resolve audio references through (fetch
    # the input audio bytes / store the produced audio). Injected; ``None`` when no audio node runs.
    artifacts: Any = None  # artifacts.LocalArtifactStore
    # M17: the observability handle for this run (the control-plane opens it via
    # ``Telemetry.begin_run`` before walking). When set, the walker wraps each node in a span and
    # captures per-node I/O through it (the §4 one-wrapper seam); when ``None`` (telemetry not
    # wired)
    # the walk is byte-for-byte its pre-M17 self — the wrapper is purely additive. The walker still
    # does no run-state/thread DB I/O itself (the M5 seam rule); telemetry is an injected
    # side-channel.
    run_trace: RunTrace | None = None


_CAMEL_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def _lower_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """Lower IR generation params (camelCase per §8.4, e.g. ``maxTokens``) to the OpenAI-native
    snake_case the data plane speaks (``max_tokens``). This is part of the walker's IR→execution
    lowering, not a contract change: the IR stays camelCase (consistent with the §8.2 envelope),
    while the §9.1 seam stays OpenAI-compatible. A generic camel→snake conversion handles every
    generation param (``topP``→``top_p``, ``responseFormat``→``response_format``, …); single-word
    params (``temperature``, ``stop``, ``seed``) are unchanged."""

    return {_CAMEL_BOUNDARY.sub("_", k).lower(): v for k, v in params.items()}


def resolve_model_key(ir: IRDocument, model_key: str) -> tuple[str, dict[str, Any]]:
    """Resolve a ``models`` key to the (logical id, generation params) forwarded to the inference
    seam (§8.4). Raises :class:`EngineNameNotAllowed` if the binding's ``model`` is an engine name —
    the logical-id invariant, enforced before anything is sent. Shared by ``llm``
    (``resolve_model``), ``transcribe`` / ``speak``, and the model ``guardrail`` (M19 §2.2/§2.6)."""
    binding = ir.models[model_key]
    if binding.model in _ENGINE_NAMES:
        raise EngineNameNotAllowed(
            f"{binding.model!r} is an engine name, not a logical model id; "
            "the model binding must carry a logical id"
        )
    return binding.model, _lower_params(binding.params)


def resolve_model(ir: IRDocument, config: LlmConfig) -> tuple[str, dict[str, Any]]:
    """Resolve an ``llm`` node's bound model (§8.4) — a thin wrapper over
    :func:`resolve_model_key`."""
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
    """Every ``transcribe``/``speak`` node with its resolved (model id, params) — the control-plane
    rejects an engine-name binding before the ``Run`` (the M19 §1.3 logical-id-only guard, extended
    to audio). Resolution raises :class:`EngineNameNotAllowed`, mirroring ``llm_models``."""
    out: list[tuple[Node, str, dict[str, Any]]] = []
    for node in ir.nodes:
        if node.type == "transcribe":
            cfg: Any = TranscribeConfig.model_validate(node.config)
        elif node.type == "speak":
            cfg = SpeakConfig.model_validate(node.config)
        else:
            continue
        model_id, params = resolve_model_key(ir, cfg.model)
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
    """Every ``server``-based ``mcp_tool`` node with its (server name, tool name) in document order.
    The control-plane checks server membership up front (→ 400 ``mcp_server_not_found``, no Run)
    and — if that server is already connected — the tool against its cached capability list (§4).
    M19 §2.4 **connection**-based mcp_tool nodes are SKIPPED here — like the http tool, the
    connection resolves at step time (a dangling connection binds ``err``, not a 400 — §1.1)."""

    out: list[tuple[Node, str, str]] = []
    for node in ir.nodes:
        if node.type == "mcp_tool":
            cfg = McpToolConfig.model_validate(node.config)
            if cfg.server:
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


def _render_content(content: Any, ports: _PortInputs, node_id: str) -> Any:
    """Render one ``messages`` ``content`` against the port map. A plain string is rendered inline
    through the ``$in`` template (the pre-M20 path, unchanged). A list is the M20 multimodal form:
    each ``text`` part's ``text`` and each ``image_url`` part's ``url`` are ``$in``-templatable, so
    the image can come from a graph in-port (``$in.image``). The parts are emitted as plain
    OpenAI-shaped dicts (gateway/LiteLLM forward them verbatim); ``detail`` is included only when
    set, so a clean OpenAI ``image_url`` reaches the wire (no ``detail: null`` for a server)."""
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
    """Render every ``messages`` content field through the port-addressed ``$in`` template (m10.md
    §1.2). One llm node can now compose several upstreams in one prompt — ``$in.file`` AND
    ``$in.question`` — because the token addresses the node's named in-ports, not a single value.
    M20: a ``content`` may also be a list of multimodal parts (text + image_url) — see
    ``_render_content`` — so a graph drives a vision model; the template applies in each part."""

    return [
        {"role": msg.role, "content": _render_content(msg.content, ports, node.id)}
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


@dataclass(frozen=True)
class ToolCall:
    """One assembled tool call the model emitted (M21) — the OpenAI ``function`` shape after the
    streamed fragments are merged. ``name`` is a key in ``ir.tools`` (or a directly-registered
    builtin); ``arguments`` is the parsed function arguments — the ``$in`` substitution dict for an
    http tool, the call args for a builtin/mcp tool (m21.md §6 Q3)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ChunkDelta:
    """One parsed streaming chunk. M21 extends the M9 reasoning/content split with ``tool_calls`` —
    the raw per-chunk ``delta.tool_calls`` fragment list (each {index, id?, function:{name?,
    arguments?}}). A non-tool chunk has ``tool_calls=None`` — byte-identical to pre-M21."""

    reasoning: str | None
    content: str | None
    finish_reason: str | None
    tool_calls: list[Any] | None = None


def parse_chunk(chunk: Any) -> ChunkDelta:
    """The one streaming-chunk parser the ``execute_llm`` body uses (and, via it, both runtimes): a
    reasoning model emits ``reasoning_content`` (thinking) before ``content`` (the answer), kept
    distinct (m9.md reasoning split); M21 also surfaces ``tool_calls`` for the loop."""

    if not chunk.choices:
        return ChunkDelta(None, None, None)
    choice = chunk.choices[0]
    finish_reason = choice.finish_reason
    delta = choice.delta
    if delta is None:
        return ChunkDelta(None, None, finish_reason)
    return ChunkDelta(
        reasoning=getattr(delta, "reasoning_content", None),
        content=getattr(delta, "content", None),
        finish_reason=finish_reason,
        tool_calls=getattr(delta, "tool_calls", None),
    )


def _accumulate_tool_calls(acc: dict[int, dict[str, Any]], fragments: list[Any]) -> None:
    """Merge a chunk's streamed ``tool_calls`` fragments into ``acc`` keyed by index (M21): the id +
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
    """The serializable result of an ``llm`` activity (m13-dbos.md §2/§6): the assembled answer
    (the only thing journaled), the finish reason, whether the model truncated to empty (the m9.md
    §2.4 reason), and — M21 — any ``tool_calls`` the model emitted this turn (empty when none).
    Tokens are a non-durable side-channel that already streamed; this return value is the durable
    record, so a replayed step re-derives the same answer + tool calls without re-executing (§6)."""

    output: str
    finish_reason: str | None
    truncated_empty: bool
    tool_calls: tuple[ToolCall, ...] = ()


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
    """Run ONE ``llm`` model turn to completion, assembling the answer + any tool calls (m13-dbos.md
    §2; M21). ``on_delta`` (``(piece, kind)``) streams tokens as a side effect (the SSE relay /
    durable delta bus, §6) while only ``content`` accumulates into the output. ``tools`` /
    ``tool_choice`` offer the model functions to call; streamed ``tool_calls`` fragments are merged
    into the returned ``tool_calls``. This is ONE turn — it does NOT loop; the loop (execute tools →
    feed results back → call again) lives in the caller (the walker / durable compiler) so each turn
    stays a journalable step (m21.md §2). A pre-stream 503/404 raises from ``open_stream``; a
    mid-stream death raises (→ failed run)."""

    upstream = await gateway.open_stream(
        model=model_id,
        messages=messages,
        params=params,
        extra_headers=extra_headers,
        tools=tools,
        tool_choice=tool_choice,
    )
    output = ""
    finish_reason: str | None = None
    tool_acc: dict[int, dict[str, Any]] = {}
    async for chunk in upstream:
        parsed = parse_chunk(chunk)
        if parsed.finish_reason:
            finish_reason = parsed.finish_reason
        if parsed.reasoning and on_delta is not None:
            on_delta(parsed.reasoning, "reasoning")
        if parsed.content:
            output += parsed.content
            if on_delta is not None:
                on_delta(parsed.content, "content")
        if parsed.tool_calls:
            _accumulate_tool_calls(tool_acc, parsed.tool_calls)
    truncated_empty = finish_reason == "length" and not output.strip() and not tool_acc
    return LlmActivityResult(
        output=output,
        finish_reason=finish_reason,
        truncated_empty=truncated_empty,
        tool_calls=tuple(_finalize_tool_calls(tool_acc)),
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


def mcp_config_from_connection(conn: ResolvedConnection) -> McpServerConfig:
    """Build the MCP server config from an ``mcp_server`` connection (M19 §2.4), server-side. For
    ``http`` the secret becomes an auth header (bearer / api-key); for ``stdio`` it can be injected
    into the subprocess env under a connection-named key (``secretEnv``). The secret exists only
    here, never in the IR (§1.1)."""
    cfg = conn.config or {}
    transport = cfg.get("transport", "stdio")
    if transport == "http":
        headers: dict[str, str] = dict(cfg.get("headers") or {})
        if conn.secret:
            auth = cfg.get("auth") or {}
            if auth.get("type") == "api_key":
                headers[str(auth.get("header", "X-API-Key"))] = conn.secret
            else:  # default: bearer
                headers["Authorization"] = f"Bearer {conn.secret}"
        return McpServerConfig(transport="http", url=cfg.get("url"), headers=headers or None)
    env: dict[str, str] = dict(cfg.get("env") or {})
    secret_env = cfg.get("secretEnv")
    if conn.secret and isinstance(secret_env, str):
        env[secret_env] = conn.secret
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
    """Invoke a CONNECTION-BACKED MCP tool (M19 §2.4) — same ok/err contract as ``execute_mcp_tool``
    but the server is reached via the connection's resolved ``config`` (transport + server-side
    auth) keyed by ``name``. The chokepoint + retry live in ``call_connection_tool``."""
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


# ── M19 §2.2: the audio model-call activities (transcribe · speak) ───────────────────────────────
#
# The only NEW model-call types M19 adds (chat/vision stay on ``llm``). Both map 1:1 to an OpenAI
# audio endpoint and route to a binding whose ``capabilities.modalities`` includes the matching key
# (the inference plane enforces logical-id-only — an engine name is rejected at the seam). The audio
# bytes go to the **inference base URL** (the gateway), in the user's trust domain — never a
# control-plane route (§10 / M18 §1.4). Non-text payloads are REFERENCES (artifacts), not journaled.

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
    """Run a ``transcribe`` activity (m19.md §2.2) — audio-ref in → text out. Fetches the audio
    bytes from the reference (artifact id / url / path) and streams them to
    ``POST /v1/audio/transcriptions`` (logical-id). The runtime-agnostic body both runtimes wrap. A
    fetch/transport failure binds an honest ``err`` (the tool ok/err contract); the bytes never
    touch a control-plane route (§10)."""
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
    """Run a ``speak`` activity (m19.md §2.2) — text in → audio-REFERENCE out. Calls ``POST
    /v1/audio/speech`` (logical-id), then stores the produced bytes as an artifact and returns the
    REFERENCE (the bytes are an artifact, not journaled — §2.2 / m13-dbos.md §6, so a resumed run
    replays the ref, not the audio). The ``format`` param maps to the data plane's
    ``response_format``."""
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


# ── M19 §2.3: the generic http tool (REST + GraphQL) with connection-injected auth ──────────────
#
# A ``tool`` node whose ``config.tool`` resolves to an ``http`` binding in ``ir.tools`` becomes a
# real
# outbound HTTP call. The auth lives in a server-side ``connection`` (§1.1): the binding names the
# connection id, the handler resolves connection → secret_ref → secret AT STEP TIME and injects the
# auth header — never the IR, the canvas, a span, or the journal. The same ok/err contract as M6's
# ``tool`` (a transport failure binds ``err``; a non-2xx is a normal return value bound to ``ok``).
# DBOS owns retry on the durable path (provider-client retry off — m13-dbos.md §2): this body does
# NOT
# retry. Substitution of url/headers/body uses the unified ``$in.field`` grammar, resolved by the
# caller before the call (deterministic, journaled inputs) — only the auth is added here.


@dataclass(frozen=True)
class ResolvedConnection:
    """A connection resolved to the data the http handler needs AT STEP TIME (M19 §1.1): its
    non-secret ``config`` (auth scheme, base url, static headers) and the decrypted ``secret`` (or
    ``None`` for an auth-less / secret-missing connection). The plaintext exists only here, inside
    the
    step — never journaled. ``enabled`` lets the resolver signal a disabled connection (→ honest
    ``err``)."""

    config: dict[str, Any]
    secret: str | None
    enabled: bool = True


#: The seam the handler resolves a connection through (M19 §1.1) — a callable injected by the
#: control-plane (it closes over the ConnectionStore + SecretStore + sessionmaker). The walker stays
#: free of a DB session: it calls this, the resolver owns the DB read (the same pattern M17's
#: telemetry side-channel uses). ``None`` result = unknown/disabled connection → the handler binds
#: ``err``.
ConnectionResolver = Callable[[str], Awaitable[ResolvedConnection | None]]


async def _resolve_oauth2_token(auth: Mapping[str, Any], secret: str) -> str:
    """Fetch an OAuth2 client-credentials access token (M19 §2.3) — POST the token endpoint with
    ``grant_type=client_credentials`` + client id/secret, return ``access_token``. The client secret
    is the connection's resolved secret; it never appears in the IR. This is the one auth scheme
    that
    needs its own I/O (a token fetch) before the main call."""
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


async def _auth_headers(conn: ResolvedConnection) -> dict[str, str]:
    """Build the auth header(s) for a resolved connection (M19 §2.3), server-side. Supports bearer,
    api-key header, basic, and oauth2 client-credentials. An auth-less connection (no scheme) or a
    missing secret adds nothing. Raw secret values never leave this function."""
    auth = (conn.config or {}).get("auth") or {}
    atype = auth.get("type")
    if not atype:
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
    if atype == "oauth2_client_credentials":
        token = await _resolve_oauth2_token(auth, conn.secret or "")
        return {"Authorization": f"Bearer {token}"}
    raise ValueError(f"unsupported connection auth type {atype!r}")


def _apply_response_map(value: Any, path: str) -> Any:
    """A minimal JSONPath shaper (M19 §2.3 ``responseMap``) — ``$.a.b`` / ``$.a[0].b`` into the
    parsed response body. No dependency; supports dotted keys + ``[index]``. If the path can't be
    walked (a typo / missing field), returns the value unchanged (graceful — a 2xx is not failed by
    a
    map miss; the data is still there to inspect)."""
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
    """Run one http-tool activity (M19 §2.3) — the runtime-agnostic body both the interactive walker
    and the durable step wrap. ``conn`` is the connection resolved AT STEP TIME (auth injected here,
    server-side); a missing/disabled connection (``conn is None``) binds an honest ``err``. The
    non-2xx status is a normal return value (bound ``ok`` as ``{status, body, headers}``); only a
    transport failure (timeout/DNS/refused) or an auth-setup error binds ``err``. The
    ``idempotency_key`` (set for unsafe methods — §2.5) adds an ``Idempotency-Key`` header so a
    partial-failure re-run is a provider no-op. Provider-client retry is OFF (DBOS owns retry). The
    ``timeout_s`` is a leaf knob, not a caller-cancellation handle (ASYNC109)."""
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
        final_headers.update(await _auth_headers(conn))  # server-side auth injection (§1.1)
    except Exception as exc:  # auth setup (e.g. oauth token fetch) failed — honest err, no hang
        return ActivityOutcome(ok=False, value=f"http tool auth failed: {exc}")
    kwargs: dict[str, Any] = {"headers": final_headers, "timeout": timeout_s or 30.0}
    if body is not None:
        kwargs["json"] = body  # JSON request (REST or GraphQL {query, variables})
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.request(method.upper(), final_url, **kwargs)
    except Exception as exc:  # transport failure → err (m6.md §4), never a hang
        return ActivityOutcome(ok=False, value=f"http tool request failed: {exc}")
    try:
        parsed: Any = resp.json()
    except (ValueError, TypeError):
        parsed = resp.text
    if response_map:
        # responseMap shapes the STEP OUTPUT (§2.3): return just the mapped value, not the
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
    absent (the M6 registry path). Lets the ``tool`` handler route http-vs-builtin (M19 §2.3)."""
    binding = ir.tools.get(tool_key)
    return binding if isinstance(binding, HttpTool) else None


_PURE_REF = re.compile(r"\$in(?:\.[A-Za-z0-9_]+)*$")


def _resolve_template_value(value: Any, ports: _PortInputs, node_id: str) -> Any:
    """Substitute ``$in`` refs inside a body-template value (M19 §2.3). Only a string that is
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
    """A fully-resolved http request (M19 §2.3) — the deterministic part (templates substituted from
    journaled inputs) the caller hands to :func:`execute_http_tool`. The connection (auth) is
    resolved
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
    """Resolve an http-tool node's config into a concrete request (M19 §2.3), substituting the
    unified ``$in.field`` grammar in url / headers / body. ``idempotency_key`` additionally expands
    ``{runId}`` / ``{nodeId}`` (the §2.5 'derived from runId+nodeId' convention). Auth is NOT added
    here — it is injected server-side from the connection inside the step (§1.1)."""
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
    """Build + execute an http-tool call (M19 §2.3) — the one body both runtimes call so the http
    path is identical interactive vs durable. Builds the request (deterministic), resolves the
    connection through ``resolver`` (server-side auth, at step time), then runs it. A template error
    or a missing resolver binds ``err`` honestly (never a hang)."""
    try:
        call = build_http_call(binding, config, ports, node_id=node_id, run_id=run_id)
    except Exception as exc:  # unresolvable $in in url/headers/body → structured err (m6.md §4)
        return ActivityOutcome(ok=False, value=f"http tool {config.tool!r}: {exc}")
    conn = await resolver(call.connection_id) if resolver is not None else None
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


# ── M21 autonomous tool-calling: function schemas + per-call dispatch ────────────────────────────


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
    name: str, binding: Any, *, registry: ToolRegistry, mcp: McpManager
) -> dict[str, Any] | None:
    """One OpenAI function schema from a tool BINDING object — the shared core of the M21 ir.tools
    path and the M22 capability-node path. ``name`` is the function name the model echoes back: an
    ir.tools key (M21) or a tool node id (M22). None when no schema resolves (skip it)."""
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
    return None


async def build_tool_schemas(
    ir: IRDocument, tool_keys: list[str], *, registry: ToolRegistry, mcp: McpManager
) -> list[dict[str, Any]]:
    """Build the OpenAI ``tools`` array from an llm node's ``tools`` keys (the M21 ir.tools path).
    The function NAME is the ir.tools key (so a tool_call maps back to its binding); a key with no
    resolvable schema is skipped. (M22 capability edges go through ``build_capability_schemas``.)"""

    schemas: list[dict[str, Any]] = []
    for key in tool_keys:
        binding = ir.tools.get(key)
        if binding is None and key in registry:  # a directly-registered builtin (echo/http_fetch)
            meta = registry.schema(key)
            if meta is not None:
                schemas.append(_fn_schema(key, meta[0], meta[1]))
            continue
        schema = await _schema_for_binding(key, binding, registry=registry, mcp=mcp)
        if schema is not None:
            schemas.append(schema)
    return schemas


def _capability_tool_nodes(ir: IRDocument, llm_node_id: str) -> list[Node]:
    """The tool/mcp_tool nodes wired to this llm's ``tool``-role port (M22 capability edges) — the
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
        if src is not None and src.type in ("tool", "mcp_tool"):
            nodes.append(src)
            seen.add(edge.source)
    return nodes


def _capability_binding(ir: IRDocument, name: str) -> Any | None:
    """Resolve a model tool_call ``name`` (a tool/mcp_tool NODE id — M22) to an ir.tools-style
    BINDING built from the node's INLINE config, so the existing ``execute_tool_call`` dispatch (and
    the durable ``_durable_tool_call``) handle it unchanged. Returns None if ``name`` is not a tool
    node id — the caller then falls back to the legacy ir.tools / registry lookup (m22.md D3/D6)."""
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
    nodes: list[Node], *, registry: ToolRegistry, mcp: McpManager
) -> list[dict[str, Any]]:
    """OpenAI tool schemas for M22 capability nodes — the function NAME is the NODE id (collision-
    safe: two llms may wire different nodes that share a builtin/server). Description + parameters
    come from each node's inline config: builtin from the registry, http from ``description`` +
    ``parameterSchema``, mcp from the server's ``list_tools`` (a connection-based or unreachable mcp
    falls back to its authored ``description`` with no parameter schema — m22.md D3)."""
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
            schemas.append(schema or _fn_schema(node.id, cfg.description, None))
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
    """The ``{role: tool}`` message fed back to the model after a tool call (M21): the tool's result
    on success, or its actionable error on failure (the M6/M7 'tool error is structured output'
    contract — a failed tool is NOT a run failure; the model sees the error and can recover)."""
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
) -> ActivityOutcome:
    """Execute ONE model-emitted tool call by dispatching to the EXISTING executor for its binding
    (M21) — no new tool machinery. ``call.name`` is an ir.tools key (or a directly-registered
    builtin); ``call.arguments`` is the model's args. For an http tool the args ARE the ``$in``
    source (m21.md §6 Q3): the binding's url/body slots are filled from them. ok/err is the M6/M7
    contract — a tool error is RETURNED, not raised, so the loop feeds it back to the model. The
    ``idempotency_suffix`` (the call id / iteration) is appended to the http idempotency key so a
    write tool called twice in one loop under at-least-once (D9) doesn't collide."""

    # M21: ``call.name`` is an ir.tools key. M22: if it isn't, it may be a tool/mcp_tool NODE id
    # wired as a capability — resolve the binding from that node's inline config (D3/D6). Either way
    # the same dispatch below runs. Legacy ir.tools keys keep working (tried first).
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
        conn = await tool_auth(binding.connection or "") if tool_auth is not None else None
        if conn is None:
            return ActivityOutcome(
                ok=False, value=f"mcp connection {binding.connection!r} not found or disabled"
            )
        cfg = mcp_config_from_connection(conn)
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


# ── M19 §2.9: the transform node (deterministic reshape — NOT a code sandbox) ────────────────────


def execute_transform(expr: str, ports: _PortInputs, node_id: str) -> Any:
    """Reshape the in-port value(s) by a deterministic JSON template (m19.md §2.9). ``expr`` is a
    JSON document whose string leaves may be ``$in`` refs (``$in.in.location.city``) — the handler
    parses it and resolves each ref over the journaled inputs (reusing ``_resolve_template_value``).
    No I/O, no sandbox (``kind: orchestration``), so it runs inline in both runtimes and replays
    deterministically. A non-JSON ``expr`` or an unresolvable ref is a LOUD :class:`TransformError`
    (no ``err`` port — a bad reshape fails the run). (A full JSONata/jq DSL is the deferred richer
    form — §2.9 'include if cheap'; the JSON-template form is the cheap, dep-free reshape.)"""
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


# ── M19 §2.6: the guardrail node (rule⇒orchestration · model⇒activity, §1.2) ─────────────────────
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
    """Evaluate a deterministic guardrail rule (m19.md §2.6) — return True = PASS. No I/O. ``value``
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
    input) — exactly one handle, so the un-taken branch's edges are dead (m6.md §4 edge-skipping).
    The canonical ``control``/``data`` example: guardrail ``pass`` → llm; guardrail ``block`` →
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
) -> str:
    """Run a model guardrail's classifier call (m19.md §2.6) — the runtime-agnostic body both
    runtimes wrap. Builds one user message (the judge prompt + the input) and assembles the answer
    via ``execute_llm``; the caller compares it to ``pass_on``. One cheap call before the costly."""
    messages = [{"role": "user", "content": f"{prompt}\n\nInput:\n{_stringify(input_value)}"}]
    result = await execute_llm(
        gateway, model_id=model_id, params=params, messages=messages, extra_headers=extra_headers
    )
    return result.output


def guardrail_model_passed(answer: str, pass_on: str) -> bool:
    """A model guardrail passes when the classifier's answer begins with ``pass_on`` (case/space
    insensitive) — e.g. a "yes/no, is this in scope?" judge with ``pass_on='yes'``."""
    return answer.strip().lower().startswith(pass_on.strip().lower())


# ── M19 §2.8: the gate nodes (ratelimit · quota — the lean per-key seam, §1.6) ───────────────────


def resolve_gate_key(key_expr: str, ports: _PortInputs, node_id: str) -> str:
    """Resolve a gate's ``key_expr`` to the caller key (m19.md §2.8/§1.6). A ``$in`` ref resolves
    over the input (per-input-field key, e.g. ``$in.in.userId``); anything else (a literal, or
    ``$caller.token``) is the key verbatim — a single per-key bucket. Per-USER attribution (refining
    ``$caller.*`` to a real principal) is the deferred identity milestone (§1.6), not faked here."""
    if key_expr.startswith("$in"):
        try:
            return _stringify(_resolve_ref(key_expr, ports, node_id))
        except _RefError:
            return key_expr  # unresolvable → fall back to the literal bucket (no silent crash)
    return key_expr


async def execute_ratelimit(gates: Any, *, scope: str, limit: int, window_seconds: int) -> bool:
    """Count one hit and return True = ALLOW (m19.md §2.8). Denies once the current window's count
    exceeds ``limit``. ``gates`` is the injected backend (the counter lives in Postgres). With no
    backend wired (no gate in the graph normally), allow — the gate is inert, never a hard fail."""
    if gates is None:
        return True
    count = await gates.hit(scope, window_seconds)
    return count <= limit


async def execute_quota(
    gates: Any, *, agent_id: str | None, budget_tokens: int, window_seconds: int
) -> bool:
    """Read accumulated token usage and return True = ALLOW (m19.md §2.8/§1.6 — read, don't
    re-meter). Denies once usage reaches ``budget_tokens`` in the window. No backend → allow."""
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
    §2.8). Exactly one handle, so the un-taken branch is dead (m6.md §4 edge-skipping)."""
    if allowed:
        live_handles[node.id] = {"allow"}
        values[(node.id, "allow")] = input_value
    else:
        live_handles[node.id] = {"deny"}
        values[(node.id, "deny")] = {"denied": True, "reason": reason}


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

    # M22: tool/mcp_tool nodes wired as CAPABILITIES (the source of a `tool`-channel edge) are not
    # run as standalone steps — the model calls them lazily inside the llm loop (build_capability_
    # schemas + execute_tool_call). Skip them in the topo walk. (topological_order already drops
    # `tool` edges, so they don't sequence anything; this stops the node body from executing.)
    capability_nodes = {e.source for e in ir.edges if e.channel == "tool"}

    for node in topological_order(ir):
        if node.id in capability_nodes:
            continue
        if _is_skipped(node, ir.edges, skipped, live_handles):
            skipped.add(node.id)
            _log_node(ctx, node, skipped=True)
            # M17: a skipped (dead-branch) node is a zero-width grey bar on the waterfall.
            if ctx.run_trace is not None:
                await ctx.run_trace.skipped(node)
            continue
        _log_node(ctx, node, skipped=False)

        # M17: capture the resolved in-port values BEFORE running the node (upstream values are
        # already bound — topo order) so the span's node_io shows what each node RECEIVED (§3).
        io_inputs = _io_input_snapshot(node, ir.edges, values, skipped, live_handles)
        # The one wrapper (§1.5/§4): each executed node runs inside a span; on close it writes the
        # span row + node_io per the effective capture policy. A no-op CM when telemetry is unwired,
        # so the walk is byte-for-byte its pre-M17 self without a RunTrace.
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
                    await _walk_guardrail_model(node, ir, ctx, values, skipped, live_handles)
                elif node.type in ("ratelimit", "quota"):
                    await _walk_gate(node, ir, ctx, values, skipped, live_handles)
                elif node.type == "transcribe":
                    await _walk_transcribe(node, ir, ctx, values, skipped, live_handles)
                elif node.type == "speak":
                    await _walk_speak(node, ir, ctx, values, skipped, live_handles)
                else:
                    # agent / rag / retriever / memory / code — deferred (§7).
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
                else:
                    # loop / map — durable-only (M14, raise here in the interactive walker).
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


# ── M17 observability glue (the one-wrapper seam, §4) ────────────────────────
# Thin, additive helpers so the walk wraps each node in a span and captures its I/O WITHOUT changing
# any handler signature or the dispatch. When ``ctx.run_trace`` is unset the span CM is a no-op, so
# a
# walk without telemetry is byte-for-byte its pre-M17 self.


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
    node consumes), for ``node_io.inputs`` (§3). Multi-input (M10) → one entry per named in-port."""
    return dict(_collect_in_ports(node, edges, values, skipped, live_handles).values)


def _io_output_snapshot(
    node: Node, values: dict[tuple[str, str], Any], live_handles: dict[str, set[str]]
) -> dict[str, Any]:
    """The node's emitted per-out-handle values, for ``node_io.outputs`` (§3). A router/tool emits
    on
    exactly the taken handle(s); an llm/input on its success handle(s); an output node emits nothing
    (its input IS the run output, captured as the input snapshot)."""
    return {handle: values.get((node.id, handle)) for handle in live_handles.get(node.id, set())}


def _node_span_status(node: Node, live_handles: dict[str, set[str]]) -> str:
    """``err`` if the node bound ONLY its error handle(s) (a tool/mcp_tool structured error — m6.md
    §4; the run continues but the node itself errored, so the bar is red), else ``ok``. A node that
    raised is marked ``err`` by the span CM's exception path instead."""
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
    scope: SpanScope | None = None,
) -> AsyncIterator[Delta]:
    config = LlmConfig.model_validate(node.config)
    model_id, params = resolve_model(ir, config)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # Naive full replay (M4 §4): prior thread turns verbatim, then this turn's rendered prompt. The
    # prompt may compose several in-ports (M10) — $in.file AND $in.question — via the port map.
    messages = [*ctx.prior_messages, *_render_messages(node, config, ports)]

    # M21 — autonomous tool-calling. If the node offers tools, build the OpenAI function schemas
    # (function names == ir.tools keys, so a tool_call maps straight back to a binding) + the
    # tool_choice ONCE, then run a BOUNDED loop: stream one model turn (the shared `execute_llm`
    # body — M13 §2/D4 / the parity refactor m21.md §6 Q1), and if it emits tool_calls, execute each
    # via the EXISTING executors, append the assistant request + each {role:tool} result, and call
    # again — until the model answers or `maxToolIterations` is hit. With no tools this is exactly
    # ONE turn, byte-identical to pre-M21 (test_m21_llm_parity is the golden gate). The
    # streaming/parse body lives ONCE in `execute_llm`, wrapped here (interactive) and in the
    # durable
    # compiler (parity). M17 §2: each turn streams inside a `model.generate` phase span.
    # M22: the available-tool set is the UNION of the legacy ir.tools keys (config.tools — M21) and
    # the tool/mcp_tool nodes wired to this llm's `tools` port (capability edges). Capability
    # schemas use the NODE id as the function name; legacy schemas use the ir.tools key. Both
    # dispatch through the same execute_tool_call. Empty union → single-shot (pre-M21).
    cap_nodes = _capability_tool_nodes(ir, node.id)
    schemas: list[dict[str, Any]] = []
    if config.tools:
        schemas += await build_tool_schemas(ir, config.tools, registry=ctx.tools, mcp=ctx.mcp)
    if cap_nodes:
        schemas += await build_capability_schemas(cap_nodes, registry=ctx.tools, mcp=ctx.mcp)
    has_tools = bool(schemas)
    tool_schemas: list[dict[str, Any]] | None = schemas if has_tools else None
    tool_choice: Any = _to_openai_tool_choice(config.tool_choice) if has_tools else None

    gen_cm = scope.child_phase("model.generate") if scope is not None else _null_acm()
    first_token_ns: int | None = None
    final_output = ""
    final_finish: str | None = None
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

            async def _turn(q: asyncio.Queue[Delta | None] = deltas) -> LlmActivityResult:
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
                        tool_choice=tool_choice,
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
                # cancel the in-flight turn so the upstream stream isn't leaked (M9 interrupt rule).
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task

            final_finish = turn.finish_reason
            # No tool calls (or the node has no tools — legacy or capability) → answer is final.
            if not (turn.tool_calls and has_tools):
                final_output = turn.output
                truncated = turn.truncated_empty
                break
            # Hit the cap with the model still wanting tools → stop honestly (m9 §2.4 discipline).
            if iteration >= config.max_tool_iterations:
                capped = True
                final_output = turn.output
                break
            # Execute the requested tools, feeding the assistant request + each result back so the
            # next turn sees them (the OpenAI loop shape). A tool error is fed back, not fatal
            # (M6/M7).
            messages.append(assistant_tool_calls_message(list(turn.tool_calls)))
            for call in turn.tool_calls:
                yield Delta(
                    node_id=node.id, content=call.name, kind="tool_call"
                )  # visible progress
                # M17: each tool call is a child span under the llm node span, so the waterfall
                # shows
                # the autonomous loop's tool steps inside the model node (m21.md Phase 6 / §6).
                tool_cm = (
                    scope.child_phase(f"tool.{call.name}") if scope is not None else _null_acm()
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
                    )
                    if tool_scope is not None:
                        tool_scope.set_attributes({"tool.name": call.name, "tool.ok": outcome.ok})
                messages.append(tool_result_message(call, outcome))
            iteration += 1

        # GenAI-semconv scalars on the generate span (time-to-first-token, finish reason, model).
        if gen_scope is not None:
            attrs: dict[str, Any] = {"gen_ai.request.model": model_id}
            if final_finish is not None:
                attrs["gen_ai.response.finish_reason"] = final_finish
            if first_token_ns is not None:
                attrs["ttft_ms"] = round((first_token_ns - gen_scope.span.start_ns) / 1e6, 1)
            gen_scope.set_attributes(attrs)

    # Mirror the model id onto the node span too, so the waterfall's left rail can label the llm
    # node without opening the phase child.
    if scope is not None:
        scope.set_attributes({"gen_ai.request.model": model_id})

    # Honest legibility (m9 §2.4): a truncated-empty answer, OR a tool loop that hit its cap without
    # a final answer, must not bind a green blank.
    if result is not None and (truncated or (capped and _is_blank(final_output))):
        result.truncated_empty_nodes.append(node.id)

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
    is a *structured error*, not a run failure (m6.md §4): bind the message to the ``err`` handle
    and continue — downstream ``err`` edges run, ``ok`` edges are skipped. (A non-200 HTTP status
    is a normal return value, bound to ``ok``; only transport failures raise.)"""

    config = ToolConfig.model_validate(node.config)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # M19 §2.3: route http-vs-builtin. An ``http`` binding in ``ir.tools`` → a real outbound call
    # with connection-injected auth (server-side, at step time); otherwise the M6 builtin registry
    # path. An unresolvable ``$in`` ref is a structured err either way (m6.md §4): bind ``err``.
    binding = is_http_tool(ir, config.tool)
    if binding is not None:
        outcome = await run_http_tool(
            binding, config, ports, node_id=node.id, run_id=ctx.run_id, resolver=ctx.tool_auth
        )
    else:
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
    tool = config.tool
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    # Resolve args deterministically, then run the shared activity body (M13 §2 — the same body the
    # durable step wraps). M19 §2.4: a ``connection``-based node resolves its connection server-side
    # (auth never in the IR) and uses the http/stdio transport; a ``server``-based node is the M7
    # registered-name path. A bad arg ref is itself a structured err (m7.md §4).
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
    """Dispatch an mcp_tool call by binding kind (M19 §2.4): ``connection`` → resolve the connection
    server-side + use its transport; ``server`` → the M7 registered-name path. Shared by the
    interactive + durable mcp handlers."""
    if config.connection:
        if ctx.tool_auth is None:
            return ActivityOutcome(ok=False, value="mcp_tool: no connection resolver configured")
        conn = await ctx.tool_auth(config.connection)
        if conn is None:
            return ActivityOutcome(
                ok=False, value=f"mcp connection {config.connection!r} not found or disabled"
            )
        mcp_config = mcp_config_from_connection(conn)
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
    """Run a ``transcribe`` node (m19.md §2.2): the audio-ref in-port value → text. Binds text to
    the success handle(s), an err message to ``err`` (the tool ok/err contract). The bytes go to the
    inference base URL (gateway), never a control-plane route (§10)."""
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
    """Run a ``speak`` node (m19.md §2.2): the text in-port value → an audio REFERENCE (the bytes
    are a stored artifact, not journaled). Binds the ref to the success handle(s), an err to err."""
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


def _walk_transform(
    node: Node,
    ir: IRDocument,
    values: dict[tuple[str, str], Any],
    skipped: set[str],
    live_handles: dict[str, set[str]],
) -> None:
    """Run a ``transform`` node (m19.md §2.9): reshape the in-port value(s) by the JSON template
    ``config.expr`` (deterministic, inline — no I/O) and bind the result to the success handle(s). A
    a bad expr / unresolvable ref raises :class:`TransformError` (failed run), like the router."""
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
    """Run a RULE guardrail (m19.md §2.6, orchestration): evaluate the deterministic predicate over
    the in-port value (no I/O) and activate ``pass`` (input through) or ``block`` (the onBlock
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
) -> None:
    """Run a MODEL guardrail (m19.md §2.6, activity): a cheap classifier call decides pass/block.
    The model sub-config is guaranteed present (validated in packages/ir); the model id resolves
    like an ``llm`` (engine-name rejected). Then the same pass/block binding as the rule path."""
    config = GuardrailConfig.model_validate(node.config)
    mcfg = config.check.model
    assert mcfg is not None  # validate_graph guarantees this for a model check
    model_id, params = resolve_model_key(ir, mcfg.model)
    ports = _collect_in_ports(node, ir.edges, values, skipped, live_handles)
    input_value = _single_in_value(ports, node)
    answer = await execute_guardrail_model(
        ctx.gateway,
        model_id=model_id,
        params=params,
        prompt=mcfg.prompt,
        input_value=input_value,
        extra_headers=ctx.extra_headers,
    )
    _bind_guardrail(
        node,
        guardrail_model_passed(answer, mcfg.pass_on),
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
    """Run a ``ratelimit`` or ``quota`` gate (m19.md §2.8): resolve the caller key, check the
    backend, and activate ``allow`` (input through) or ``deny`` (a clean result). Both are
    activities (counter / usage reads = I/O). Per-key now; per-user deferred (§1.6)."""
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
