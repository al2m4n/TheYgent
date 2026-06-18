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

#: The node ``type``s the M5 walker actually executes. Every other known ``type`` is a valid
#: IR shape with a dispatcher branch that raises ``NotImplementedError`` — adding it later is
#: an additive walker handler, not a refactor (M5 §7).
EXECUTABLE_TYPES: frozenset[str] = frozenset({"input", "output", "llm"})


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
    node types that need it."""

    id: str
    type: str = "any"


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
    ``IRDocument.models``; ``messages`` is the prompt template — ``$input`` in a content field
    is substituted with the value threaded into the node's in-port at walk time."""

    model: str
    messages: list[_Message]


class InputConfig(_Wire):
    """``input`` boundary node — graph entry. No config in M5 (the run's ``input`` binds to
    its out-port); the model exists so an unexpected key fails validation, not silently."""


class OutputConfig(_Wire):
    """``output`` boundary node — graph exit. No config in M5 (its in-port value is the run's
    output)."""


_CONFIG_MODELS: dict[str, type[_Wire]] = {
    "llm": LlmConfig,
    "input": InputConfig,
    "output": OutputConfig,
}

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
      5. no node with a required in-port is left disconnected;
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

    # 5: no disconnected required in-port. An ``input`` boundary legitimately has no inbound
    # edge; every other node that declares in-ports must be fed, or the walker would read an
    # unbound value.
    fed: set[str] = {e.target for e in ir.edges}
    for node in ir.nodes:
        if node.type == "input":
            continue
        if node.ports.in_ and node.id not in fed:
            raise GraphValidationError(f"node {node.id!r}: required in-port is not connected")

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
