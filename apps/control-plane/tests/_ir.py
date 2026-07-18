"""The trivial 3-node IR document (input -> llm -> output) the graph fast suite drives.

This is the trivial IR document envelope, inline. Helpers return a fresh deep copy each call so
a test can mutate one (a bad enum, a cycle, an engine-name binding) without disturbing another.
"""

from __future__ import annotations

import copy
from typing import Any

_TRIVIAL: dict[str, Any] = {
    "schemaVersion": "1.0",
    "id": "agt_01J9X8TRIVIAL",
    "name": "trivial-llm",
    "version": "0.1.0",
    "models": {
        "default": {
            "binding": "mlx",
            "model": "triage-fast",
            "params": {"temperature": 0.2, "maxTokens": 256},
        }
    },
    "tools": {},
    "nodes": [
        {
            "id": "n_in",
            "type": "input",
            "kind": "boundary",
            "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
        },
        {
            "id": "n_llm",
            "type": "llm",
            "kind": "activity",
            "config": {"model": "default", "messages": [{"role": "user", "content": "$in"}]},
            "ports": {
                "in": [{"id": "in", "type": "any"}],
                "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
            },
        },
        {
            "id": "n_out",
            "type": "output",
            "kind": "boundary",
            "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
        },
    ],
    "edges": [
        {
            "id": "e1",
            "source": "n_in",
            "sourceHandle": "out",
            "target": "n_llm",
            "targetHandle": "in",
            "channel": "data",
        },
        {
            "id": "e2",
            "source": "n_llm",
            "sourceHandle": "ok",
            "target": "n_out",
            "targetHandle": "in",
            "channel": "data",
        },
    ],
}


def trivial_ir() -> dict[str, Any]:
    return copy.deepcopy(_TRIVIAL)


# ── graph builders (tool + router) ───────────────────────────────────────────
# Compact constructors so each test reads as a graph shape, not a wall of JSON.

_DEFAULT_MODEL = {"default": {"binding": "mlx", "model": "triage-fast", "params": {}}}


def _ports(ins: list[str], outs: list[str]) -> dict[str, Any]:
    # An out-handle named "err" is an error-typed port (the tool/llm err branch); else "any".
    return {
        "in": [{"id": i, "type": "any"} for i in ins],
        "out": [{"id": o, "type": "error" if o == "err" else "any"} for o in outs],
    }


def _node(node_id: str, type_: str, kind: str, *, config: Any = None, ins=(), outs=()) -> dict:
    n: dict[str, Any] = {
        "id": node_id,
        "type": type_,
        "kind": kind,
        "ports": _ports(list(ins), list(outs)),
    }
    if config is not None:
        n["config"] = config
    return n


def _edge(eid: str, source: str, sh: str, target: str, th: str = "in") -> dict:
    return {
        "id": eid,
        "source": source,
        "sourceHandle": sh,
        "target": target,
        "targetHandle": th,
        "channel": "data",
    }


def _doc(nodes: list[dict], edges: list[dict], *, models: dict | None = None) -> dict[str, Any]:
    # Deep-copy ``models``: the constructors below pass module-level dicts (``_DEFAULT_MODEL`` /
    # ``_VISION_MODEL``) by reference, and tests mutate the returned IR in place (e.g. an
    # engine-name binding ``doc["models"]["default"]["model"] = "mlx"``). Without the copy that
    # mutation leaks into the module-level dict and poisons every later ``llm_ir``/``agent_ir``
    # call, so a valid graph is wrongly rejected with ``engine_name_not_allowed`` depending on test
    # order. Honors this module's contract: a fresh deep copy each call, without disturbing another.
    return {
        "schemaVersion": "1.0",
        "id": "agt_router_test",
        "name": "m6-test",
        "version": "0.1.0",
        "models": copy.deepcopy(models) if models else {},
        "tools": {},
        "nodes": nodes,
        "edges": edges,
    }


def tool_echo_ir() -> dict[str, Any]:
    """input → tool(echo, {value: $in}) → output. Proves dispatch + arg templating."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_tool",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "$in"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_tool"), _edge("e2", "n_tool", "ok", "n_out")],
    )


def tool_ok_err_ir(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """input → tool(<tool>, <args>) → {ok: n_ok, err: n_err}. The ok/err split is the tool
    contract: a result binds ok; a raised exception (transport failure, an
    unresolvable ``$in`` arg ref) binds err and the run continues."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_tool",
                "tool",
                "activity",
                config={"tool": tool, "args": args},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_ok", "output", "boundary", ins=["in"]),
            _node("n_err", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_tool"),
            _edge("e2", "n_tool", "ok", "n_ok"),
            _edge("e3", "n_tool", "err", "n_err"),
        ],
    )


def tool_http_ir(url: str) -> dict[str, Any]:
    """input → tool(http_fetch, {url}) → {ok: out_ok, err: out_err}."""
    return tool_ok_err_ir("http_fetch", {"url": url})


def mcp_tool_ir(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """input → mcp_tool(<server>, <tool>, <args>) → output. The single-ok-path MCP graph
    Same IR shape as a local tool, only the node type differs."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_mcp",
                "mcp_tool",
                "activity",
                config={"server": server, "tool": tool, "args": args},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_mcp"), _edge("e2", "n_mcp", "ok", "n_out")],
    )


def mcp_tool_ok_err_ir(server: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """input → mcp_tool(<server>, <tool>, <args>) → {ok: n_ok, err: n_err}. For asserting the
    err branch on a tool-level error / transport failure."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_mcp",
                "mcp_tool",
                "activity",
                config={"server": server, "tool": tool, "args": args},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_ok", "output", "boundary", ins=["in"]),
            _node("n_err", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_mcp"),
            _edge("e2", "n_mcp", "ok", "n_ok"),
            _edge("e3", "n_mcp", "err", "n_err"),
        ],
    )


def router_ir(decision: Any) -> dict[str, Any]:
    """input → tool(echo, {value: <decision>}) → router(select $in.in.handle) → {yes: out_yes,
    no: out_no}. echo emits the literal decision object (network-free); the router branches on
    its ``handle``. ``decision`` is e.g. {"handle": "yes", "payload": 42}. The select is
    port-addressed: ``$in.in.handle`` = default in-port ``in`` → drill ``handle``."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_decide",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": decision}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_route",
                "router",
                "orchestration",
                config={"select": "$in.in.handle"},
                ins=["in"],
                outs=["yes", "no"],
            ),
            _node("n_yes", "output", "boundary", ins=["in"]),
            _node("n_no", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_decide"),
            _edge("e2", "n_decide", "ok", "n_route"),
            _edge("e3", "n_route", "yes", "n_yes"),
            _edge("e4", "n_route", "no", "n_no"),
        ],
    )


def llm_ir(content: str) -> dict[str, Any]:
    """input → llm(<content>) → output. The llm's in-port carries the raw run input string, so
    ``content`` exercises the unified ``$in`` template against a string value."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_llm",
                "llm",
                "activity",
                config={"model": "default", "messages": [{"role": "user", "content": content}]},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_llm"), _edge("e2", "n_llm", "ok", "n_out")],
        models=_DEFAULT_MODEL,
    )


_VISION_MODEL = {"vision": {"binding": "mlx", "model": "qwen2-vl-vision", "params": {}}}


def vision_llm_ir(text: str, image_url: str, *, detail: str | None = None) -> dict[str, Any]:
    """input → llm(MULTIMODAL content: [text, image_url]) → output — a graph drives a vision model.
    The llm is bound to a vision logical model; ``text``
    and ``image_url`` each exercise the unified ``$in`` template INSIDE a content part, so an image
    enters from the run input (``image_url="$in"``) rather than being hardcoded — the thing a string
    ``content`` could never express. The image block must survive IR serialize + the real gateway
    hop unchanged (LiteLLM forwards ``image_url`` parts)."""
    image: dict[str, Any] = {"url": image_url}
    if detail is not None:
        image["detail"] = detail
    content = [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": image},
    ]
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_llm",
                "llm",
                "activity",
                config={"model": "vision", "messages": [{"role": "user", "content": content}]},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_llm"), _edge("e2", "n_llm", "ok", "n_out")],
        models=_VISION_MODEL,
    )


def llm_over_object_ir(content: str, value: Any) -> dict[str, Any]:
    """input → tool(echo, {value: <value>}) → llm(<content>) → output. echo emits the literal
    object ``value`` (network-free), so the llm's default in-port ``in`` is an OBJECT and
    ``content`` can drill into it with ``$in.in.field`` (port-addressed, field-aware)."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_obj",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": value}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_llm",
                "llm",
                "activity",
                config={"model": "default", "messages": [{"role": "user", "content": content}]},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_obj"),
            _edge("e2", "n_obj", "ok", "n_llm"),
            _edge("e3", "n_llm", "ok", "n_out"),
        ],
        models=_DEFAULT_MODEL,
    )


def two_input_llm_ir(content: str, file_value: Any, question_value: Any) -> dict[str, Any]:
    """input fans out to two echo tools emitting ``file_value`` and
    ``question_value`` (network-free); BOTH feed one ``llm`` node on DISTINCT in-ports ``file``
    and ``question``; ``content`` composes ``$in.file`` AND ``$in.question`` in one prompt — the
    thing a single-input graph could never express. Each echo's own in-port is fed by ``n_in``
    (so its required in-port is connected); its literal arg makes the two upstream values distinct
    and deterministic."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_file",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": file_value}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_q",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": question_value}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_llm",
                "llm",
                "activity",
                config={"model": "default", "messages": [{"role": "user", "content": content}]},
                ins=["file", "question"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_file"),
            _edge("e2", "n_in", "out", "n_q"),
            _edge("e3", "n_file", "ok", "n_llm", "file"),
            _edge("e4", "n_q", "ok", "n_llm", "question"),
            _edge("e5", "n_llm", "ok", "n_out"),
        ],
        models=_DEFAULT_MODEL,
    )


def output_multi_inport_ir() -> dict[str, Any]:
    """input fans out to two echo tools → an OUTPUT node declaring TWO in-ports [a, b]. A
    single-value consumer with >1 in-port is ambiguous — *which* value is the run output? — so the
    walker rejects it LOUDLY rather than silently picking one. Validation
    PASSES (each port fed exactly once); the ambiguity is a walk-time error, not a wiring error."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_a",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "A"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_b",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "B"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["a", "b"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_a"),
            _edge("e2", "n_in", "out", "n_b"),
            _edge("e3", "n_a", "ok", "n_out", "a"),
            _edge("e4", "n_b", "ok", "n_out", "b"),
        ],
    )


def router_multi_inport_ir(decision: Any) -> dict[str, Any]:
    """input fans out to two echo tools → a ROUTER declaring TWO in-ports [a, b]. A router forwards
    a single value along its chosen branch, so >1 in-port is ambiguous → loud walk-time error
    ``select`` reads ``$in.a.handle`` (which resolves fine over the port map), then
    the forward rejects the multi-port shape — proving the addressing/forward split."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_a",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": decision}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_b",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "x"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_route",
                "router",
                "orchestration",
                config={"select": "$in.a.handle"},
                ins=["a", "b"],
                outs=["yes", "no"],
            ),
            _node("n_yes", "output", "boundary", ins=["in"]),
            _node("n_no", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_a"),
            _edge("e2", "n_in", "out", "n_b"),
            _edge("e3", "n_a", "ok", "n_route", "a"),
            _edge("e4", "n_b", "ok", "n_route", "b"),
            _edge("e5", "n_route", "yes", "n_yes"),
            _edge("e6", "n_route", "no", "n_no"),
        ],
    )


def tool_unhandled_err_ir(tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """input → tool(<tool>) → output, with the tool's ``err`` handle wired NOWHERE. When the tool
    errors, its ``ok`` edge to ``output`` is dead and nothing reaches the output boundary — the
    The "empty output from an upstream error" case (no green run masquerading as success)."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_tool",
                "tool",
                "activity",
                config={"tool": tool, "args": args},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_tool"), _edge("e2", "n_tool", "ok", "n_out")],
    )


def agent_ir() -> dict[str, Any]:
    """The motivating agent shape: input → llm → router → {yes: tool→out, no: out}.
    The llm emits a routing decision (deterministic via the fake's ``response``); the router
    branches on it; on "yes" a tool runs against the payload."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_llm",
                "llm",
                "activity",
                config={"model": "default", "messages": [{"role": "user", "content": "$in"}]},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_route",
                "router",
                "orchestration",
                config={"select": "$in.in.handle"},
                ins=["in"],
                outs=["yes", "no"],
            ),
            _node(
                "n_tool",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "$in.in.payload"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_yes", "output", "boundary", ins=["in"]),
            _node("n_no", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_llm"),
            _edge("e2", "n_llm", "ok", "n_route"),
            _edge("e3", "n_route", "yes", "n_tool"),
            _edge("e4", "n_tool", "ok", "n_yes"),
            _edge("e5", "n_route", "no", "n_no"),
        ],
        models=_DEFAULT_MODEL,
    )
