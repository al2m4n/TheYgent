"""Pure-unit tests for the Agent Graph IR seam (theygent-graph-schema.md §8).

No DB, no HTTP — these pin the static contract the control-plane walker depends on:
content-hash determinism (§8.2), the ``type``/``kind`` taxonomy (§8.1), per-type config
validation, edge/port integrity, and cycle rejection. The end-to-end exercise through the
``/graphs/runs`` endpoint lives in the control-plane fast suite.
"""

from __future__ import annotations

import copy

import pytest
from theygent_ir import (
    GraphValidationError,
    content_hash,
    parse_document,
    topological_order,
    validate_graph,
)


def _trivial() -> dict:
    """The simplest graph that runs: input -> llm -> output (m5.md §4)."""
    return {
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


def test_trivial_validates_and_orders() -> None:
    ir = parse_document(_trivial())
    validate_graph(ir)
    order = [n.id for n in topological_order(ir)]
    assert order == ["n_in", "n_llm", "n_out"]


def test_source_is_optional() -> None:
    # The m5.md §4 binding omits `source` (a lifecycle concern, irrelevant to M5 — §8.4).
    ir = parse_document(_trivial())
    assert ir.models["default"].source is None
    # ...and a §8.4 document that includes it still validates.
    doc = _trivial()
    doc["models"]["default"]["source"] = "hf"
    assert parse_document(doc).models["default"].source == "hf"


def test_content_hash_strips_view_dragging_a_node_is_not_a_new_version() -> None:
    # THE load-bearing §8.2 promise (§8.0 rule 2): editing the view — dragging a node,
    # changing zoom/collapsed state — must NEVER change the hash, or every drag would mint a
    # "new version". Proven the way the promise is worded: the SAME logical graph with two
    # DIFFERENT view blocks (and one with none) all hash identically.
    base = _trivial()
    h = content_hash(parse_document(base))

    no_view = copy.deepcopy(base)  # view absent
    dragged_a = copy.deepcopy(base)
    dragged_a["view"] = {
        "nodes": {"n_llm": {"position": {"x": 240, "y": 120}, "collapsed": False}},
        "viewport": {"x": 0, "y": 0, "zoom": 1},
    }
    dragged_b = copy.deepcopy(base)  # same graph, node dragged + zoomed — a different view
    dragged_b["view"] = {
        "nodes": {"n_llm": {"position": {"x": 999, "y": 17}, "collapsed": True}},
        "viewport": {"x": -50, "y": 80, "zoom": 2.5},
    }

    ha = content_hash(parse_document(dragged_a))
    hb = content_hash(parse_document(dragged_b))
    assert ha == hb == content_hash(parse_document(no_view)) == h


def test_content_hash_ignores_key_order_whitespace_and_self_hash() -> None:
    base = _trivial()
    h = content_hash(parse_document(base))

    # Key order / whitespace are normalized (validated model -> canonical dump).
    reordered = {k: base[k] for k in reversed(list(base.keys()))}
    assert content_hash(parse_document(reordered)) == h

    # A pre-set contentHash on the input is excluded (it is derived, not hashed).
    pre = copy.deepcopy(base)
    pre["contentHash"] = "sha256:deadbeef"
    assert content_hash(parse_document(pre)) == h


def test_content_hash_changes_on_real_content_change() -> None:
    base = _trivial()
    h = content_hash(parse_document(base))
    changed = copy.deepcopy(base)
    changed["nodes"][1]["config"]["messages"][0]["content"] = "different prompt"
    assert content_hash(parse_document(changed)) != h


def test_hash_prefix() -> None:
    assert content_hash(parse_document(_trivial())).startswith("sha256:")


def test_wrong_kind_for_type_rejected() -> None:
    doc = _trivial()
    doc["nodes"][1]["kind"] = "boundary"  # llm must be activity (§8.1)
    with pytest.raises(GraphValidationError, match="must have kind 'activity'"):
        validate_graph(parse_document(doc))


def test_unknown_type_rejected() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "wizard"
    with pytest.raises(GraphValidationError, match="unknown node type"):
        validate_graph(parse_document(doc))


def test_llm_referencing_undeclared_model_rejected() -> None:
    doc = _trivial()
    doc["nodes"][1]["config"]["model"] = "nope"
    with pytest.raises(GraphValidationError, match="undeclared model"):
        validate_graph(parse_document(doc))


def test_edge_to_unknown_handle_rejected() -> None:
    doc = _trivial()
    doc["edges"][1]["sourceHandle"] = "ghost"
    with pytest.raises(GraphValidationError, match="no out-port"):
        validate_graph(parse_document(doc))


def test_disconnected_required_port_rejected() -> None:
    doc = _trivial()
    doc["edges"] = [doc["edges"][0]]  # drop llm -> output; output's in-port now dangles
    with pytest.raises(GraphValidationError, match="not connected"):
        validate_graph(parse_document(doc))


def test_cycle_rejected() -> None:
    doc = _trivial()
    # Add llm.ok -> input.in via a data edge, after giving input an in-port: a cycle.
    doc["nodes"][0]["ports"]["in"] = [{"id": "loop", "type": "any"}]
    doc["edges"].append(
        {
            "id": "e3",
            "source": "n_llm",
            "sourceHandle": "ok",
            "target": "n_in",
            "targetHandle": "loop",
            "channel": "data",
        }
    )
    with pytest.raises(GraphValidationError, match="cycle"):
        validate_graph(parse_document(doc))


def test_unknown_envelope_key_rejected() -> None:
    from pydantic import ValidationError

    doc = _trivial()
    doc["surprise"] = True  # extra="forbid": an IR typo fails loudly (§3.1)
    with pytest.raises(ValidationError):
        parse_document(doc)


# ── M6 node types: tool + router config + taxonomy ────────────────────────────


def test_tool_and_router_configs_validate() -> None:
    from theygent_ir import RouterConfig, ToolConfig

    assert ToolConfig.model_validate({"tool": "echo", "args": {"x": "$in"}}).tool == "echo"
    assert ToolConfig.model_validate({"tool": "echo"}).args == {}  # args optional
    assert RouterConfig.model_validate({"select": "$in.in.handle"}).select == "$in.in.handle"


def test_router_must_be_orchestration() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "router"  # kind is still 'activity' (wrong for router — §8.1)
    with pytest.raises(GraphValidationError, match="must have kind 'orchestration'"):
        validate_graph(parse_document(doc))


def test_tool_must_be_activity() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "tool"
    doc["nodes"][1]["kind"] = "boundary"  # wrong for tool
    with pytest.raises(GraphValidationError, match="must have kind 'activity'"):
        validate_graph(parse_document(doc))


def test_tool_config_shape_validated() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "tool"  # kind 'activity' is correct for tool
    doc["nodes"][1]["config"] = {"args": {}}  # missing required 'tool'
    with pytest.raises(GraphValidationError, match="invalid config"):
        validate_graph(parse_document(doc))


def test_content_hash_changes_when_node_added() -> None:
    base = _trivial()
    h = content_hash(parse_document(base))
    doc = _trivial()
    doc["nodes"].append(
        {
            "id": "n_route",
            "type": "router",
            "kind": "orchestration",
            "config": {"select": "$in.in.handle"},
            "ports": {"in": [{"id": "in", "type": "any"}], "out": [{"id": "yes", "type": "any"}]},
        }
    )
    assert content_hash(parse_document(doc)) != h


def test_content_hash_stable_under_config_key_reorder() -> None:
    base = _trivial()
    h = content_hash(parse_document(base))
    doc = _trivial()
    cfg = doc["nodes"][1]["config"]
    doc["nodes"][1]["config"] = {"messages": cfg["messages"], "model": cfg["model"]}  # reordered
    assert content_hash(parse_document(doc)) == h


# ── M7 node type: mcp_tool config + taxonomy ──────────────────────────────────


def test_mcp_tool_config_validates() -> None:
    from theygent_ir import McpToolConfig

    cfg = McpToolConfig.model_validate({"server": "fs", "tool": "read_file", "args": {"p": "$in"}})
    assert (cfg.server, cfg.tool) == ("fs", "read_file")
    assert McpToolConfig.model_validate({"server": "fs", "tool": "ls"}).args == {}  # args optional


def test_mcp_tool_must_be_activity() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "mcp_tool"
    doc["nodes"][1]["kind"] = "orchestration"  # wrong for mcp_tool (it's an activity — §8.1)
    with pytest.raises(GraphValidationError, match="must have kind 'activity'"):
        validate_graph(parse_document(doc))


def test_mcp_tool_config_shape_validated() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "mcp_tool"
    doc["nodes"][1]["config"] = {"server": "fs"}  # missing required 'tool'
    with pytest.raises(GraphValidationError, match="invalid config"):
        validate_graph(parse_document(doc))


# ── M10: multi-input (named in-ports, port-addressed) ─────────────────────────


def _two_input_doc() -> dict:
    """input fans out to two echo tools → one llm with in-ports [file, question] → output. The
    minimal multi-input graph (m10.md §3): the llm reads two distinct upstreams on two in-ports."""

    def ports(ins: list[str], outs: list[str]) -> dict:
        return {
            "in": [{"id": i, "type": "any"} for i in ins],
            "out": [{"id": o, "type": "error" if o == "err" else "any"} for o in outs],
        }

    def edge(eid: str, src: str, sh: str, tgt: str, th: str) -> dict:
        return {
            "id": eid,
            "source": src,
            "sourceHandle": sh,
            "target": tgt,
            "targetHandle": th,
            "channel": "data",
        }

    return {
        "schemaVersion": "1.0",
        "id": "agt_mi",
        "name": "multi-input",
        "version": "0.1.0",
        "models": {"default": {"binding": "mlx", "model": "triage-fast", "params": {}}},
        "tools": {},
        "nodes": [
            {"id": "n_in", "type": "input", "kind": "boundary", "ports": ports([], ["out"])},
            {
                "id": "n_file",
                "type": "tool",
                "kind": "activity",
                "config": {"tool": "echo", "args": {"value": "f"}},
                "ports": ports(["in"], ["ok", "err"]),
            },
            {
                "id": "n_q",
                "type": "tool",
                "kind": "activity",
                "config": {"tool": "echo", "args": {"value": "q"}},
                "ports": ports(["in"], ["ok", "err"]),
            },
            {
                "id": "n_llm",
                "type": "llm",
                "kind": "activity",
                "config": {
                    "model": "default",
                    "messages": [{"role": "user", "content": "$in.file $in.question"}],
                },
                "ports": ports(["file", "question"], ["ok", "err"]),
            },
            {"id": "n_out", "type": "output", "kind": "boundary", "ports": ports(["in"], [])},
        ],
        "edges": [
            edge("e1", "n_in", "out", "n_file", "in"),
            edge("e2", "n_in", "out", "n_q", "in"),
            edge("e3", "n_file", "ok", "n_llm", "file"),
            edge("e4", "n_q", "ok", "n_llm", "question"),
            edge("e5", "n_llm", "ok", "n_out", "in"),
        ],
    }


def test_two_input_node_validates_and_orders() -> None:
    # A node with two in-ports, each fed by one data edge, is a first-class valid shape (m10.md §2).
    ir = parse_document(_two_input_doc())
    validate_graph(ir)
    order = [n.id for n in topological_order(ir)]
    assert order.index("n_llm") > order.index("n_file")
    assert order.index("n_llm") > order.index("n_q")  # runs after BOTH producers


def test_duplicate_data_edge_to_one_in_port_rejected() -> None:
    # Two data edges into the `file` port is an ambiguous multi-input binding (m10.md §3).
    doc = _two_input_doc()
    doc["edges"].append(
        {
            "id": "e6",
            "source": "n_q",
            "sourceHandle": "ok",
            "target": "n_llm",
            "targetHandle": "file",
            "channel": "data",
        }
    )
    with pytest.raises(GraphValidationError, match="ambiguous multi-input binding"):
        validate_graph(parse_document(doc))


def test_unfed_required_in_port_rejected_per_port() -> None:
    # Per-port required check: drop the `question` feed; `file` is still fed but the node fails
    # because a REQUIRED in-port dangles (m10.md §3 — per-port, not per-node).
    doc = _two_input_doc()
    doc["edges"] = [e for e in doc["edges"] if e["id"] != "e4"]  # remove n_q -> n_llm.question
    with pytest.raises(GraphValidationError, match="required in-port 'question' is not connected"):
        validate_graph(parse_document(doc))


def test_optional_in_port_may_be_unfed() -> None:
    # An in-port declared `required: false` may dangle and resolve to null at runtime (m10.md §3).
    doc = _two_input_doc()
    doc["edges"] = [e for e in doc["edges"] if e["id"] != "e4"]
    # mark the `question` in-port optional (it's the 2nd in-port of n_llm, index 1).
    doc["nodes"][3]["ports"]["in"][1]["required"] = False
    validate_graph(parse_document(doc))  # no raise


def test_edge_to_undeclared_in_port_rejected() -> None:
    # An edge targeting an in-port the node never declared is caught by the handle check (§3).
    doc = _two_input_doc()
    doc["edges"][3]["targetHandle"] = "ghost"  # e4 -> n_llm.ghost (no such in-port)
    with pytest.raises(GraphValidationError, match="no in-port"):
        validate_graph(parse_document(doc))


def test_content_hash_stable_across_port_required_default() -> None:
    # The M10→M11 hinge (m11.md §1.1 makes hash agreement load-bearing): the registry must hash
    # byte-for-byte like the walker, over view-stripped canonical JSON, forever. M10 introduced
    # `Port.required: bool = True` INSIDE the hashed region — so an IR that OMITS `required` MUST
    # hash identically to one that writes `required: true`, or two "identical" agents mint two
    # versions. Canonicalization materializes the default, so omitted == explicit-true.
    base = _trivial()
    h = content_hash(parse_document(base))
    explicit = copy.deepcopy(base)
    for node in explicit["nodes"]:
        for port in node["ports"]["in"] + node["ports"]["out"]:
            port["required"] = True  # write the default explicitly on every port
    assert content_hash(parse_document(explicit)) == h

    # ...while an explicit `required: false` IS a real content change and MUST move the hash.
    optional = copy.deepcopy(base)
    optional["nodes"][2]["ports"]["in"][0]["required"] = False  # output's in-port made optional
    assert content_hash(parse_document(optional)) != h


# ── M19 the node palette (m19.md §2) — new types, per-instance guardrail kind, role ──────────────


def _graph_with(node: dict, *, src_handle: str, in_handle: str = "in") -> dict:
    """Build ``input -> NODE -> output`` reusing ``_trivial`` wiring: replace the middle node (keeps
    id ``n_llm`` so the edges stand), re-point the in-edge's targetHandle and the out-edge's
    sourceHandle to the node's actual ports."""
    doc = _trivial()
    node = {**node, "id": "n_llm"}
    doc["nodes"][1] = node
    doc["edges"][0]["targetHandle"] = in_handle
    doc["edges"][1]["sourceHandle"] = src_handle
    return doc


def test_m19_new_types_validate() -> None:
    # transcribe / speak reference the declared ``default`` model; both are activities.
    for node, src, in_h in (
        (
            {
                "type": "transcribe",
                "kind": "activity",
                "config": {"model": "default", "params": {}},
                "ports": {
                    "in": [{"id": "audio", "type": "any"}],
                    "out": [{"id": "text", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            "text",
            "audio",
        ),
        (
            {
                "type": "speak",
                "kind": "activity",
                "config": {"model": "default", "params": {"voice": "alloy"}},
                "ports": {
                    "in": [{"id": "text", "type": "any"}],
                    "out": [{"id": "audio", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            "audio",
            "text",
        ),
        (
            {
                "type": "transform",
                "kind": "orchestration",
                "config": {"expr": "{ city: $in.in.city }"},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "out", "type": "any"}],
                },
            },
            "out",
            "in",
        ),
        (
            {
                "type": "ratelimit",
                "kind": "activity",
                "config": {"keyExpr": "$caller.token", "limit": 60, "windowSeconds": 3600},
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "allow", "type": "any"}, {"id": "deny", "type": "any"}],
                },
            },
            "allow",
            "in",
        ),
        (
            {
                "type": "quota",
                "kind": "activity",
                "config": {
                    "keyExpr": "$caller.token",
                    "budgetTokens": 1_000_000,
                    "windowSeconds": 2_592_000,
                },
                "ports": {
                    "in": [{"id": "in", "type": "any"}],
                    "out": [{"id": "allow", "type": "any"}, {"id": "deny", "type": "any"}],
                },
            },
            "allow",
            "in",
        ),
    ):
        validate_graph(parse_document(_graph_with(node, src_handle=src, in_handle=in_h)))


def _guardrail_node(kind: str, check: dict) -> dict:
    return {
        "type": "guardrail",
        "kind": kind,
        "config": {"check": check, "onBlock": {"message": "nope"}},
        "ports": {
            "in": [{"id": "in", "type": "any"}],
            "out": [{"id": "pass", "type": "any"}, {"id": "block", "type": "any"}],
        },
    }


def test_guardrail_rule_is_orchestration() -> None:
    node = _guardrail_node(
        "orchestration", {"type": "rule", "rule": {"kind": "regex", "spec": {"pattern": "x"}}}
    )
    validate_graph(parse_document(_graph_with(node, src_handle="pass")))


def test_guardrail_model_is_activity() -> None:
    node = _guardrail_node(
        "activity",
        {"type": "model", "model": {"model": "default", "prompt": "in scope?", "passOn": "yes"}},
    )
    validate_graph(parse_document(_graph_with(node, src_handle="pass")))


def test_guardrail_model_as_orchestration_rejected() -> None:
    # The §1.2 determinism guard: a model check is I/O — it MUST be an activity, never lowered as
    # deterministic orchestration. The per-instance kind derivation rejects the mislabel at compile.
    node = _guardrail_node(
        "orchestration",
        {"type": "model", "model": {"model": "default", "prompt": "in scope?"}},
    )
    with pytest.raises(GraphValidationError, match="must have kind 'activity'"):
        validate_graph(parse_document(_graph_with(node, src_handle="pass")))


def test_guardrail_rule_as_activity_rejected() -> None:
    node = _guardrail_node("activity", {"type": "rule", "rule": {"kind": "length", "spec": {}}})
    with pytest.raises(GraphValidationError, match="must have kind 'orchestration'"):
        validate_graph(parse_document(_graph_with(node, src_handle="pass")))


def test_guardrail_rule_needs_rule_subconfig() -> None:
    node = _guardrail_node("orchestration", {"type": "rule"})  # no `rule`
    with pytest.raises(GraphValidationError, match=r"needs check\.rule"):
        validate_graph(parse_document(_graph_with(node, src_handle="pass")))


def test_guardrail_model_references_declared_model() -> None:
    node = _guardrail_node(
        "activity", {"type": "model", "model": {"model": "ghost", "prompt": "?"}}
    )
    with pytest.raises(GraphValidationError, match="undeclared model 'ghost'"):
        validate_graph(parse_document(_graph_with(node, src_handle="pass")))


def test_transcribe_undeclared_model_rejected() -> None:
    node = {
        "type": "transcribe",
        "kind": "activity",
        "config": {"model": "ghost", "params": {}},
        "ports": {
            "in": [{"id": "audio", "type": "any"}],
            "out": [{"id": "text", "type": "any"}, {"id": "err", "type": "error"}],
        },
    }
    with pytest.raises(GraphValidationError, match="undeclared model 'ghost'"):
        validate_graph(parse_document(_graph_with(node, src_handle="text", in_handle="audio")))


def test_http_tool_binding_and_node_validate() -> None:
    doc = _trivial()
    doc["tools"] = {"weather": {"kind": "http", "connection": "con_abc"}}
    doc["nodes"][1] = {
        "id": "n_llm",
        "type": "tool",
        "kind": "activity",
        "config": {
            "tool": "weather",
            "method": "GET",
            "urlTemplate": "https://api.example.com/f?city={$in.in.city}",
            "headers": {"X-Trace": "{$in.in.runId}"},
            "responseMap": "$.data",
        },
        "ports": {
            "in": [{"id": "in", "type": "any"}],
            "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
        },
    }
    doc["edges"][1]["sourceHandle"] = "ok"
    validate_graph(parse_document(doc))


def test_mcp_tool_binding_requires_exactly_one_target() -> None:
    # M19 §2.4: an mcp tool binding sets EXACTLY ONE of server / connection.
    both = {
        "schemaVersion": "1.0",
        "id": "a",
        "name": "n",
        "version": "0.1.0",
        "models": {},
        "tools": {"t": {"kind": "mcp", "tool": "x", "server": "s", "connection": "c"}},
        "nodes": [],
        "edges": [],
    }
    with pytest.raises(Exception, match="EXACTLY ONE"):
        parse_document(both)
    neither = copy.deepcopy(both)
    neither["tools"]["t"] = {"kind": "mcp", "tool": "x"}
    with pytest.raises(Exception, match="EXACTLY ONE"):
        parse_document(neither)
    # Either alone is fine.
    ok_conn = copy.deepcopy(both)
    ok_conn["tools"]["t"] = {"kind": "mcp", "tool": "x", "connection": "con_1"}
    parse_document(ok_conn)


def test_port_role_is_hashed_content() -> None:
    # role is IR content (not view): changing a handle's role moves the contentHash (M19 §2.10),
    # while the default ``data`` is materialized so omitting it hashes like writing it.
    base = _trivial()
    h = content_hash(parse_document(base))
    explicit = copy.deepcopy(base)
    for node in explicit["nodes"]:
        for port in node["ports"]["in"] + node["ports"]["out"]:
            port["role"] = "data"  # the default, written explicitly
    assert content_hash(parse_document(explicit)) == h
    control = copy.deepcopy(base)
    control["nodes"][1]["ports"]["in"][0]["role"] = "control"
    assert content_hash(parse_document(control)) != h
