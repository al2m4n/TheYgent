"""The Agent Graph IR — the builder ↔ runtime seam.

The IR is the input format the walker executes, the no-code builder writes, the registry
versions, and observability maps spans onto. This module defines the document envelope,
nodes/edges, model bindings, and the determinism-class ``kind`` taxonomy; the inference-plane
registration payload lives in ``registration.py``.

Two rules this module encodes, both load-bearing:

* **Dispatch is by ``kind``, never ``type``.** ``kind``
  (``activity`` / ``orchestration`` / ``boundary``) is the determinism class the compiler
  lowers on; ``type`` (``llm`` / ``input`` / ``output`` / …) selects the *handler within a
  kind*. ``NODE_TYPE_KIND`` pins the one correct ``kind`` for every known ``type`` so a
  ``type``/``kind`` mismatch is rejected, not silently lowered wrong.
* **Layout never pollutes logic.** The optional ``view`` block holds React-Flow
  positions and is *never* hashed; ``content_hash`` (``contenthash.py``) is computed over the
  view-stripped, key-sorted JSON, so dragging a node never produces a "new version".

This package stays pure: Pydantic models + static graph algorithms (validation, topological
order). It imports no engine SDK and no gateway client — execution (the walker) lives in the
control-plane, which owns the inference seam. The IR owns the *format*, not the *run*.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator
from pydantic.alias_generators import to_camel

# ── the determinism taxonomy ─────────────────────────────────────────────────

NodeKind = Literal["activity", "orchestration", "boundary"]

#: The one correct ``kind`` for every known node ``type``. The walker dispatches on
#: ``kind``; this table is what makes a ``type``/``kind`` mismatch a validation error rather
#: than a wrong lowering. Extended additively as the plugin API opens.
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
    # audio model-call types + the gate seam (all activities)
    "transcribe": "activity",  # POST /v1/audio/transcriptions
    "speak": "activity",  # POST /v1/audio/speech
    "imagine": "activity",  # POST /v1/images/generations (text -> image ref)
    "ratelimit": "activity",  # per-key counter (I/O)
    "quota": "activity",  # accumulated-usage read (I/O)
    # ``guardrail`` is the first PER-INSTANCE kind: rule⇒orchestration, model⇒activity.
    # This is the table's DEFAULT (the rule backend, the default config); validate_graph derives the
    # real expected kind from each instance's ``check.type`` and enforces it (``_expected_kind``).
    "guardrail": "orchestration",
    # orchestration — deterministic control flow
    "router": "orchestration",
    "condition": "orchestration",
    "loop": "orchestration",
    "iterator": "orchestration",
    "map": "orchestration",
    "transform": "orchestration",  # deterministic reshape, inline, no I/O
}

#: Types whose ``kind`` is determined PER INSTANCE by their ``config``, not fixed in
#: ``NODE_TYPE_KIND``. ``validate_graph`` derives the expected kind via
#: ``_expected_kind`` and enforces it; the table value above is only the palette/default.
_PER_INSTANCE_KIND_TYPES: frozenset[str] = frozenset({"guardrail"})

#: The node ``type``s the walker actually executes. Every other known ``type`` is a valid IR
#: shape with a dispatcher branch that raises ``NotImplementedError`` — adding it later is an
#: additive walker handler, not a refactor.
#: ``human``/``subgraph`` (``boundary``) and ``loop``/``map`` (``orchestration``) are the
#: additive-lowering types. They run ONLY on the durable runtime (each lowers onto a DBOS
#: primitive: ``recv`` / child workflow / bounded inline repetition / durable-queue fan-out);
#: the non-durable walker still raises ``NotImplementedError`` for them.
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
        # the node palette — audio/image/gate/transform types
        "transcribe",
        "speak",
        "imagine",
        "guardrail",
        "ratelimit",
        "quota",
        "transform",
        # retrieval over a saved control-plane source (step-mode or llm capability)
        "rag",
    }
)


class _Wire(BaseModel):
    """camelCase on the wire (the builder/registry speak it), snake_case in code; reject
    unknown keys so an IR typo fails loudly instead of being silently dropped."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
        protected_namespaces=(),
    )


# ── model bindings ────────────────────────────────────────────────────────────

BindingName = Literal["mlx", "vllm", "llamacpp", "openai-compatible"]
SourceName = Literal["hf", "local-path", "url"]


class ModelBinding(_Wire):
    """A logical model the graph's nodes reference by key — the sovereignty dial.

    Nodes never hardcode a provider; they name a key in ``IRDocument.models`` and the
    ``binding`` resolves at runtime through the L0 gateway. ``binding`` names only the engines
    theygent manages (``mlx``/``vllm``/``llamacpp``) plus the catch-all ``openai-compatible``;
    ``source`` (where the *weights* come from) is orthogonal and never an engine.

    ``model`` is the id forwarded to the inference seam. The walker forwards ``model`` as the
    logical id the inference plane already knows (the logical-id invariant holds — an engine
    name here is rejected at the seam). ``source`` is optional: it answers a lifecycle question
    (fetch the weights from where?) that only matters once theygent manages the engine.
    """

    binding: BindingName
    model: str
    source: SourceName | None = None
    params: dict[str, Any] = Field(default_factory=dict)


# ── tools ─────────────────────────────────────────────────────────────────────


class BuiltinTool(_Wire):
    kind: Literal["builtin"]
    ref: str


class HttpTool(_Wire):
    """An http/REST/GraphQL tool binding — the workhorse + the substrate for action tools
    and crawler tools. ``connection`` is the id of an ``http_auth`` connection whose secret the
    handler injects SERVER-SIDE at step time — never inline in the IR. The optional ``template``
    names a provider request shape (e.g. ``slack.chat.postMessage``) for the small built-in action
    set; everything else is the raw http tool config on the node."""

    kind: Literal["http"]
    connection: str
    template: str | None = None
    # A model-callable http tool is SELF-DESCRIBING: ``description`` + ``parameter_schema``
    # (an OpenAI function ``parameters`` JSON-Schema) are what the model reads to decide the call.
    # The request shape lives here too, so the binding is a COMPLETE, reusable tool the llm
    # tool-loop invokes by reference (no wired node) — one HttpTool serves both invocation modes.
    # Auth stays in ``connection`` (injected server-side), never a slot. All optional, so a
    # node-wired http binding that omits them is unaffected.
    # For an http tool used by an llm, the validator requires ``description`` + ``parameter_schema``
    # AND that the schema's top-level properties equal the ``$in.*`` slots referenced by
    # ``url_template``/``body_template`` (slot↔property equivalence).
    description: str | None = None
    parameter_schema: dict[str, Any] | None = None
    method: str | None = None  # GET|POST|PUT|PATCH|DELETE
    url_template: str | None = (
        None  # $in.<param> slots; the model fills them via function.arguments
    )
    body_template: Any = None  # JSON body; $in.<param> slots at any depth
    headers: dict[str, str] = Field(default_factory=dict)  # connection adds auth headers at runtime
    response_map: str | None = None  # optional JSONPath ($.data) to shape the tool result
    idempotency_key: str | None = None  # replay-safe re-run for unsafe methods
    # A per-request timeout (mirrors ToolConfig.timeout_seconds), so a node-authored http tool
    # wired as a CAPABILITY keeps its configured timeout instead of dropping to the httpx default
    # (step-mode reads it from ToolConfig; capability-mode rebuilds a ToolConfig from this binding).
    timeout_seconds: float | None = None


class McpTool(_Wire):
    """An MCP tool binding. ``tool`` is the named tool the server exposes; the server is reached
    EITHER by the ``server`` name (a registered ``mcp_server``) OR by a ``connection`` id (an
    ``mcp_server`` connection, stdio or http, auth via its secret_ref). Exactly one of
    ``server`` / ``connection`` is set (validated below); no secret in the IR."""

    kind: Literal["mcp"]
    tool: str
    server: str | None = None
    connection: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> McpTool:
        if bool(self.server) == bool(self.connection):
            raise ValueError(
                "an mcp tool binding must set EXACTLY ONE of `server` "
                "(a registered server name) or `connection` (an mcp_server connection id)"
            )
        return self


ToolBinding = Annotated[BuiltinTool | HttpTool | McpTool, Field(discriminator="kind")]


# ── nodes & edges ─────────────────────────────────────────────────────────────


# The port roles / edge channels. A ``tool`` handle on an ``llm`` (its ``tools`` in-port)
# receives capability edges from configured tool nodes — the model may CALL those tools
# (autonomous tool-calling), distinct from a ``data`` edge (the tool runs as a step, output
# flows) and a ``control`` edge (pure sequencing).
PortRole = Literal["data", "control", "tool"]


class Port(_Wire):
    """A declared (not inferred) connection point, so the compiler can type-check edges.
    ``type`` is advisory (``any``/``error``); real port typing lands with the node types
    that need it.

    ``required`` is meaningful for **in**-ports: a required in-port must be fed by a ``data`` edge
    or the graph fails validation; ``required: false`` declares an in-port that may be left unfed
    and resolves to ``null`` at runtime. The default is required, so an unfed input is a loud
    validation error rather than a silent ``None`` — the multi-input analogue of the
    no-silent-pass-through rule. (Ignored on out-ports.)

    ``role`` is the channel a handle carries: ``data`` (the default — threads a value) or
    ``control`` (pure ordering: "run after this passes", no value). The editor renders the two
    distinctly and only allows data→data / control→control connections; an edge's ``channel`` is
    DERIVED from the handles it joins, not a separate toggle. The walker/compiler dispatch by edge
    ``channel`` exactly as before — ``role`` is the per-handle declaration the channel follows, so a
    port that omits ``role`` defaults to ``data`` and is unchanged in meaning."""

    id: str
    type: str = "any"
    required: bool = True
    role: PortRole = "data"


class Ports(_Wire):
    in_: list[Port] = Field(default_factory=list, alias="in")
    out: list[Port] = Field(default_factory=list)


EdgeChannel = Literal["data", "control", "tool"]


class Node(_Wire):
    """One graph node. ``id`` is stable — it is what an OTel span binds to later
    (stamped on per-node logs as the attach-point). ``config`` is ``type``-specific and
    validated against a per-``type`` schema in ``validate_graph`` — Pydantic at the boundary,
    not a free-form dict the walker trusts blindly."""

    id: str
    type: str
    kind: NodeKind
    label: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    ports: Ports = Field(default_factory=Ports)


class Edge(_Wire):
    """A directed connection. ``channel: data`` passes a value along the edge;
    ``channel: control`` is pure sequencing. ``condition`` is reserved for router-driven
    edges."""

    id: str
    source: str
    source_handle: str
    target: str
    target_handle: str
    channel: EdgeChannel = "data"
    condition: str | None = None


class IRDocument(_Wire):
    """A saved agent — one JSON document. ``id`` + ``version`` is the registry coordinate;
    ``content_hash`` (computed over the view-stripped graph) gives content-addressed deploys.
    ``view`` is the React-Flow layer and is **never** hashed."""

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


# ── per-type config schemas ───────────────────────────────────────────────────


# ── multimodal message content ────────────────────────────────────────────────
#
# The inference plane can serve vision locally. To drive a vision agent from a graph, an ``llm``
# node's message ``content`` must carry an image, not just text. These models mirror the OpenAI
# chat content-part shape: a ``content`` is EITHER a plain string (the original shape, unchanged)
# OR a list of parts, each a ``text`` or ``image_url`` block. A part's text and an image's url
# are both ``$in``-templatable, so the image enters from a graph in-port (``$in.image``) rather
# than being hardcoded. LiteLLM already forwards ``image_url`` parts, so the control-plane
# threads them through unchanged.


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


#: One block of a multimodal message ``content``. Discriminated on ``type`` so an unknown
#: block fails loudly (``_Wire`` forbids extra keys) rather than being silently dropped.
_ContentPart = Annotated[_TextContentPart | _ImageContentPart, Field(discriminator="type")]


class _Message(_Wire):
    # ``content`` is EITHER a plain string OR a list of OpenAI content parts (text + image_url)
    # so a vision agent can carry an image. Load-bearing invariant: a string validates as ``str``
    # and serializes byte-for-byte as a bare string (never wrapped in a list/object), so a
    # string-content IR's ``contentHash`` is stable — only a list ``content`` (the multimodal
    # form) changes the hash.
    role: Literal["system", "user", "assistant"]
    content: str | list[_ContentPart]


# ── tool-calling: tool_choice vocabulary (the OpenAI shape) ──────────────────


class _ToolFunctionName(_Wire):
    name: str


class _NamedToolChoice(_Wire):
    """Force a specific tool — OpenAI's ``{"type":"function","function":{"name":...}}``."""

    type: Literal["function"]
    function: _ToolFunctionName


#: ``tool_choice`` follows the OpenAI vocabulary: ``auto`` (model decides), ``none`` (never call a
#: tool — equivalent to a plain completion), ``required`` (must call one), or a named function.
ToolChoice = Literal["auto", "none", "required"] | _NamedToolChoice


class LlmConfig(_Wire):
    """``llm`` node config. ``model`` names a key in ``IRDocument.models``; ``messages`` is the
    prompt template — content fields use the port-addressed substitution token language shared with
    ``tool``/``mcp_tool``/``router``: ``$in`` is the default in-port ``in``, ``$in.<port>`` selects
    a named in-port (so one node composes multiple upstreams), ``$in.<port>.<field>`` drills in. An
    unknown port or token fails loudly, never silent literal pass-through. (The segment after
    ``$in.`` is a port name.)

    A message ``content`` may be a list of OpenAI content parts (text + image_url), not only a
    string, so a graph can drive a vision model (on ``/v1/chat/completions``). The ``$in`` grammar
    applies inside each text part and each image url, so an image enters from an in-port
    (``$in.image``); a plain-string ``content`` is unchanged and hashes identically (see
    ``_Message``)."""

    model: str
    messages: list[_Message]
    # ``messages`` content uses the token grammar: ``$in`` (default in-port ``in``),
    # ``$in.<port>`` (a named in-port — so one llm node can compose ``$in.file`` AND
    # ``$in.question``), ``$in.<port>.<field>`` (drill into it). Unknown port / unknown token →
    # loud error. The token lives inside the opaque content string — no schema change.
    #
    # Autonomous tool-calling. ``tools`` lists keys into ``IRDocument.tools`` the model may call;
    # EMPTY (the default) is a single-shot completion (the loop branch is taken only when ``tools``
    # is non-empty). ``tool_choice`` follows the OpenAI vocabulary; ``max_tool_iterations`` bounds
    # the loop (>= 1, validated). ``Node.config`` is hashed as the opaque authored dict (not
    # hydrated through this model), so a graph that omits these fields hashes unchanged.
    # There is NO compile-time model-capability gate (plane-split: no
    # inference Capabilities here) — a model that ignores ``tools`` emits no tool_calls, the loop
    # runs zero iterations, you get plain content.
    tools: list[str] = Field(default_factory=list)
    tool_choice: ToolChoice = "auto"
    max_tool_iterations: int = 8


# The I/O modality vocabulary (DISTINCT from the model ``Modality`` in registration.py —
# that is chat/vision/embeddings/audio.*; this is the payload SHAPE a boundary declares). Non-text
# payloads enter/leave as REFERENCES (a stored blob handle + content type), never multi-MB blobs
# journaled through step args.
InputModality = Literal["text", "audio", "image", "video", "json", "file"]
OutputModality = Literal["text", "audio", "image", "json"]


class InputConfig(_Wire):
    """``input`` boundary node — the single typed entry. ``modality`` declares the payload shape
    (text by default); ``schema`` is the JSON Schema of the expected payload that triggers map
    their source onto. Both are declarative — the walker still binds the run input to the out-port;
    modality/schema drive the editor + trigger mapping, not execution."""

    modality: InputModality = "text"
    payload_schema: dict[str, Any] | None = Field(default=None, alias="schema")


class OutputConfig(_Wire):
    """``output`` boundary node — the run's return value. ``modality`` is the result shape (text
    default); for audio/image it is a REFERENCE to the artifact a ``speak``/tool produced upstream.
    ``output`` performs NO side effect — posting/sending is a tool step."""

    modality: OutputModality = "text"
    payload_schema: dict[str, Any] | None = Field(default=None, alias="schema")


class ToolConfig(_Wire):
    """``tool`` activity node config. ``tool`` is a logical name: a key in ``ir.tools`` (an ``http``
    connection-backed binding or a ``builtin``) OR a directly-registered builtin
    (``echo``/``http_fetch``). Membership is checked in the control plane, not here
    (``packages/ir`` stays pure).

    Builtin path: ``args`` is the arg template — each value a literal or a ``$in``-reference
    (``$in`` = the whole in-port value, ``$in.a.b`` = a path into it) resolved before the call.

    Http path (when ``tool`` resolves to an ``http`` binding): the request is described by the
    additive fields below, all optional so a builtin ``tool`` node is byte-identical. The connection
    (resolved from ``ir.tools[tool].connection``) injects auth headers SERVER-SIDE — auth never
    appears in the IR or the rendered template. ``url_template``/``headers``/``body_template`` use
    the unified ``$in.field`` substitution; ``response_map`` is an optional JSONPath to shape the
    step output; ``idempotency_key`` (set for unsafe methods) makes a partial-failure re-run a
    provider no-op."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    # http-tool fields (all optional — present only when ``tool`` is an http binding).
    method: str | None = None  # GET|POST|PUT|PATCH|DELETE
    url_template: str | None = None  # supports $in.field; query string ok
    headers: dict[str, str] = Field(default_factory=dict)  # connection adds auth headers at runtime
    body_template: Any = None  # JSON body; for GraphQL: {"query": ..., "variables": {...}}
    timeout_seconds: float | None = None
    response_map: str | None = None  # optional JSONPath ($.data) to shape the step output
    idempotency_key: str | None = None  # set for unsafe methods (replay-safe re-run)
    # The binding lives ON the node. ``connection`` is the http_auth connection id (resolves
    # auth server-side — still an id, never a secret). ``description`` is the "use this when…"
    # hint the model reads to decide a call; ``parameter_schema`` is the OpenAI function schema —
    # both REQUIRED for an http tool node wired as a CAPABILITY (validated), so it self-describes.
    # A ``data``/``control``-wired (step-mode) tool node ignores them.
    connection: str | None = None
    description: str | None = None
    parameter_schema: dict[str, Any] | None = None


class RouterConfig(_Wire):
    """``router`` orchestration node config. ``select`` is a ``$in``-reference that must resolve
    to the **name of one of this node's outgoing handles** — handle-name routing, not an expression
    DSL. The walker follows only the edge(s) from the selected handle."""

    select: str


class McpToolConfig(_Wire):
    """``mcp_tool`` activity node config. The server is reached EITHER by the ``server`` name (the
    **logical name** of a registered MCP server, resolved by the manager) OR by a ``connection`` id
    (an ``mcp_server`` connection, stdio or http, whose auth is resolved SERVER-SIDE at step time —
    the IR carries no secret). EXACTLY ONE of ``server`` / ``connection`` is set (validated below).
    ``tool`` is the tool that server exposes; ``args`` is the same ``$in`` / ``$in.a.b`` arg
    template as a ``tool`` node — the IR seam matches a local tool, only the transport differs."""

    tool: str
    server: str | None = None
    connection: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    # The "use this when…" hint, surfaced when this mcp_tool is wired as an llm CAPABILITY.
    # Optional: an mcp tool already self-describes at runtime via the server's ``list_tools``
    # inputSchema, so a description only augments it; step-mode ignores it.
    description: str | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> McpToolConfig:
        if bool(self.server) == bool(self.connection):
            raise ValueError(
                "an mcp_tool node must set EXACTLY ONE of `server` (a registered server name) or "
                "`connection` (an mcp_server connection id)"
            )
        return self


# ── additive-lowering tier — config shapes only ────────────────────────────────
#
# Four node types whose ``kind`` is ``boundary`` (``human``/``subgraph``) or ``orchestration``
# (``loop``/``map``). The IR package owns their *shape*; the lowering onto DBOS primitives lives in
# the control-plane's durable compiler (the ``dbos`` import never reaches here). ``subgraph``/
# ``loop``/``map`` all compose a SAVED, PINNED agent as their body (never inline IR): the unit of
# composition/repetition/fan-out is one saved-agent invocation, which the durable runtime runs as
# ``theygent_run``. The pin is part of ``config`` and therefore part of the hashed IR, so it is
# **frozen into the parent's contentHash** — composition is immutable even when the child agent
# publishes a new version.


class HumanConfig(_Wire):
    """``human`` boundary node config — a durable wait for external input. The node lowers onto
    ``DBOS.recv``: the run persists as ``waiting`` and survives a worker crash while paused;
    ``POST /runs/{id}/resume`` (→ ``DBOS.send``) delivers the awaited input and the workflow
    resumes from the checkpoint. ``prompt`` describes what input is expected (advisory);
    ``input_schema`` is the awaited input's declared shape (advisory — resume records it, doesn't
    enforce a full JSON-Schema validation). ``timeout`` (seconds; ``None`` = wait forever) bounds
    the wait; on timeout the node fails honestly (``on_timeout='fail'``, the default → ``failed``
    run) or binds the declared ``default`` (``on_timeout='default'``)."""

    prompt: str | None = None
    input_schema: dict[str, Any] | None = None
    timeout: float | None = None
    on_timeout: Literal["fail", "default"] = "fail"
    default: Any = None


class SubgraphConfig(_Wire):
    """``subgraph`` boundary node config — an agent calls another SAVED agent, run as a DBOS child
    workflow (independently durable + resumable). ``agent`` is the child's IR id; the node pins it
    with EXACTLY ONE of ``version`` / ``content_hash`` (validated in ``validate_graph``) and the
    pin is frozen into the parent's contentHash, so composition is immutable. ``max_depth`` bounds
    recursion: exceeding it fails honestly, preventing unbounded / mutually-recursive expansion.
    The parent's in-port value is the child's run input (named ports); the child's output binds
    the node's success handle."""

    agent: str
    version: str | None = None
    content_hash: str | None = None
    max_depth: int = 8


class LoopConfig(_Wire):
    """``loop`` orchestration node config — bounded, deterministic agentic repetition
    (think→act→observe). Each iteration runs the pinned ``agent`` (a SAVED agent, child workflow);
    the previous iteration's output feeds the next as input. ``max_iterations`` is **REQUIRED** (no
    unbounded loops — the termination + determinism guarantee). ``condition`` (optional) is a
    ``$in``-reference over the iteration output (the same port-addressed grammar the router uses):
    the loop **stops early** when it resolves truthy. The loop control is deterministic
    orchestration with NO I/O — it reads journaled child results, never calls inference, so a crash
    mid-loop resumes at the last completed iteration (no completed iteration re-runs). ``max_depth``
    bounds nesting as in ``subgraph``."""

    agent: str
    version: str | None = None
    content_hash: str | None = None
    max_iterations: int
    condition: str | None = None
    max_depth: int = 8


class MapConfig(_Wire):
    """``map`` orchestration node config — durable fan-out/join over a collection. The in-port
    value must be a list; one task (the pinned ``agent`` run as a child workflow) is enqueued on a
    DBOS durable queue per element, then all results are durably awaited — so a crash mid-fan-out
    resumes ONLY the incomplete branches. ``concurrency`` bounds how many branches are in flight at
    once (``None`` = unbounded). ``on_error`` is the partial-failure policy: ``fail_fast`` (default
    — any element error fails the map) vs ``collect`` (gather successes + per-element errors).
    ``max_depth`` bounds nesting as in ``subgraph``."""

    agent: str
    version: str | None = None
    content_hash: str | None = None
    concurrency: int | None = None
    on_error: Literal["fail_fast", "collect"] = "fail_fast"
    max_depth: int = 8


# ── node palette additions — config shapes only ────────────────────────────────
#
# Additional node types. Each is a runtime-agnostic handler + a dispatch branch + this per-type
# ``config`` schema; no envelope/edge/``kind`` shape changes. ``transcribe``/``speak`` are the
# MODEL-call types (the two OpenAI audio endpoints); ``guardrail`` is the first type whose ``kind``
# is per-instance (rule⇒orchestration, model⇒activity); ``ratelimit``/``quota`` are the lean gate
# seam; ``transform`` is a deterministic reshape, NOT a code sandbox. The http/action/crawler tools
# reuse the existing ``tool`` type (its additive fields above), and ``switch`` is the existing
# ``router`` — no new types for those.


class TranscribeConfig(_Wire):
    """``transcribe`` activity node config — audio-ref in → text out, mapping 1:1 to
    ``POST /v1/audio/transcriptions``. ``model`` names a key in ``IRDocument.models`` whose
    binding's ``capabilities.modalities`` must include ``audio.transcription`` (data-driven — an
    incompatible binding is rejected at run, not silently); ``params`` (language/prompt/…) ride
    through to the endpoint. The audio bytes go to the inference base URL in the user's trust
    domain, never the control plane."""

    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class SpeakConfig(_Wire):
    """``speak`` activity node config — text in → audio-ref out, mapping 1:1 to
    ``POST /v1/audio/speech``. ``model`` names a ``models`` key whose binding advertises the
    ``audio.speech`` modality; ``params`` carries ``voice``/``format``/``speed``. The step's
    return value is a REFERENCE to the produced audio (bytes are an artifact, never journaled); it
    feeds an ``output`` (``modality:audio``) or an action ``tool``."""

    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class ImagineConfig(_Wire):
    """``imagine`` activity node config — text in → image-ref out, mapping 1:1 to
    ``POST /v1/images/generations``. The image analog of ``speak``: ``model`` names a ``models``
    key whose binding advertises the ``images.generation`` modality; ``params`` carries
    ``size``/``n``/``steps``. The step's return value is a REFERENCE to the produced image (bytes
    are an artifact, never journaled); it feeds an ``output`` (``modality:image``) or an action
    ``tool``."""

    model: str
    params: dict[str, Any] = Field(default_factory=dict)


class RagConfig(_Wire):
    """``rag`` activity node config — retrieve from a saved retrieval source.

    ``source`` is the STABLE id of a control-plane retrieval source (``rag_<ulid>``) — the same
    reference discipline as a connection id: the id is hashed content, the documents behind it
    are not, so re-ingesting/re-crawling a source never bumps an agent's ``contentHash``. The
    embedding model is pinned ON the source (control-plane side), NOT here — a
    ``models`` key would weld an inference binding to retrieval identity.

    Two wirings, like a tool node: as a STEP (``data`` edges), ``query`` is the query template
    (``$in`` grammar; ``None`` = the default in-port's whole value); as a CAPABILITY (a ``tool``
    edge into an llm's ``tools`` port), the model calls it with ``{query}`` and ``description``
    is the "use this when…" hint — ``query`` is ignored there (the model supplies it)."""

    source: str = Field(min_length=1)
    query: str | None = None
    top_k: int = Field(default=5, ge=1, le=50)
    min_similarity: float | None = Field(default=None, ge=-1.0, le=1.0)
    description: str | None = None


class GuardrailRule(_Wire):
    """A deterministic guardrail check backend (``check.type == "rule"``). ``kind`` selects the
    predicate; ``spec`` carries its knob (the regex, the min/max length, the json schema, the
    allow/deny list, the PII pattern set). Evaluated INLINE over journaled input — no I/O, no
    latency — so a rule guardrail is ``kind: orchestration``."""

    kind: Literal["regex", "length", "json_schema", "allow", "deny", "pii"]
    spec: dict[str, Any] = Field(default_factory=dict)


class GuardrailModel(_Wire):
    """A model guardrail check backend (``check.type == "model"``) — an LLM-judge "is this in
    scope?" classifier. ``model`` names a ``models`` key (a real data-plane call, so a model
    guardrail is ``kind: activity``); ``prompt`` is the judge question; ``pass_on`` is the model
    answer that PASSES (default ``"yes"``) — anything else routes to ``block``."""

    model: str
    prompt: str
    pass_on: str = "yes"


class GuardrailCheck(_Wire):
    """The guardrail's check. ``type`` picks the backend: ``rule`` (deterministic,
    ⇒orchestration) or ``model`` (a classifier call, ⇒activity). Exactly the matching sub-config
    is set; the node's ``kind`` follows the backend and is validated against it."""

    type: Literal["rule", "model"]
    rule: GuardrailRule | None = None
    model: GuardrailModel | None = None


class GuardrailConfig(_Wire):
    """``guardrail`` node config — a first-class check that short-circuits before downstream work.
    ``pass`` carries the input through; ``block`` carries the ``on_block`` payload (or the
    violation) — wire it to an ``output`` to refuse BEFORE the expensive node. ``kind`` is
    per-instance: the editor stamps ``orchestration`` for a rule check, ``activity`` for a model
    check, and ``validate_graph`` enforces the match."""

    check: GuardrailCheck
    on_block: dict[str, Any] = Field(default_factory=dict)  # e.g. {"message": "..."}


class RateLimitConfig(_Wire):
    """``ratelimit`` activity node config — the lean per-key gate seam. ``key_expr`` resolves the
    caller key from a trigger token / webhook signature or a ``$in`` field. Denies past ``limit``
    requests in ``window_seconds`` per key, backed by a trivial Postgres counter. ``policy: "deny"``
    emits the ``deny`` port (a clean 429-style result); ``policy: "wait"`` (a bounded suspend) is
    reserved but not implemented — only ``deny`` runs today."""

    key_expr: str
    limit: int
    window_seconds: int
    policy: Literal["deny", "wait"] = "deny"


class QuotaConfig(_Wire):
    """``quota`` activity node config — a token-budget gate that READS accumulated usage, never
    re-meters. ``source: "spans"`` rolls up the per-node token counts already on existing spans;
    denies past ``budget_tokens`` in ``window_seconds`` per key. Builds NO new metering pipeline."""

    key_expr: str
    budget_tokens: int
    window_seconds: int
    source: Literal["spans"] = "spans"


class TransformConfig(_Wire):
    """``transform`` orchestration node config — a lightweight, deterministic payload reshaper (a
    JSONata/jq-style ``expr``) so a builder maps ``A.out → B.in`` without a code node. NO sandbox,
    NO I/O — evaluated inline over journaled input (``kind: orchestration``). This is explicitly NOT
    the ``code`` type (sandboxed user code; a separate subsystem)."""

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
    # node palette — audio/image/gate/transform types
    "transcribe": TranscribeConfig,
    "speak": SpeakConfig,
    "imagine": ImagineConfig,
    "rag": RagConfig,
    "guardrail": GuardrailConfig,
    "ratelimit": RateLimitConfig,
    "quota": QuotaConfig,
    "transform": TransformConfig,
}

#: Node types that compose a saved, pinned agent body. Each must pin EXACTLY ONE of ``version`` /
#: ``content_hash`` — an unbounded "latest" body would let composition silently drift (the same
#: immutability discipline as trigger agents). Validated in ``validate_graph``.
_PINNED_BODY_TYPES: frozenset[str] = frozenset({"subgraph", "loop", "map"})

_IR_ADAPTER: TypeAdapter[IRDocument] = TypeAdapter(IRDocument)


class GraphValidationError(ValueError):
    """A semantic graph error beyond Pydantic shape validation (bad ``type``/``kind`` pairing,
    a dangling edge, a disconnected required port, a cycle). The control-plane maps it — like a
    Pydantic ``ValidationError`` — to a 400 with a precise message and creates no ``Run``."""


def parse_document(data: Any) -> IRDocument:
    """Validate a raw IR payload into an :class:`IRDocument`. Raises
    ``pydantic.ValidationError`` on a shape error — the caller maps it to 400."""

    return _IR_ADAPTER.validate_python(data)


# ── tool-calling: $in-slot extraction for the slot↔property equivalence check ─

_IN_SLOT_RE = re.compile(r"\$in(?:\.([A-Za-z_][A-Za-z0-9_]*))?")


def _in_slots(value: Any) -> tuple[set[str], bool]:
    """Extract the named ``$in.<slot>`` references in an http tool template (a string, or a JSON
    body scanned recursively over its string leaves). Returns ``(named_slots, has_whole)`` —
    ``has_whole`` is true if a bare ``$in`` (the whole args object) appears. ``$$`` escapes ``$``,
    so it is stripped before scanning. Used to validate an http tool's slots against its
    ``parameter_schema`` (slot↔property equivalence)."""

    named: set[str] = set()
    saw_whole = False

    def scan(v: Any) -> None:
        nonlocal saw_whole
        if isinstance(v, str):
            for m in _IN_SLOT_RE.finditer(v.replace("$$", "")):
                seg = m.group(1)
                if seg is None:
                    saw_whole = True
                else:
                    named.add(seg)
        elif isinstance(v, dict):
            for sub in v.values():
                scan(sub)
        elif isinstance(v, list):
            for sub in v:
                scan(sub)

    scan(value)
    return named, saw_whole


def _expected_kind(node: Node, cfg: _Wire | None) -> NodeKind | None:
    """The ``kind`` a node MUST carry. For most types this is the fixed ``NODE_TYPE_KIND`` value.
    For a PER-INSTANCE-kind type (only ``guardrail`` today) it is DERIVED from the validated
    config: a ``rule`` check ⇒ ``orchestration`` (no I/O, inline), a ``model`` check ⇒
    ``activity`` (a classifier call). Returns ``None`` for an unknown type (the caller errors)."""
    if node.type == "guardrail" and isinstance(cfg, GuardrailConfig):
        return "activity" if cfg.check.type == "model" else "orchestration"
    return NODE_TYPE_KIND.get(node.type)


def _model_refs(cfg: _Wire | None) -> list[str]:
    """Every ``models`` key a node's config references — so each is checked against ``ir.models``
    up front. ``llm``/``transcribe``/``speak``/``imagine`` name one model directly; a ``model``
    guardrail names one in ``check.model.model``. Anything else references no model."""
    if isinstance(cfg, LlmConfig | TranscribeConfig | SpeakConfig | ImagineConfig):
        return [cfg.model]
    if isinstance(cfg, GuardrailConfig) and cfg.check.type == "model" and cfg.check.model:
        return [cfg.check.model.model]
    return []


def validate_graph(ir: IRDocument) -> None:
    """Static, execution-independent graph validation. Raises :class:`GraphValidationError` on the
    first problem. This runs *before* a ``Run`` is created, so an invalid graph never persists
    anything.

    Checks, in order:
      1. every ``type`` is known and carries its one correct ``kind`` (``NODE_TYPE_KIND``);
      2. per-``type`` ``config`` validates (``input``/``output``/``llm`` and all palette types);
      3. ``llm`` nodes reference a declared model key;
      4. every edge references existing nodes *and* declared handles;
      4b. no in-port is fed by more than one ``data`` edge (an ambiguous multi-input binding:
          which upstream value would ``$in.<port>`` mean?);
      5. every *required* in-port is fed by a ``data`` edge (per-port, not per-node:
          a multi-input node must have each required port wired);
      6. no cycle among ``data`` edges (``loop``/``map`` only run on the durable runtime).
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
        # is also wrong), so check it BEFORE parsing config. For a PER-INSTANCE-kind type
        # (guardrail) the expected kind is DERIVED from the config, so parse the config first,
        # then derive + check the kind.
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
                    "model⇒activity)"
                )
        if cfg is not None:
            # Every model the node references must be a declared ``models`` key —
            # llm/transcribe/speak directly, a model guardrail via check.model.
            for model_key in _model_refs(cfg):
                if model_key not in ir.models:
                    raise GraphValidationError(
                        f"node {node.id!r}: references undeclared model {model_key!r}"
                    )
            # A guardrail's check must carry the sub-config matching its ``type`` (a rule check
            # needs ``rule``; a model check needs ``model``) — a loud shape error, never a
            # silently-empty check that would pass everything.
            if isinstance(cfg, GuardrailConfig):
                if cfg.check.type == "rule" and cfg.check.rule is None:
                    raise GraphValidationError(
                        f"node {node.id!r}: a rule guardrail needs check.rule"
                    )
                if cfg.check.type == "model" and cfg.check.model is None:
                    raise GraphValidationError(
                        f"node {node.id!r}: a model guardrail needs check.model"
                    )
            # A subgraph/loop/map composes a SAVED, PINNED agent — exactly one of version /
            # content_hash, so composition is immutable (never silently "latest"). The pin is part
            # of config, hence frozen into the parent's contentHash.
            if node.type in _PINNED_BODY_TYPES:
                version = getattr(cfg, "version", None)
                chash = getattr(cfg, "content_hash", None)
                if bool(version) == bool(chash):
                    raise GraphValidationError(
                        f"node {node.id!r}: a {node.type!r} body must pin EXACTLY ONE of `version` "
                        f"or `contentHash` (composition is immutable, never 'latest')"
                    )
                # The depth budget must admit at least the body itself: `maxDepth: 0` (or
                # negative) would pass every static check and only fail when the durable lowering
                # hits the depth guard at run time.
                max_depth = getattr(cfg, "max_depth", None)
                if max_depth is not None and max_depth < 1:
                    raise GraphValidationError(
                        f"node {node.id!r}: `maxDepth` must be >= 1 (got {max_depth}) — a "
                        "non-positive depth budget can never run its body"
                    )
            # A loop is bounded — max_iterations is required (Pydantic) AND must be ≥ 1 (no
            # unbounded/zero-iteration loop). This is the termination + determinism guarantee.
            if isinstance(cfg, LoopConfig) and cfg.max_iterations < 1:
                raise GraphValidationError(
                    f"node {node.id!r}: loop `maxIterations` must be >= 1 "
                    f"(got {cfg.max_iterations}) — an unbounded or zero-iteration loop is rejected"
                )
            # An llm with autonomous tools (pure-IR checks only — runtime model-capability is NOT
            # gated here, the plane-split). Empty `tools` is the single-shot path.
            if isinstance(cfg, LlmConfig) and cfg.tools:
                if cfg.max_tool_iterations < 1:
                    raise GraphValidationError(
                        f"node {node.id!r}: llm `maxToolIterations` must be >= 1 "
                        f"(got {cfg.max_tool_iterations})"
                    )
                if len(set(cfg.tools)) != len(cfg.tools):
                    raise GraphValidationError(
                        f"node {node.id!r}: duplicate key in llm `tools` {cfg.tools!r}"
                    )
                for tkey in cfg.tools:
                    if tkey not in ir.tools:
                        raise GraphValidationError(
                            f"node {node.id!r}: llm references undeclared tool {tkey!r} "
                            f"(declare it in the graph's `tools`)"
                        )
                if isinstance(cfg.tool_choice, _NamedToolChoice):
                    forced = cfg.tool_choice.function.name
                    if forced not in cfg.tools:
                        raise GraphValidationError(
                            f"node {node.id!r}: `toolChoice` names {forced!r}, which is not in "
                            f"this node's `tools` {cfg.tools!r}"
                        )
                # An http tool a model can call must SELF-DESCRIBE and its schema top-level
                # properties must equal the `$in.*` slots its url/body reference: a slot with no
                # property is unfillable, a property with no slot is dead. (MCP tools self-describe
                # at runtime via list_tools; builtin describability is checked in the control-plane
                # up-front, where the registry is visible.)
                for tkey in cfg.tools:
                    binding = ir.tools[tkey]
                    if not isinstance(binding, HttpTool):
                        continue
                    if binding.description is None or binding.parameter_schema is None:
                        raise GraphValidationError(
                            f"node {node.id!r}: http tool {tkey!r} used by an llm must set "
                            "`description` + `parameterSchema` (a model-callable tool "
                            "self-describes)"
                        )
                    props = set((binding.parameter_schema.get("properties") or {}).keys())
                    named, saw_whole = _in_slots(binding.url_template)
                    body_named, body_whole = _in_slots(binding.body_template)
                    slots = named | body_named
                    unfillable = slots - props
                    if unfillable:
                        raise GraphValidationError(
                            f"node {node.id!r}: http tool {tkey!r} template references $in slot(s) "
                            f"{sorted(unfillable)} with no matching `parameterSchema` property "
                            "(unfillable)"
                        )
                    dead = props - slots
                    if dead and not (saw_whole or body_whole):
                        raise GraphValidationError(
                            f"node {node.id!r}: http tool {tkey!r} `parameterSchema` declares "
                            f"propert(ies) {sorted(dead)} with no matching $in slot in the "
                            "url/body template (dead)"
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
    # multi-input binding — ``$in.<port>`` could mean either upstream value — so reject it
    # statically. ``control`` edges impose no value and are unconstrained.
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

    # 4c: a ``tool``-channel edge wires a configured tool node to an llm as a CAPABILITY (the model
    # may call it), distinct from a ``data`` edge (the tool runs as a step, output flows). It must
    # source a tool/mcp_tool node and target an ``llm``'s ``tool``-role in-port. A tool node is a
    # capability OR a step, never both (mixed out-channels are ambiguous). An http capability node
    # must self-describe (description + parameterSchema + slot↔property equivalence), read from
    # the node's own config.
    role_by_in_port = {(n.id, p.id): p.role for n in ir.nodes for p in n.ports.in_}
    for edge in ir.edges:
        if edge.channel != "tool":
            continue
        src = node_by_id[edge.source]
        tgt = node_by_id[edge.target]
        if src.type not in {"tool", "mcp_tool", "rag"}:
            raise GraphValidationError(
                f"edge {edge.id!r}: a `tool` edge must come from a tool/mcp_tool/rag node, "
                f"not {src.type!r}"
            )
        if tgt.type != "llm":
            raise GraphValidationError(
                f"edge {edge.id!r}: a `tool` edge must target an llm node, not {tgt.type!r}"
            )
        role = role_by_in_port.get((tgt.id, edge.target_handle))
        if role is not None and role != "tool":
            raise GraphValidationError(
                f"edge {edge.id!r}: a `tool` edge must target a `tool`-role port, but "
                f"{tgt.id!r}.{edge.target_handle!r} is role {role!r}"
            )

    # The inverse: a `tool`-role in-port may ONLY be fed by a `tool`-channel edge — a data/control
    # edge into an llm's tools port is a miswire (its value would never be consumed).
    for edge in ir.edges:
        if edge.channel == "tool":
            continue
        if role_by_in_port.get((edge.target, edge.target_handle)) == "tool":
            raise GraphValidationError(
                f"edge {edge.id!r}: a `{edge.channel}` edge cannot target the `tool`-role port "
                f"{edge.target!r}.{edge.target_handle!r} — only a `tool` capability edge may "
                "(only a `tool` capability edge may)"
            )

    for node in ir.nodes:
        if node.type not in {"tool", "mcp_tool", "rag"}:
            continue
        out_channels = {e.channel for e in ir.edges if e.source == node.id}
        is_capability = "tool" in out_channels
        if is_capability and out_channels - {"tool"}:
            raise GraphValidationError(
                f"node {node.id!r}: a tool node is either a capability (`tool` edges to an llm) "
                f"or a step (`data`/`control` edges), not both"
            )
        # NOTE: a capability node's id IS its `ir.tools` key — the editor derives the global tool
        # registry from the wired nodes (keyed by node id), so ``ir.tools[node.id]`` mirrors the
        # node config (like ir.models). Dispatch resolves the node id via either source, so the
        # id↔key coincidence is intentional, never a shadowing bug.
        if is_capability and node.type == "tool":
            cfg = ToolConfig.model_validate(node.config)
            if cfg.connection or cfg.url_template:  # an http tool (vs a builtin ref)
                if cfg.description is None or cfg.parameter_schema is None:
                    raise GraphValidationError(
                        f"node {node.id!r}: an http tool wired as a capability must set "
                        "`description` + `parameterSchema` (a model-callable tool self-describes)"
                    )
                props = set((cfg.parameter_schema.get("properties") or {}).keys())
                named, saw_whole = _in_slots(cfg.url_template)
                body_named, body_whole = _in_slots(cfg.body_template)
                slots = named | body_named
                unfillable = slots - props
                if unfillable:
                    raise GraphValidationError(
                        f"node {node.id!r}: http tool template references $in slot(s) "
                        f"{sorted(unfillable)} with no matching `parameterSchema` property "
                        "(unfillable)"
                    )
                dead = props - slots
                if dead and not (saw_whole or body_whole):
                    raise GraphValidationError(
                        f"node {node.id!r}: `parameterSchema` declares propert(ies) "
                        f"{sorted(dead)} with no matching $in slot (dead)"
                    )

    # 5: every REQUIRED in-port must be fed by a ``data`` edge (per-port, not per-node).
    # An ``input`` boundary legitimately has no inbound edge; an in-port declared ``required:
    # false`` may be unfed (resolves to null at runtime). Without this the walker would read an
    # unbound port — the silent ``None`` this rule was written to forbid. A CAPABILITY tool node
    # (with a ``tool`` out-edge) is exempt — its args are supplied by the model at call time, not
    # an upstream edge, so its ``in`` port is legitimately unfed.
    capability_nodes = {e.source for e in ir.edges if e.channel == "tool"}
    for node in ir.nodes:
        if node.type == "input" or node.id in capability_nodes:
            continue
        for port in node.ports.in_:
            if port.required and (node.id, port.id) not in fed_ports:
                raise GraphValidationError(
                    f"node {node.id!r}: required in-port {port.id!r} is not connected"
                )

    # 6: cycle detection (Kahn over data + control edges; both impose ordering).
    topological_order(ir)


def topological_order(ir: IRDocument) -> list[Node]:
    """Order nodes so every edge points forward. ``data`` edges thread values, ``control`` edges
    sequence — both constrain order. Raises :class:`GraphValidationError` on a cycle
    (``loop``/``map`` only run on the durable runtime; non-durable graphs must be acyclic)."""

    node_by_id = {n.id: n for n in ir.nodes}
    indegree: dict[str, int] = {n.id: 0 for n in ir.nodes}
    successors: dict[str, list[str]] = {n.id: [] for n in ir.nodes}
    for edge in ir.edges:
        if edge.source not in node_by_id or edge.target not in node_by_id:
            # validate_graph reports this precisely; ignore here so topo can run standalone.
            continue
        # A ``tool``-channel edge is a CAPABILITY wire (the llm may call the tool on-demand),
        # NOT sequencing — it imposes no order, so it is excluded from the topo sort (the walker
        # runs the capability lazily inside the llm step, never as a standalone node).
        if edge.channel == "tool":
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
        raise GraphValidationError("graph has a cycle (loop/map only run on the durable runtime)")
    return order
