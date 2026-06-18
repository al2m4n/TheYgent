"""The IR walker — M5's IP: walk an :class:`~theygent_ir.IRDocument` node by node and
execute it against the existing M3/M4 spine (theygent docs/private/m5.md §5).

**This is an interpreter, not a compiler (M5's one rule).** The walker lowers each node to
in-process Python now; the §8.7 compiler will later re-target the same IR to a durable runtime
(Temporal/Restate/DBOS). The seam — IR in, a stream of deltas out — is identical either way;
that is the entire point of §8.1's determinism split. So the walker imports no durable-runtime
SDK, wraps no node in retries/checkpoints, and persists no per-node intermediate state. M5's
"durability" is exactly ``/runs`` today: the ``Run`` row records the final outcome.

Two seam rules it holds (M5 §3.2 / §5):

* **Dispatch is by ``kind``** (``boundary``/``activity``/``orchestration``), then by ``type``
  *within* a kind — so adding a node type later is an additive handler, and the §8.7 compiler
  can lower the same IR to a durable target with the same dispatch.
* **No DB calls inside the walker.** It is a pure async function over a :class:`WalkContext`:
  the control-plane loads thread memory and persists the ``Run`` through the same M4 seams
  ``/runs`` uses, then hands the walker the prior messages and the gateway client. The walker
  asks for nothing it cannot get from ``ctx``.
"""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from theygent_gateway_client import GatewayClient
from theygent_ir import IRDocument, LlmConfig, Node, topological_order

logger = logging.getLogger("theygent.control_plane.walker")

# The managed-engine names (theygent-graph-schema.md §8.4). A resolved model id that is an
# engine name is not a logical id — the §9.1.1 invariant the inference seam enforces. Mirrors
# the same constant in ``app.py`` for the ``/runs`` path; both guard the one seam rule.
_ENGINE_NAMES = frozenset({"mlx", "vllm", "llamacpp"})


class EngineNameNotAllowed(ValueError):
    """A node resolved its model to an engine name, not a logical id (§8.4 / §3.2). Raised at
    resolution time so the control-plane rejects it up front — before a ``Run`` exists and
    before anything reaches the wire — exactly as ``/runs`` rejects an engine name today."""


@dataclass(frozen=True)
class Delta:
    """One streamed token piece, tagged with the node that produced it. The control-plane's SSE
    relay turns a ``Delta`` into ``event: delta`` / ``data: {runId, delta}`` — the *same* shape
    ``/runs`` emits, so a graph run is indistinguishable on the wire from a prompt run."""

    node_id: str
    content: str


@dataclass
class WalkContext:
    """Everything the walker needs that it must not fetch itself (M5 §5). ``prior_messages`` is
    the thread replay the control-plane already loaded via the M4 store; ``extra_headers`` carries
    the ``x-theygent-run-id`` correlation header. The walker does no I/O beyond the gateway call."""

    gateway: GatewayClient
    run_id: str
    prior_messages: list[dict[str, Any]] = field(default_factory=list)
    extra_headers: Mapping[str, str] = field(default_factory=dict)


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
    (b) record the resolved logical id on the ``Run`` (M5 trivial graph has exactly one)."""

    out: list[tuple[Node, str, dict[str, Any]]] = []
    for node in ir.nodes:
        if node.type == "llm":
            model_id, params = resolve_model(ir, LlmConfig.model_validate(node.config))
            out.append((node, model_id, params))
    return out


def _render_messages(config: LlmConfig, input_value: Any) -> list[dict[str, str]]:
    """Substitute the ``$input`` placeholder with the value threaded into the node's in-port.
    M5's templating is deliberately the simplest thing that runs the trivial graph — a literal
    ``$input`` swap; a real expression language is a later, evidence-driven addition."""

    rendered: list[dict[str, str]] = []
    for msg in config.messages:
        content = msg.content.replace("$input", "" if input_value is None else str(input_value))
        rendered.append({"role": msg.role, "content": content})
    return rendered


def _incoming_value(node: Node, edges: list, values: dict[tuple[str, str], Any]) -> Any:
    """The value arriving on a node's in-port via a ``data`` edge (§8.7 step 3: data edges are
    typed arguments). M5 nodes have a single in-port; the first connected data edge feeds it."""

    for edge in edges:
        if edge.target == node.id and edge.channel == "data":
            return values.get((edge.source, edge.source_handle))
    return None


async def walk(ir: IRDocument, input_value: Any, ctx: WalkContext) -> AsyncIterator[Delta]:
    """Walk ``ir`` node by node, yielding the ``llm`` node's deltas as they stream (M5 §5).

    Assumes a *validated* IR (the control-plane runs ``validate_graph`` before creating the
    ``Run``, so an invalid graph never persists). Threads ``data`` edges as values through a
    ``(node_id, out_port) -> value`` map; ``control`` edges only sequence. Each node's ``id`` is
    stamped on a structured log keyed by ``run_id`` — the OTel attach-point, not OTel itself."""

    values: dict[tuple[str, str], Any] = {}
    for node in topological_order(ir):
        logger.info(
            "graph.node",
            extra={
                "run_id": ctx.run_id,
                "node_id": node.id,
                "node_kind": node.kind,
                "node_type": node.type,
            },
        )
        if node.kind == "boundary":
            _walk_boundary(node, ir, input_value, values)
        elif node.kind == "activity":
            async for delta in _walk_activity(node, ir, edges=ir.edges, ctx=ctx, values=values):
                yield delta
        elif node.kind == "orchestration":
            # No orchestration node executes in M5 (no router/loop/map — §7). The branch exists
            # so the first one is an additive handler, not a dispatcher refactor.
            raise NotImplementedError(
                f"orchestration node {node.id!r} (type {node.type!r}) is not implemented in M5"
            )


def _walk_boundary(
    node: Node, ir: IRDocument, input_value: Any, values: dict[tuple[str, str], Any]
) -> None:
    if node.type == "input":
        # Bind the run's input to every out-port the input node declares (§5: in/out plumbing).
        for port in node.ports.out:
            values[(node.id, port.id)] = input_value
    elif node.type == "output":
        # The output node's in-port value is the run's output. M5's run output is the llm
        # node's text, which the control-plane also reassembles from the deltas — so the node
        # is plumbing here; the value is read for completeness/connectivity, nothing observable.
        _incoming_value(node, ir.edges, values)
    else:
        # human / subgraph — deferred (§7).
        raise NotImplementedError(
            f"boundary node {node.id!r} (type {node.type!r}) is not implemented in M5"
        )


async def _walk_activity(
    node: Node,
    ir: IRDocument,
    *,
    edges: list,
    ctx: WalkContext,
    values: dict[tuple[str, str], Any],
) -> AsyncIterator[Delta]:
    if node.type != "llm":
        # agent / tool / mcp_tool / rag / retriever / memory / code — deferred (§7).
        raise NotImplementedError(
            f"activity node {node.id!r} (type {node.type!r}) is not implemented in M5"
        )

    config = LlmConfig.model_validate(node.config)
    model_id, params = resolve_model(ir, config)
    input_value = _incoming_value(node, edges, values)
    # Naive full replay (M4 §4): prior thread turns verbatim, then this turn's rendered prompt.
    messages = [*ctx.prior_messages, *_render_messages(config, input_value)]

    # open_stream sends the request and validates status, so a pre-stream 503/404 raises here —
    # before any delta — letting the control-plane surface a clean status (mirrors /runs).
    upstream = await ctx.gateway.open_stream(
        model=model_id, messages=messages, params=params, extra_headers=ctx.extra_headers
    )
    output = ""
    async for chunk in upstream:
        delta = chunk.choices[0].delta if chunk.choices else None
        content = getattr(delta, "content", None) if delta else None
        if content:
            output += content
            yield Delta(node_id=node.id, content=content)

    # Make the node's result available to downstream data edges (the output node reads it).
    # Stored under every non-error out-port so an edge picks it by ``sourceHandle`` (e.g. "ok").
    for port in node.ports.out:
        if port.type != "error":
            values[(node.id, port.id)] = output
