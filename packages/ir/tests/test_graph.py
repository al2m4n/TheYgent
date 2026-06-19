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
    assert RouterConfig.model_validate({"select": "$in.handle"}).select == "$in.handle"


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
            "config": {"select": "$in.handle"},
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
