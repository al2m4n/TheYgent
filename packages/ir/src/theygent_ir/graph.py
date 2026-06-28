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

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
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
    # M19 §2 — audio model-call types + the gate seam (all activities)
    "transcribe": "activity",  # §2.2 — POST /v1/audio/transcriptions
    "speak": "activity",  # §2.2 — POST /v1/audio/speech
    "ratelimit": "activity",  # §2.8 — per-key counter (I/O)
    "quota": "activity",  # §2.8 — accumulated-usage read (I/O)
    # M19 §1.2 — ``guardrail`` is the first PER-INSTANCE kind: rule⇒orchestration, model⇒activity.
    # This is the table's DEFAULT (the rule backend, the default config); validate_graph derives the
    # real expected kind from each instance's ``check.type`` and enforces it (``_expected_kind``).
    "guardrail": "orchestration",
    # orchestration — deterministic control flow
    "router": "orchestration",
    "condition": "orchestration",
    "loop": "orchestration",
    "iterator": "orchestration",
    "map": "orchestration",
    "transform": "orchestration",  # M19 §2.9 — deterministic reshape, inline, no I/O
}

#: Types whose ``kind`` is determined PER INSTANCE by their ``config``, not fixed in
#: ``NODE_TYPE_KIND`` (m19.md §1.2). ``validate_graph`` derives the expected kind via
#: ``_expected_kind`` and enforces it; the table value above is only the palette/default.
_PER_INSTANCE_KIND_TYPES: frozenset[str] = frozenset({"guardrail"})

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
    {
        "input",
        "output",
        "llm",
        "tool",
        "router",
        "mcp_tool",
        "human",
        "subgraph",
        "loop",
        "map",
        # M19 §2 — the node palette
        "transcribe",
        "speak",
        "guardrail",
        "ratelimit",
        "quota",
        "transform",
    }
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


class HttpTool(_Wire):
    """An http/REST/GraphQL tool binding (M19 §2.3) — the workhorse + the substrate for action tools
    (§2.5) and crawler tools (§1.4). ``connection`` is the id of an ``http_auth`` connection (M19
    §1.1) whose secret the handler injects SERVER-SIDE at step time — never inline in the IR. The
    optional ``template`` names a provider request shape (e.g. ``slack.chat.postMessage``) for the
    small built-in action set; everything else is the raw http tool config on the node (§2.3)."""

    kind: Literal["http"]
    connection: str
    template: str | None = None


class McpTool(_Wire):
    """An MCP tool binding (M7 + M19 §2.4). ``tool`` is the named tool the server exposes; the
    server is reached EITHER by the M7 ``server`` name (a registered ``mcp_server``) OR — M19 — by a
    ``connection`` id (an ``mcp_server`` connection, stdio or http, auth via its secret_ref).
    Exactly one of ``server`` / ``connection`` is set (validated below); no secret in the IR."""

    kind: Literal["mcp"]
    tool: str
    server: str | None = None
    connection: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> McpTool:
        if bool(self.server) == bool(self.connection):
            raise ValueError(
                "an mcp tool binding must set EXACTLY ONE of `server` (M7 registered name) or "
                "`connection` (M19 mcp_server connection id)"
            )
        return self


ToolBinding = Annotated[BuiltinTool | HttpTool | McpTool, Field(discriminator="kind")]


# ── nodes & edges (§8.3) ──────────────────────────────────────────────────────


PortRole = Literal["data", "control"]


class Port(_Wire):
    """A declared (not inferred) connection point, so the compiler can type-check edges
    (§8.3). ``type`` is advisory in M5 (``any``/``error``); real port typing lands with the
    node types that need it.

    ``required`` is meaningful for **in**-ports (M10 §3): a required in-port must be fed by a
    ``data`` edge or the graph fails validation; ``required: false`` declares an in-port that may
    be left unfed and resolves to ``null`` at runtime. The default is required, so an unfed input
    is a loud validation error rather than a silent ``None`` — the multi-input analogue of M9's
    no-silent-pass-through rule. (Ignored on out-ports.)

    ``role`` (M19 §2.10) is the channel a handle carries: ``data`` (the default — threads a value)
    or ``control`` (pure ordering: "run after this passes", no value). The editor renders the two
    distinctly and only allows data→data / control→control connections; an edge's ``channel`` is
    DERIVED from the handles it joins, not a separate toggle. The walker/compiler dispatch by edge
    ``channel`` exactly as before — ``role`` is the per-handle declaration the channel follows, so a
    pre-M19 IR (all ports default ``data``) is unchanged in meaning. This is the ONLY IR-shape
    addition in M19 (§0/§5)."""

    id: str
    type: str = "any"
    required: bool = True
    role: PortRole = "data"


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


# ── multimodal message content (M20 cross-plane follow-up) ────────────────────
#
# M20 made the inference plane serve vision locally (mlx_vlm.server on /v1/chat/completions). To
# DRIVE a vision agent from a graph, an ``llm`` node's message ``content`` must carry an image, not
# just text. These models mirror the OpenAI chat content-part shape: a ``content`` is EITHER a plain
# string (pre-M20 — unchanged) OR a list of parts, each a ``text`` or ``image_url`` block. A part's
# text and an image's url are both ``$in``-templatable, so the image enters from a graph in-port
# (``$in.image``) rather than being hardcoded — the whole point of "drive a vision agent from a
# graph". LiteLLM already forwards ``image_url`` parts, so the control-plane threads them through
# unchanged (m20.md follow-up).


class _TextContentPart(_Wire):
    """An OpenAI ``text`` content part. ``text`` is rendered through the same ``$in`` template
    language as a plain-string ``content`` (so a part can still compose an upstream value)."""

    type: Literal["text"]
    text: str


class _ImageUrl(_Wire):
    """The ``image_url`` payload of an image content part (OpenAI shape). ``url`` is an http(s) URL
    or a ``data:`` URI (base64-inline); it is ``$in``-templatable so the image can come from a graph
    in-port. ``detail`` is OpenAI's optional fidelity hint (``low``/``high``/``auto``)."""

    url: str
    detail: str | None = None


class _ImageContentPart(_Wire):
    """An OpenAI ``image_url`` content part — the block that carries a vision input."""

    type: Literal["image_url"]
    image_url: _ImageUrl


#: One block of a multimodal message ``content`` (M20). Discriminated on ``type`` so an unknown
#: block fails loudly (``_Wire`` forbids extra keys) rather than being silently dropped.
_ContentPart = Annotated[_TextContentPart | _ImageContentPart, Field(discriminator="type")]


class _Message(_Wire):
    # M20 cross-plane follow-up: ``content`` is EITHER a plain string (the pre-M20 shape, unchanged)
    # OR a list of OpenAI content parts (text + image_url) so a vision agent can carry an image. The
    # union is backward-compatible by construction: a string validates as ``str`` and serializes
    # byte-for-byte as before, so a pre-M20 IR's ``contentHash`` is UNMOVED (the canonical dump is
    # identical — contenthash.py decision D2). A list ``content`` is the new multimodal form. Only a
    # list ``content`` changes the hash; a string does not — so ``schemaVersion`` is NOT bumped
    # (the addition is purely additive + back-compat: name the extension, don't break the frozen
    # surface).
    role: Literal["system", "user", "assistant"]
    content: str | list[_ContentPart]


class LlmConfig(_Wire):
    """``llm`` node config: a single model call, no tool loop (§8.3). ``model`` names a key in
    ``IRDocument.models``; ``messages`` is the prompt template — content fields use the ONE
    port-addressed substitution token language shared with ``tool``/``mcp_tool``/``router``
    (§8.5 / m10.md §1.2): ``$in`` is the default in-port ``in``, ``$in.<port>`` selects a named
    in-port (so one node composes multiple upstreams), ``$in.<port>.<field>`` drills in. An unknown
    port or token fails loudly, never silent literal pass-through. (``$input`` was the M5 spelling,
    renamed to ``$in`` in M9; M10 made the segment after ``$in.`` a port name — see m10.md.)

    M20 cross-plane follow-up: a message ``content`` may be a list of OpenAI content parts (text +
    image_url), not only a string, so a graph can drive a vision model (mlx_vlm on
    ``/v1/chat/completions``). The ``$in`` grammar applies inside each text part and each image url,
    so an image enters from an in-port (``$in.image``); a plain-string ``content`` is unchanged and
    hashes identically (see ``_Message``)."""

    model: str
    messages: list[_Message]
    # ``messages`` content uses the §8.5 token grammar: ``$in`` (default in-port ``in``),
    # ``$in.<port>`` (a named in-port — so one llm node can compose ``$in.file`` AND
    # ``$in.question``), ``$in.<port>.<field>`` (drill into it). Unknown port / unknown token →
    # loud error (m10.md §1.2). The token lives inside the opaque content string — no schema change.


# M19 §2.1: the I/O modality vocabulary (DISTINCT from the model ``Modality`` in registration.py —
# that is chat/vision/embeddings/audio.*; this is the payload SHAPE a boundary declares). Non-text
# payloads enter/leave as REFERENCES (a stored blob handle + content type), never multi-MB blobs
# journaled through step args (m13-dbos.md §3 / M17 §1.3).
InputModality = Literal["text", "audio", "image", "video", "json", "file"]
OutputModality = Literal["text", "audio", "image", "json"]


class InputConfig(_Wire):
    """``input`` boundary node — the single typed entry (M19 §2.1, made real). ``modality`` declares
    the payload shape (text by default — a pre-M19 input is unchanged); ``schema`` is the JSON
    Schema of the expected payload that M12 triggers map their source onto (§1.5). Both are
    declarative — the walker still binds the run input to the out-port; modality/schema drive the
    editor + trigger mapping, not execution."""

    modality: InputModality = "text"
    payload_schema: dict[str, Any] | None = Field(default=None, alias="schema")


class OutputConfig(_Wire):
    """``output`` boundary node — the run's return value (M19 §2.1, made real). ``modality`` is the
    result shape (text default); for audio/image it is a REFERENCE to the artifact a ``speak``/tool
    produced upstream. ``output`` performs NO side effect (§0) — posting/sending is a tool step."""

    modality: OutputModality = "text"
    payload_schema: dict[str, Any] | None = Field(default=None, alias="schema")


class ToolConfig(_Wire):
    """``tool`` activity node config (§8.3 / m6.md §2/§3.1 + M19 §2.3). ``tool`` is a logical name:
    a key in ``ir.tools`` (an ``http`` connection-backed binding — M19 — or an M6 ``builtin``) OR a
    directly-registered builtin (``echo``/``http_fetch``). Membership is checked in the control
    plane, not here (``packages/ir`` stays pure).

    M6 path (builtin): ``args`` is the arg template — each value a literal or a ``$in``-reference
    (``$in`` = the whole in-port value, ``$in.a.b`` = a path into it) resolved before the call.

    M19 http path (when ``tool`` resolves to an ``http`` binding, §2.3): the request is described by
    the additive fields below, all optional so a builtin ``tool`` node is byte-identical to M6. The
    connection (resolved from ``ir.tools[tool].connection``) injects auth headers SERVER-SIDE — auth
    never appears in the IR or the rendered template. ``url_template``/``headers``/``body_template``
    use the unified ``$in.field`` substitution; ``response_map`` is an optional JSONPath to shape
    the step output; ``idempotency_key`` (set for unsafe methods — §2.5) makes a partial-failure
    re-run a provider no-op."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    # M19 §2.3 http-tool fields (all optional — present only when ``tool`` is an http binding).
    method: str | None = None  # GET|POST|PUT|PATCH|DELETE
    url_template: str | None = None  # supports $in.field; query string ok
    headers: dict[str, str] = Field(default_factory=dict)  # connection adds auth headers at runtime
    body_template: Any = None  # JSON body; for GraphQL: {"query": ..., "variables": {...}}
    timeout_seconds: float | None = None
    response_map: str | None = None  # optional JSONPath ($.data) to shape the step output
    idempotency_key: str | None = None  # §2.5 — set for unsafe methods (replay-safe re-run)


class RouterConfig(_Wire):
    """``router`` orchestration node config (§8.3 / m6.md §3.2). ``select`` is a ``$in``-reference
    that must resolve to the **name of one of this node's outgoing handles** — handle-name routing,
    not an expression DSL. The walker follows only the edge(s) from the selected handle."""

    select: str


class McpToolConfig(_Wire):
    """``mcp_tool`` activity node config (§8.5 / m7.md §2 + M19 §2.4). The server is reached EITHER
    by the M7 ``server`` (the **logical name** of a registered MCP server, resolved by the manager)
    OR — M19 — by a ``connection`` id (an ``mcp_server`` connection, stdio or http, whose auth is
    resolved SERVER-SIDE at step time — the IR carries no secret, §1.1). EXACTLY ONE of
    ``server`` / ``connection`` is set (validated below). ``tool`` is the tool that server exposes;
    ``args`` is the same ``$in`` / ``$in.a.b`` arg template as M6's ``tool`` — the IR seam matches a
    local tool, only the transport differs."""

    tool: str
    server: str | None = None
    connection: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_target(self) -> McpToolConfig:
        if bool(self.server) == bool(self.connection):
            raise ValueError(
                "an mcp_tool node must set EXACTLY ONE of `server` (M7 registered name) or "
                "`connection` (M19 mcp_server connection id)"
            )
        return self


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


# ── M19 the node palette (m19.md §2) — config shapes only ─────────────────────
#
# Six genuinely-new node types M19 adds to the palette. Each is a runtime-agnostic handler + a
# dispatch branch + this per-type ``config`` schema; no envelope/edge/``kind`` shape moves (m19.md
# §0). ``transcribe``/``speak`` are the only new MODEL-call types (the two OpenAI audio endpoints,
# §2.2); ``guardrail`` is the first type whose ``kind`` is per-instance (§1.2 — rule⇒orchestration,
# model⇒activity); ``ratelimit``/``quota`` are the lean gate seam (§2.8/§1.6); ``transform`` is a
# deterministic reshape, NOT a code sandbox (§2.9). The http/action/crawler tools reuse the existing
# ``tool`` type (its M19 fields above), and ``switch`` is the existing ``router`` (§2.7) — no new
# types for those.


class TranscribeConfig(_Wire):
    """``transcribe`` activity node config (m19.md §2.2) — audio-ref in → text out, mapping 1:1 to
    ``POST /v1/audio/transcriptions`` (M18 §1.1). ``model`` names a key in ``IRDocument.models``
    whose binding's ``capabilities.modalities`` must include ``audio.transcription`` (data-driven —
    an incompatible binding is rejected at run, not silently); ``params`` (language/prompt/…) ride
    through to the endpoint. The audio bytes go to the inference base URL in the user's trust
    domain, never the control plane (§10 / M18 §1.4)."""

    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class SpeakConfig(_Wire):
    """``speak`` activity node config (m19.md §2.2) — text in → audio-ref out, mapping 1:1 to
    ``POST /v1/audio/speech`` (M18 §1.1). ``model`` names a ``models`` key whose binding advertises
    the ``audio.speech`` modality; ``params`` carries ``voice``/``format``/``speed``. The step's
    return value is a REFERENCE to the produced audio (bytes are an artifact, never journaled —
    m13-dbos.md §6); it feeds an ``output`` (``modality:audio``) or an action ``tool``."""

    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class GuardrailRule(_Wire):
    """A deterministic guardrail check backend (m19.md §2.6, ``check.type == "rule"``). ``kind``
    selects the predicate; ``spec`` carries its knob (the regex, the min/max length, the json
    schema, the allow/deny list, the PII pattern set). Evaluated INLINE over journaled input — no
    I/O, no latency — so a rule guardrail is ``kind: orchestration`` (§1.2)."""

    kind: Literal["regex", "length", "json_schema", "allow", "deny", "pii"]
    spec: dict[str, Any] = Field(default_factory=dict)


class GuardrailModel(_Wire):
    """A model guardrail check backend (m19.md §2.6, ``check.type == "model"``) — an LLM-judge "is
    this in scope?" classifier. ``model`` names a ``models`` key (a real data-plane call, so a model
    guardrail is ``kind: activity`` — §1.2); ``prompt`` is the judge question; ``pass_on`` is the
    model answer that PASSES (default ``"yes"``) — anything else routes to ``block``."""

    model: str
    prompt: str
    pass_on: str = "yes"


class GuardrailCheck(_Wire):
    """The guardrail's check (m19.md §2.6). ``type`` picks the backend: ``rule`` (deterministic,
    ⇒orchestration) or ``model`` (a classifier call, ⇒activity). Exactly the matching sub-config is
    set; the node's ``kind`` follows the backend and is validated against it (§1.2)."""

    type: Literal["rule", "model"]
    rule: GuardrailRule | None = None
    model: GuardrailModel | None = None


class GuardrailConfig(_Wire):
    """``guardrail`` node config (m19.md §2.6) — a first-class check that short-circuits before
    downstream work. ``pass`` carries the input through; ``block`` carries the ``on_block`` payload
    (or the violation) — wire it to an ``output`` to refuse BEFORE the expensive node. ``kind`` is
    per-instance (§1.2): the editor stamps ``orchestration`` for a rule check, ``activity`` for a
    model check, and ``validate_graph`` enforces the match."""

    check: GuardrailCheck
    on_block: dict[str, Any] = Field(default_factory=dict)  # e.g. {"message": "..."}


class RateLimitConfig(_Wire):
    """``ratelimit`` activity node config (m19.md §2.8/§1.6) — the lean per-key gate seam.
    ``key_expr`` resolves the caller key from the M12 token / webhook signature or a ``$in`` field
    (per-USER is the deferred identity work — §1.6). Denies past ``limit`` requests in
    ``window_seconds`` per key, backed by a trivial Postgres counter. ``policy: "deny"`` emits the
    ``deny`` port (a clean 429-style result); ``policy: "wait"`` (a bounded suspend) is NAMED, not
    built in M19 — start with deny."""

    key_expr: str
    limit: int
    window_seconds: int
    policy: Literal["deny", "wait"] = "deny"


class QuotaConfig(_Wire):
    """``quota`` activity node config (m19.md §2.8/§1.6) — a token-budget gate that READS
    accumulated usage, never re-meters (§1.6). ``source: "spans"`` rolls up the per-node token
    counts already on M17 spans; denies past ``budget_tokens`` in ``window_seconds`` per key.
    Builds NO new metering pipeline (the §8 Do-NOT)."""

    key_expr: str
    budget_tokens: int
    window_seconds: int
    source: Literal["spans"] = "spans"


class TransformConfig(_Wire):
    """``transform`` orchestration node config (m19.md §2.9) — a lightweight, deterministic payload
    reshaper (a JSONata/jq-style ``expr``) so a builder maps ``A.out → B.in`` without a code node.
    NO sandbox, NO I/O — evaluated inline over journaled input (``kind: orchestration``). This is
    explicitly NOT the ``code`` type (sandboxed user code; a subsystem; deferred — M14 §4)."""

    expr: str


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
    # M19 §2 — the node palette
    "transcribe": TranscribeConfig,
    "speak": SpeakConfig,
    "guardrail": GuardrailConfig,
    "ratelimit": RateLimitConfig,
    "quota": QuotaConfig,
    "transform": TransformConfig,
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


def _expected_kind(node: Node, cfg: _Wire | None) -> NodeKind | None:
    """The ``kind`` a node MUST carry (§8.1). For most types this is the fixed ``NODE_TYPE_KIND``
    value. For a PER-INSTANCE-kind type (m19.md §1.2 — only ``guardrail`` today) it is DERIVED from
    the validated config: a ``rule`` check ⇒ ``orchestration`` (no I/O, inline), a ``model`` check
    ⇒ ``activity`` (a classifier call). Returns ``None`` for an unknown type (the caller errors)."""
    if node.type == "guardrail" and isinstance(cfg, GuardrailConfig):
        return "activity" if cfg.check.type == "model" else "orchestration"
    return NODE_TYPE_KIND.get(node.type)


def _model_refs(cfg: _Wire | None) -> list[str]:
    """Every ``models`` key a node's config references (§8.4) — so each is checked against
    ``ir.models`` up front. ``llm``/``transcribe``/``speak`` name one model directly; a ``model``
    guardrail names one in ``check.model.model``. Anything else references no model."""
    if isinstance(cfg, LlmConfig | TranscribeConfig | SpeakConfig):
        return [cfg.model]
    if isinstance(cfg, GuardrailConfig) and cfg.check.type == "model" and cfg.check.model:
        return [cfg.check.model.model]
    return []


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
        if node.type not in NODE_TYPE_KIND:
            raise GraphValidationError(f"unknown node type {node.type!r}")
        per_instance = node.type in _PER_INSTANCE_KIND_TYPES
        config_model = _CONFIG_MODELS.get(node.type)
        cfg: _Wire | None = None
        # For a FIXED-kind type the type/kind mismatch is the clearest error (even when the config
        # is also wrong), so check it BEFORE parsing config — the M5 message order. For a
        # PER-INSTANCE-kind type (guardrail) the expected kind is DERIVED from the config, so parse
        # the config first, then derive + check the kind (m19.md §1.2).
        if not per_instance:
            fixed = NODE_TYPE_KIND[node.type]
            if node.kind != fixed:
                raise GraphValidationError(
                    f"node {node.id!r}: type {node.type!r} must have kind {fixed!r}, "
                    f"got {node.kind!r}"
                )
        if config_model is not None:
            try:
                cfg = config_model.model_validate(node.config)
            except ValidationError as exc:
                raise GraphValidationError(f"node {node.id!r}: invalid config: {exc}") from exc
        if per_instance:
            expected = _expected_kind(node, cfg)
            if node.kind != expected:
                raise GraphValidationError(
                    f"node {node.id!r}: type {node.type!r} must have kind {expected!r}, got "
                    f"{node.kind!r} (a guardrail's kind follows its check: rule⇒orchestration, "
                    "model⇒activity — m19.md §1.2)"
                )
        if cfg is not None:
            # Every model the node references must be a declared ``models`` key (§8.4) —
            # llm/transcribe/speak directly, a model guardrail via check.model (m19.md §1.2/§2.2).
            for model_key in _model_refs(cfg):
                if model_key not in ir.models:
                    raise GraphValidationError(
                        f"node {node.id!r}: references undeclared model {model_key!r}"
                    )
            # M19 §2.6: a guardrail's check must carry the sub-config matching its ``type`` (a rule
            # check needs ``rule``; a model check needs ``model``) — a loud shape error, never a
            # silently-empty check that would pass everything.
            if isinstance(cfg, GuardrailConfig):
                if cfg.check.type == "rule" and cfg.check.rule is None:
                    raise GraphValidationError(
                        f"node {node.id!r}: a rule guardrail needs check.rule (m19.md §2.6)"
                    )
                if cfg.check.type == "model" and cfg.check.model is None:
                    raise GraphValidationError(
                        f"node {node.id!r}: a model guardrail needs check.model (m19.md §2.6)"
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
