"""The Agent Graph IR — the builder ↔ runtime seam (theygent-graph-schema.md §8).

This is the §4-class, hard-to-reverse decision M5 first exercises: the IR is the input
format the walker executes, the no-code builder will *write*, the registry will *version*,
and observability will *map spans onto*. M1 froze the inference-plane registration payload
(``registration.py``); M5 lands the rest of §8 — the document envelope (§8.2), nodes/edges
(§8.3), model bindings (§8.4), and the determinism-class ``kind`` taxonomy (§8.1).

Two rules this module encodes, both load-bearing:

* **Dispatch is by ``kind`` (§8.1), never ``type`` (§3.2 of M5).** ``kind``
  (``activity`` / ``orchestration`` / ``boundary``) is the determinism class the compiler
  lowers on; ``type`` (``llm`` / ``input`` / ``output`` / …) selects the *handler within a
  kind*. ``NODE_TYPE_KIND`` pins the one correct ``kind`` for every known ``type`` so a
  ``type``/``kind`` mismatch is rejected, not silently lowered wrong.
* **Layout never pollutes logic (§8.0/§8.2).** The optional ``view`` block holds React-Flow
  positions and is *never* hashed; ``content_hash`` (``contenthash.py``) is computed over the
  view-stripped, key-sorted JSON, so dragging a node never produces a "new version".

This package stays pure: Pydantic models + static graph algorithms (validation, topological
order). It imports no engine SDK and no gateway client — execution (the walker) lives in the
control-plane, which owns the inference seam. The IR owns the *format*, not the *run*.
"""

from __future__ import annotations

from collections import deque
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from pydantic.alias_generators import to_camel

# ── the determinism taxonomy (§8.1) ──────────────────────────────────────────

NodeKind = Literal["activity", "orchestration", "boundary"]

#: The one correct ``kind`` for every known node ``type`` (§8.3). The walker dispatches on
#: ``kind``; this table is what makes a ``type``/``kind`` mismatch a validation error rather
#: than a wrong lowering. Extended additively as the plugin API opens (§8.3 is extensible).
NODE_TYPE_KIND: dict[str, NodeKind] = {
    # boundary — graph entry/exit, human wait, sub-graph call
    "input": "boundary",
    "output": "boundary",
    "human": "boundary",
    "subgraph": "boundary",
    # activity — non-deterministic side effects (model / tool / retrieval / code)
    "agent": "activity",
    "llm": "activity",
    "tool": "activity",
    "mcp_tool": "activity",
    "rag": "activity",
    "retriever": "activity",
    "memory": "activity",
    "code": "activity",
    # orchestration — deterministic control flow
    "router": "orchestration",
    "condition": "orchestration",
    "loop": "orchestration",
    "iterator": "orchestration",
    "map": "orchestration",
}

#: The node ``type``s the walker actually executes. Every other known ``type`` is a valid IR
#: shape with a dispatcher branch that raises ``NotImplementedError`` — adding it later is an
#: additive walker handler, not a refactor. M5 shipped ``input``/``output``/``llm``; M6 added
#: ``tool`` (first non-llm activity) and ``router`` (first orchestration node); M7 adds
#: ``mcp_tool`` (an external MCP server's tool, same contract over a new transport) — m7.md §0.
#: M14 adds ``human``/``subgraph`` (``boundary``) and ``loop``/``map`` (``orchestration``) — the
#: four additive-lowering types. They run ONLY on the durable runtime (each lowers onto a DBOS
#: primitive: ``recv`` / child workflow / bounded inline repetition / durable-queue fan-out);
#: the interactive M5 walker still raises ``NotImplementedError`` for them (m14.md §0/§1).
EXECUTABLE_TYPES: frozenset[str] = frozenset(
    {"input", "output", "llm", "tool", "router", "mcp_tool", "human", "subgraph", "loop", "map"}
)


class _Wire(BaseModel):
    """camelCase on the wire (the builder/registry speak it), snake_case in code; reject
    unknown keys so an IR typo fails loudly instead of being silently dropped (§3.1: refuse
    anything that isn't the IR)."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        protected_namespaces=(),
    )


# ── model bindings (§8.4) ─────────────────────────────────────────────────────

BindingName = Literal["mlx", "vllm", "llamacpp", "openai-compatible"]
SourceName = Literal["hf", "local-path", "url"]


class ModelBinding(_Wire):
    """A logical model the graph's nodes reference by key (§8.4) — the sovereignty dial.

    Nodes never hardcode a provider; they name a key in ``IRDocument.models`` and the
    ``binding`` resolves at runtime through the L0 gateway. ``binding`` names only the engines
    theygent manages (``mlx``/``vllm``/``llamacpp``) plus the catch-all ``openai-compatible``;
    ``source`` (where the *weights* come from) is orthogonal and never an engine.

    ``model`` is the id forwarded to the inference seam. M5 has no registry/lifecycle manager
    (M5 §7), so the walker forwards ``model`` as the logical id the inference plane already
    knows (the §9.1.1 logical-id invariant still holds — an engine name here is rejected at
    the seam). ``source`` is optional: it answers a lifecycle question (fetch the weights from
    where?) that only matters once theygent manages the engine, which M5 does not — so the
    trivial M5 graph omits it, while a §8.4 document that includes it still validates.
    """

    binding: BindingName
    model: str
    source: SourceName | None = None
    params: dict[str, Any] = Field(default_factory=dict)


# ── tools (§8.5) ──────────────────────────────────────────────────────────────


class BuiltinTool(_Wire):
    kind: Literal["builtin"]
    ref: str


class McpTool(_Wire):
    kind: Literal["mcp"]
    server: str
    tool: str


ToolBinding = Annotated[BuiltinTool | McpTool, Field(discriminator="kind")]


# ── nodes & edges (§8.3) ──────────────────────────────────────────────────────


class Port(_Wire):
    """A declared (not inferred) connection point, so the compiler can type-check edges
    (§8.3). ``type`` is advisory in M5 (``any``/``error``); real port typing lands with the
    node types that need it.

    ``required`` is meaningful for **in**-ports (M10 §3): a required in-port must be fed by a
    ``data`` edge or the graph fails validation; ``required: false`` declares an in-port that may
    be left unfed and resolves to ``null`` at runtime. The default is required, so an unfed input
    is a loud validation error rather than a silent ``None`` — the multi-input analogue of M9's
    no-silent-pass-through rule. (Ignored on out-ports.)"""

    id: str
    type: str = "any"
    required: bool = True


class Ports(_Wire):
    in_: list[Port] = Field(default_factory=list, alias="in")
    out: list[Port] = Field(default_factory=list)


EdgeChannel = Literal["data", "control"]


class Node(_Wire):
    """One graph node (§8.3). ``id`` is stable — it is what an OTel span binds to later
    (M5 stamps it on per-node logs, the attach-point). ``config`` is ``type``-specific and
    validated against a per-``type`` schema in ``validate_graph`` — Pydantic at the boundary,
    not a free-form dict the walker trusts blindly."""

    id: str
    type: str
    kind: NodeKind
    label: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    ports: Ports = Field(default_factory=Ports)


class Edge(_Wire):
    """A directed connection (§8.3). ``channel: data`` passes a value along the edge;
    ``channel: control`` is pure sequencing. ``condition`` is reserved for router-driven
    edges (the first ``orchestration`` node, deferred — M5 §7)."""

    id: str
    source: str
    source_handle: str
    target: str
    target_handle: str
    channel: EdgeChannel = "data"
    condition: str | None = None


class IRDocument(_Wire):
    """A saved agent — one JSON document (§8.2). ``id`` + ``version`` is the registry
    coordinate; ``content_hash`` (computed over the view-stripped graph) gives content-addressed
    deploys. ``view`` is the React-Flow layer and is **never** hashed (§8.6)."""

    schema_version: str
    id: str
    name: str
    version: str
    content_hash: str | None = None
    models: dict[str, ModelBinding] = Field(default_factory=dict)
    tools: dict[str, ToolBinding] = Field(default_factory=dict)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    # Layout + free-form metadata: present in a real document, ignored by execution and never
    # hashed. Typed as opaque so the walker never grows a dependency on either.
    view: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


# ── per-type config schemas (§8.7 step 3, scoped to M5's three types) ─────────


class _Message(_Wire):
    role: Literal["system", "user", "assistant"]
    content: str


class LlmConfig(_Wire):
    """``llm`` node config: a single model call, no tool loop (§8.3). ``model`` names a key in
    ``IRDocument.models``; ``messages`` is the prompt template — content fields use the ONE
    port-addressed substitution token language shared with ``tool``/``mcp_tool``/``router``
    (§8.5 / m10.md §1.2): ``$in`` is the default in-port ``in``, ``$in.<port>`` selects a named
    in-port (so one node composes multiple upstreams), ``$in.<port>.<field>`` drills in. An unknown
    port or token fails loudly, never silent literal pass-through. (``$input`` was the M5 spelling,
    renamed to ``$in`` in M9; M10 made the segment after ``$in.`` a port name — see m10.md.)"""

    model: str
    messages: list[_Message]
    # ``messages`` content uses the §8.5 token grammar: ``$in`` (default in-port ``in``),
    # ``$in.<port>`` (a named in-port — so one llm node can compose ``$in.file`` AND
    # ``$in.question``), ``$in.<port>.<field>`` (drill into it). Unknown port / unknown token →
    # loud error (m10.md §1.2). The token lives inside the opaque content string — no schema change.


class InputConfig(_Wire):
    """``input`` boundary node — graph entry. No config in M5 (the run's ``input`` binds to
    its out-port); the model exists so an unexpected key fails validation, not silently."""


class OutputConfig(_Wire):
    """``output`` boundary node — graph exit. No config in M5 (its in-port value is the run's
    output)."""


class ToolConfig(_Wire):
    """``tool`` activity node config (§8.3 / m6.md §2/§3.1). ``tool`` is a logical name resolved
    against the control-plane's in-code tool registry (membership is checked there, not here —
    ``packages/ir`` stays pure). ``args`` is an arg template: each value is either a literal or a
    ``$in``-reference (``$in`` = the whole in-port value, ``$in.a.b`` = a path into it) the walker
    resolves against upstream data before invoking the callable."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class RouterConfig(_Wire):
    """``router`` orchestration node config (§8.3 / m6.md §3.2). ``select`` is a ``$in``-reference
    that must resolve to the **name of one of this node's outgoing handles** — handle-name routing,
    not an expression DSL. The walker follows only the edge(s) from the selected handle."""

    select: str


class McpToolConfig(_Wire):
    """``mcp_tool`` activity node config (§8.5 / m7.md §2). ``server`` is the **logical name** of a
    registered MCP server (resolved by the control-plane's MCP manager at runtime — same logical-id
    discipline as model bindings and the M6 tool registry); ``tool`` is a tool that server exposes;
    ``args`` is the same ``$in`` / ``$in.a.b`` arg template as M6's ``tool``. The IR seam matches a
    local tool — only the transport (an external MCP process) differs."""

    server: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


# ── M14 the additive-lowering tier (m14.md §1) — config shapes only ───────────
#
# Four node types whose ``kind`` is ``boundary`` (``human``/``subgraph``) or ``orchestration``
# (``loop``/``map``). The IR package owns their *shape*; the lowering onto DBOS primitives lives in
# the control-plane's durable compiler (the ``dbos`` import never reaches here). ``subgraph``/
# ``loop``/``map`` all compose a SAVED, PINNED agent as their body (never inline IR — m14.md §1.2 /
# the Do-NOT): the unit of composition/repetition/fan-out is one saved-agent invocation, which the
# durable runtime already runs as ``theygent_run``. The pin is part of ``config`` and therefore part
# of the hashed IR, so it is **frozen into the parent's contentHash** — composition is immutable
# even when the child agent publishes a new version (m14.md §1.2).


class HumanConfig(_Wire):
    """``human`` boundary node config (m14.md §1.1) — a durable wait for external input. The node
    lowers onto ``DBOS.recv``: the run persists as ``waiting`` and survives a worker crash while
    paused; ``POST /runs/{id}/resume`` (→ ``DBOS.send``) delivers the awaited input and the workflow
    resumes from the checkpoint. ``prompt`` describes what input is expected (advisory);
    ``input_schema`` is the awaited input's declared shape (advisory in M14 — resume records it,
    doesn't enforce a full JSON-Schema validation). ``timeout`` (seconds; ``None`` = wait forever)
    bounds the wait; on timeout the node fails honestly (``on_timeout='fail'``, the default →
    ``failed`` run, M9) or binds the declared ``default`` (``on_timeout='default'``)."""

    prompt: str | None = None
    input_schema: dict[str, Any] | None = None
    timeout: float | None = None
    on_timeout: Literal["fail", "default"] = "fail"
    default: Any = None


class SubgraphConfig(_Wire):
    """``subgraph`` boundary node config (m14.md §1.2) — an agent calls another SAVED agent, run as
    a DBOS child workflow (independently durable + resumable). ``agent`` is the child's §8.2 id; the
    node pins it with EXACTLY ONE of ``version`` / ``content_hash`` (validated in
    ``validate_graph``) and the pin is frozen into the parent's contentHash, so composition is
    immutable. ``max_depth`` bounds recursion (the ``subgraph`` analogue of ``loop``'s
    ``max_iterations``): exceeding it fails honestly, preventing unbounded / mutually-recursive
    expansion. The parent's in-port value is the child's run input (M10 named ports); the child's
    output binds the node's success handle."""

    agent: str
    version: str | None = None
    content_hash: str | None = None
    max_depth: int = 8


class LoopConfig(_Wire):
    """``loop`` orchestration node config (m14.md §1.3) — bounded, deterministic agentic repetition
    (think→act→observe). Each iteration runs the pinned ``agent`` (a SAVED agent, child workflow);
    the previous iteration's output feeds the next as input. ``max_iterations`` is **REQUIRED** (no
    unbounded loops — the termination + determinism guarantee). ``condition`` (optional) is a
    ``$in``-reference over the iteration output (the same port-addressed grammar the router uses —
    m14.md §1.3 "reuse the router expression evaluator"): the loop **stops early** when it resolves
    truthy. The loop control is deterministic orchestration with NO I/O — it reads journaled child
    results, never calls inference, so a crash mid-loop resumes at the last completed iteration (no
    completed iteration re-runs). ``max_depth`` bounds nesting as in ``subgraph``."""

    agent: str
    version: str | None = None
    content_hash: str | None = None
    max_iterations: int
    condition: str | None = None
    max_depth: int = 8


class MapConfig(_Wire):
    """``map`` orchestration node config (m14.md §1.4) — durable fan-out/join over a collection. The
    in-port value must be a list; one task (the pinned ``agent`` run as a child workflow) is
    enqueued on a DBOS durable queue per element, then all results are durably awaited — so a crash
    mid-fan-out resumes ONLY the incomplete branches. ``concurrency`` bounds how many branches are
    in flight at once (``None`` = unbounded). ``on_error`` is the partial-failure policy:
    ``fail_fast`` (default — any element error fails the map) vs ``collect`` (gather successes +
    per-element errors). ``max_depth`` bounds nesting as in ``subgraph``."""

    agent: str
    version: str | None = None
    content_hash: str | None = None
    concurrency: int | None = None
    on_error: Literal["fail_fast", "collect"] = "fail_fast"
    max_depth: int = 8


_CONFIG_MODELS: dict[str, type[_Wire]] = {
    "llm": LlmConfig,
    "input": InputConfig,
    "output": OutputConfig,
    "tool": ToolConfig,
    "router": RouterConfig,
    "mcp_tool": McpToolConfig,
    "human": HumanConfig,
    "subgraph": SubgraphConfig,
    "loop": LoopConfig,
    "map": MapConfig,
}

#: The M14 node types that compose a saved, pinned agent body (m14.md §1.2). Each must pin EXACTLY
#: ONE of ``version`` / ``content_hash`` — an unbounded "latest" body would let composition
#: silently drift, the same immutability discipline triggers hold (M12 §1.1). Validated in
#: ``validate_graph``.
_PINNED_BODY_TYPES: frozenset[str] = frozenset({"subgraph", "loop", "map"})

_IR_ADAPTER: TypeAdapter[IRDocument] = TypeAdapter(IRDocument)


class GraphValidationError(ValueError):
    """A semantic graph error beyond Pydantic shape validation (bad ``type``/``kind`` pairing,
    a dangling edge, a disconnected required port, a cycle). The control-plane maps it — like a
    Pydantic ``ValidationError`` — to a 400 with a precise message and creates no ``Run``."""


def parse_document(data: Any) -> IRDocument:
    """Validate a raw IR payload into an :class:`IRDocument` (the §8.2 envelope). Raises
    ``pydantic.ValidationError`` on a shape error — the caller maps it to 400."""

    return _IR_ADAPTER.validate_python(data)


def validate_graph(ir: IRDocument) -> None:
    """Static, execution-independent graph validation (§5 step 1-2). Raises
    :class:`GraphValidationError` on the first problem. This runs *before* a ``Run`` is created,
    so an invalid graph never persists anything.

    Checks, in order:
      1. every ``type`` is known and carries its one correct ``kind`` (§8.1 / ``NODE_TYPE_KIND``);
      2. per-``type`` ``config`` validates (M5: ``input``/``output``/``llm``);
      3. ``llm`` nodes reference a declared model key (§8.4);
      4. every edge references existing nodes *and* declared handles;
      4b. no in-port is fed by more than one ``data`` edge (M10 §3 — an ambiguous multi-input
          binding: which upstream value would ``$in.<port>`` mean?);
      5. every *required* in-port is fed by a ``data`` edge (M10 §3 — per-port, not per-node:
          a multi-input node must have each required port wired);
      6. no cycle among ``data`` edges (M5 has no ``loop``/``map`` — §7).
    """

    node_by_id = {n.id: n for n in ir.nodes}
    if len(node_by_id) != len(ir.nodes):
        raise GraphValidationError("duplicate node id")

    # 1-3: per-node validation.
    for node in ir.nodes:
        expected = NODE_TYPE_KIND.get(node.type)
        if expected is None:
            raise GraphValidationError(f"unknown node type {node.type!r}")
        if node.kind != expected:
            raise GraphValidationError(
                f"node {node.id!r}: type {node.type!r} must have kind {expected!r}, "
                f"got {node.kind!r}"
            )
        config_model = _CONFIG_MODELS.get(node.type)
        if config_model is not None:
            try:
                cfg = config_model.model_validate(node.config)
            except ValidationError as exc:
                raise GraphValidationError(f"node {node.id!r}: invalid config: {exc}") from exc
            if isinstance(cfg, LlmConfig) and cfg.model not in ir.models:
                raise GraphValidationError(
                    f"node {node.id!r}: references undeclared model {cfg.model!r}"
                )
            # M14 §1.2: a subgraph/loop/map composes a SAVED, PINNED agent — exactly one of
            # version / content_hash, so composition is immutable (never silently "latest"). The
            # pin is part of config, hence frozen into the parent's contentHash.
            if node.type in _PINNED_BODY_TYPES:
                version = getattr(cfg, "version", None)
                chash = getattr(cfg, "content_hash", None)
                if bool(version) == bool(chash):
                    raise GraphValidationError(
                        f"node {node.id!r}: a {node.type!r} body must pin EXACTLY ONE of `version` "
                        f"or `contentHash` (m14.md §1.2 — composition is immutable, never 'latest')"
                    )
            # M14 §1.3: a loop is bounded — max_iterations is required (Pydantic) AND must be ≥ 1
            # (no unbounded/zero-iteration loop). This is the termination + determinism guarantee.
            if isinstance(cfg, LoopConfig) and cfg.max_iterations < 1:
                raise GraphValidationError(
                    f"node {node.id!r}: loop `maxIterations` must be >= 1 "
                    f"(got {cfg.max_iterations}) — an unbounded or zero-iteration loop is "
                    "rejected (m14.md §1.3)"
                )

    # 4: edges reference existing nodes + declared handles.
    for edge in ir.edges:
        src = node_by_id.get(edge.source)
        tgt = node_by_id.get(edge.target)
        if src is None:
            raise GraphValidationError(f"edge {edge.id!r}: unknown source {edge.source!r}")
        if tgt is None:
            raise GraphValidationError(f"edge {edge.id!r}: unknown target {edge.target!r}")
        if edge.source_handle not in {p.id for p in src.ports.out}:
            raise GraphValidationError(
                f"edge {edge.id!r}: {edge.source!r} has no out-port {edge.source_handle!r}"
            )
        if edge.target_handle not in {p.id for p in tgt.ports.in_}:
            raise GraphValidationError(
                f"edge {edge.id!r}: {edge.target!r} has no in-port {edge.target_handle!r}"
            )

    # 4b: at most one ``data`` edge may target a given in-port. Two would be an ambiguous
    # multi-input binding (M10 §3) — ``$in.<port>`` could mean either upstream value — so reject
    # it statically. ``control`` edges impose no value and are unconstrained.
    fed_ports: set[tuple[str, str]] = set()
    for edge in ir.edges:
        if edge.channel != "data":
            continue
        key = (edge.target, edge.target_handle)
        if key in fed_ports:
            raise GraphValidationError(
                f"node {edge.target!r}: in-port {edge.target_handle!r} is fed by more than one "
                f"data edge (ambiguous multi-input binding)"
            )
        fed_ports.add(key)

    # 5: every REQUIRED in-port must be fed by a ``data`` edge (M10 §3 — per-port, not per-node).
    # An ``input`` boundary legitimately has no inbound edge; an in-port declared ``required:
    # false`` may be unfed (resolves to null at runtime). Without this the walker would read an
    # unbound port — the silent ``None`` M9 was written to forbid.
    for node in ir.nodes:
        if node.type == "input":
            continue
        for port in node.ports.in_:
            if port.required and (node.id, port.id) not in fed_ports:
                raise GraphValidationError(
                    f"node {node.id!r}: required in-port {port.id!r} is not connected"
                )

    # 6: cycle detection (Kahn over data + control edges; both impose ordering).
    topological_order(ir)


def topological_order(ir: IRDocument) -> list[Node]:
    """Order nodes so every edge points forward (§8.7 step 1). ``data`` edges thread values,
    ``control`` edges sequence — both constrain order. Raises :class:`GraphValidationError` on a
    cycle (M5 rejects cycles; ``loop``/``map`` arrive with the durable runtime — §7)."""

    node_by_id = {n.id: n for n in ir.nodes}
    indegree: dict[str, int] = {n.id: 0 for n in ir.nodes}
    successors: dict[str, list[str]] = {n.id: [] for n in ir.nodes}
    for edge in ir.edges:
        if edge.source not in node_by_id or edge.target not in node_by_id:
            # validate_graph reports this precisely; ignore here so topo can run standalone.
            continue
        successors[edge.source].append(edge.target)
        indegree[edge.target] += 1

    ready: deque[str] = deque(sorted(nid for nid, d in indegree.items() if d == 0))
    order: list[Node] = []
    while ready:
        nid = ready.popleft()
        order.append(node_by_id[nid])
        for nxt in successors[nid]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                ready.append(nxt)

    if len(order) != len(ir.nodes):
        raise GraphValidationError("graph has a cycle (M5 rejects cycles — no loop/map yet)")
    return order
