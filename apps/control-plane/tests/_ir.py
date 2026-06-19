"""The trivial 3-node IR document (input -> llm -> output) the graph fast suite drives.

This is the m5.md §4 envelope, inline. Helpers return a fresh deep copy each call so a test
can mutate one (a bad enum, a cycle, an engine-name binding) without disturbing another.
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
            "config": {"model": "default", "messages": [{"role": "user", "content": "$input"}]},
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


# ── M6 graph builders (tool + router) ────────────────────────────────────────
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
    return {
        "schemaVersion": "1.0",
        "id": "agt_m6_test",
        "name": "m6-test",
        "version": "0.1.0",
        "models": models or {},
        "tools": {},
        "nodes": nodes,
        "edges": edges,
    }


def tool_echo_ir() -> dict[str, Any]:
    """input → tool(echo, {value: $in}) → output. Proves dispatch + arg templating (m6.md §5)."""
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
    contract (m6.md §4): a result binds ok; a raised exception (transport failure, an
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


def router_ir(decision: Any) -> dict[str, Any]:
    """input → tool(echo, {value: <decision>}) → router(select $in.handle) → {yes: out_yes,
    no: out_no}. echo emits the literal decision object (network-free); the router branches on
    its ``handle`` (m6.md §5). ``decision`` is e.g. {"handle": "yes", "payload": 42}."""
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
                config={"select": "$in.handle"},
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


def agent_ir() -> dict[str, Any]:
    """The motivating agent shape (m6.md §6): input → llm → router → {yes: tool→out, no: out}.
    The llm emits a routing decision (deterministic via the fake's ``response``); the router
    branches on it; on "yes" a tool runs against the payload."""
    return _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_llm",
                "llm",
                "activity",
                config={"model": "default", "messages": [{"role": "user", "content": "$input"}]},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_route",
                "router",
                "orchestration",
                config={"select": "$in.handle"},
                ins=["in"],
                outs=["yes", "no"],
            ),
            _node(
                "n_tool",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "$in.payload"}},
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
