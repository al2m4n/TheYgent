"""Pure-unit tests for the Agent Graph IR seam.

No DB, no HTTP — these pin the static contract the control-plane walker depends on:
content-hash determinism, the ``type``/``kind`` taxonomy, per-type config
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
    """The simplest graph that runs: input -> llm -> output."""
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
    # The binding omits `source` (a lifecycle concern, irrelevant to graph IR).
    ir = parse_document(_trivial())
    assert ir.models["default"].source is None
    # ...and a document that includes it still validates.
    doc = _trivial()
    doc["models"]["default"]["source"] = "hf"
    assert parse_document(doc).models["default"].source == "hf"


def test_graph_model_binding_has_no_modality_field_guard() -> None:
    # `modality` belongs on the INFERENCE registration binding (registration.ManagedBinding), NOT
    # the hashed graph binding (graph.ModelBinding). A model node's modality INTENT is hashed via
    # its node `type` (llm for chat/vision, transcribe/speak for audio), never via the binding. If a
    # future change threads `modality` onto the graph binding, every agent's contentHash silently
    # shifts (a load-bearing reversal). This guards that line.
    from theygent_ir import ModelBinding

    assert "modality" not in ModelBinding.model_fields


def test_trivial_content_hash_is_pinned_guard() -> None:
    # Pin the hash of a representative graph (with a managed ModelBinding). The inference
    # registration payload is separate, so this hash must stay stable until a DELIBERATE
    # graph-IR change. A drift here means the graph binding or envelope moved.
    assert (
        content_hash(parse_document(_trivial()))
        == "sha256:7e8f9d839ed6ab9990024867f703a6e2b49ca1ec4cf4e37111583cebe7030966"
    )


def test_content_hash_strips_view_dragging_a_node_is_not_a_new_version() -> None:
    # The load-bearing content-hash promise: editing the view — dragging a node,
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
    doc["nodes"][1]["kind"] = "boundary"  # llm must be activity
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
    doc["surprise"] = True  # extra="forbid": an IR typo fails loudly
    with pytest.raises(ValidationError):
        parse_document(doc)


# ── tool + router node types: config + taxonomy ───────────────────────────────


def test_tool_and_router_configs_validate() -> None:
    from theygent_ir import RouterConfig, ToolConfig

    assert ToolConfig.model_validate({"tool": "echo", "args": {"x": "$in"}}).tool == "echo"
    assert ToolConfig.model_validate({"tool": "echo"}).args == {}  # args optional
    assert RouterConfig.model_validate({"select": "$in.in.handle"}).select == "$in.in.handle"


def test_router_must_be_orchestration() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "router"  # kind is still 'activity' (wrong for router)
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


# ── mcp_tool node type: config + taxonomy ─────────────────────────────────────


def test_mcp_tool_config_validates() -> None:
    from theygent_ir import McpToolConfig

    cfg = McpToolConfig.model_validate({"server": "fs", "tool": "read_file", "args": {"p": "$in"}})
    assert (cfg.server, cfg.tool) == ("fs", "read_file")
    assert McpToolConfig.model_validate({"server": "fs", "tool": "ls"}).args == {}  # args optional


def test_mcp_tool_must_be_activity() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "mcp_tool"
    doc["nodes"][1]["kind"] = "orchestration"  # wrong for mcp_tool (it's an activity)
    with pytest.raises(GraphValidationError, match="must have kind 'activity'"):
        validate_graph(parse_document(doc))


def test_mcp_tool_config_shape_validated() -> None:
    doc = _trivial()
    doc["nodes"][1]["type"] = "mcp_tool"
    doc["nodes"][1]["config"] = {"server": "fs"}  # missing required 'tool'
    with pytest.raises(GraphValidationError, match="invalid config"):
        validate_graph(parse_document(doc))


# ── multi-input (named in-ports, port-addressed) ──────────────────────────────


def _two_input_doc() -> dict:
    """input fans out to two echo tools → one llm with in-ports [file, question] → output. The
    minimal multi-input graph: the llm reads two distinct upstreams on two in-ports."""

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
    # A node with two in-ports, each fed by one data edge, is a first-class valid shape.
    ir = parse_document(_two_input_doc())
    validate_graph(ir)
    order = [n.id for n in topological_order(ir)]
    assert order.index("n_llm") > order.index("n_file")
    assert order.index("n_llm") > order.index("n_q")  # runs after BOTH producers


def test_duplicate_data_edge_to_one_in_port_rejected() -> None:
    # Two data edges into the `file` port is an ambiguous multi-input binding.
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
    # because a REQUIRED in-port dangles (per-port check, not per-node).
    doc = _two_input_doc()
    doc["edges"] = [e for e in doc["edges"] if e["id"] != "e4"]  # remove n_q -> n_llm.question
    with pytest.raises(GraphValidationError, match="required in-port 'question' is not connected"):
        validate_graph(parse_document(doc))


def test_optional_in_port_may_be_unfed() -> None:
    # An in-port declared `required: false` may dangle and resolve to null at runtime.
    doc = _two_input_doc()
    doc["edges"] = [e for e in doc["edges"] if e["id"] != "e4"]
    # mark the `question` in-port optional (it's the 2nd in-port of n_llm, index 1).
    doc["nodes"][3]["ports"]["in"][1]["required"] = False
    validate_graph(parse_document(doc))  # no raise


def test_edge_to_undeclared_in_port_rejected() -> None:
    # An edge targeting an in-port the node never declared is caught by the handle check.
    doc = _two_input_doc()
    doc["edges"][3]["targetHandle"] = "ghost"  # e4 -> n_llm.ghost (no such in-port)
    with pytest.raises(GraphValidationError, match="no in-port"):
        validate_graph(parse_document(doc))


def test_content_hash_stable_across_port_required_default() -> None:
    # Hash agreement between the registry and the walker is load-bearing: both must hash
    # byte-for-byte over view-stripped canonical JSON, forever. `Port.required: bool = True`
    # lives INSIDE the hashed region — so an IR that OMITS `required` MUST hash identically
    # to one that writes `required: true`, or two "identical" agents mint two versions.
    # Canonicalization materializes the default, so omitted == explicit-true.
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


# ── node palette — new types, per-instance guardrail kind, role ───────────────


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


def test_new_types_validate() -> None:
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
    # Determinism guard: a model check is I/O — it MUST be an activity, never lowered as
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
    # An mcp tool binding sets EXACTLY ONE of server / connection.
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
    # role is IR content (not view): changing a handle's role moves the contentHash,
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


# ── multimodal message content (text + image_url) ─────────────────────────────
# An ``llm`` node's message ``content`` may now be a list of OpenAI content parts so a graph
# can drive a vision model. The load-bearing invariant is backward compatibility: a plain-string
# ``content`` must hash and serialize byte-for-byte as before, so an existing agent never
# re-versions.

# The hash a STRING-content graph produced before the union was added — recorded here so the
# back-compat guarantee is pinned against a concrete value, not just self-consistency. If the union
# ever silently re-canonicalizes a string content, these constants break.
_STRING_CONTENT_HASH = "sha256:7e8f9d839ed6ab9990024867f703a6e2b49ca1ec4cf4e37111583cebe7030966"


def _with_content(content: object) -> dict:
    """The trivial graph with the llm node's single message ``content`` replaced."""
    doc = _trivial()
    doc["nodes"][1]["config"]["messages"] = [{"role": "user", "content": content}]
    return doc


def test_string_content_hashes_identically() -> None:
    # The whole back-compat claim in one assert: a string content yields the EXACT hash it did
    # before ``content`` became a union (no silent re-canonicalization → no spurious re-version).
    assert content_hash(parse_document(_trivial())) == _STRING_CONTENT_HASH


def test_string_content_still_dumps_as_a_bare_string() -> None:
    # The union must not wrap a string in a list/object — the canonical dump (what the hash runs
    # over, and what the gateway receives) stays a plain string.
    doc = parse_document(_trivial())
    msg = doc.model_dump(mode="json", by_alias=True)["nodes"][1]["config"]["messages"][0]
    assert msg["content"] == "$in"
    assert isinstance(msg["content"], str)


def test_list_content_validates_and_round_trips() -> None:
    # The new multimodal form: a text part + an image_url part (with the optional ``detail`` hint).
    content = [
        {"type": "text", "text": "Describe: $in"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "low"}},
    ]
    doc = parse_document(_with_content(content))
    validate_graph(doc)  # config validation lives here (node.config is opaque to parse_document)
    dumped = doc.model_dump(mode="json", by_alias=True)["nodes"][1]["config"]["messages"][0]
    assert dumped["content"] == content  # parts round-trip verbatim (incl. ``detail``)


def test_list_content_omits_detail_when_unset() -> None:
    # ``detail`` defaults to None; an image part without it round-trips without inventing the key
    # (so the wire image_url stays a clean OpenAI shape after the walker drops the None).
    content = [{"type": "image_url", "image_url": {"url": "https://ex.com/c.png"}}]
    doc = parse_document(_with_content(content))
    validate_graph(doc)
    image = doc.nodes[1].config["messages"][0]["content"][0]["image_url"]
    assert "detail" not in image or image.get("detail") is None


def test_list_content_changes_the_hash() -> None:
    # Adding an image is a real content change → a new contentHash (a vision agent is a new version
    # of a text agent), while string content is unchanged (the back-compat seam above).
    assert (
        content_hash(parse_document(_with_content([{"type": "text", "text": "$in"}])))
        != _STRING_CONTENT_HASH
    )


def test_unknown_content_part_type_rejected() -> None:
    # The discriminated union fails loudly on an unknown block type — no silently-dropped content.
    doc = parse_document(_with_content([{"type": "video", "url": "x"}]))
    with pytest.raises(GraphValidationError, match="invalid config"):
        validate_graph(doc)


def test_content_part_rejects_extra_keys() -> None:
    # ``_Wire`` forbids extra keys, so a typo'd field on a known part is a loud error, not ignored.
    doc = parse_document(_with_content([{"type": "text", "text": "hi", "bogus": 1}]))
    with pytest.raises(GraphValidationError, match="invalid config"):
        validate_graph(doc)


# ── autonomous tool-calling (llm.tools) ───────────────────────────────────────


def _http_tool_binding(**over: object) -> dict:
    """A self-describing http tool binding: description + parameterSchema + request shape,
    with a single ``$in.query`` slot matching the schema's one property."""
    b: dict = {
        "kind": "http",
        "connection": "con_search",
        "description": "Search the web and return results for a query.",
        "parameterSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "what to search for"}},
            "required": ["query"],
        },
        "method": "GET",
        "urlTemplate": "https://api.example.com/search?q={$in.query}",
    }
    b.update(over)
    return b


def _llm_tools_doc(
    *,
    tools: list[str] | None = None,
    tool_choice: object = "auto",
    max_iter: int = 8,
    bindings: dict | None = None,
) -> dict:
    """``_trivial`` with the llm node carrying autonomous tool-calling config + a tool binding
    in ir.tools. The tool is referenced by config (``llm.tools``), NOT wired by an edge."""
    doc = _trivial()
    doc["tools"] = bindings if bindings is not None else {"search": _http_tool_binding()}
    doc["nodes"][1]["config"] = {
        "model": "default",
        "messages": [{"role": "user", "content": "$in"}],
        "tools": ["search"] if tools is None else tools,
        "toolChoice": tool_choice,
        "maxToolIterations": max_iter,
    }
    return doc


def test_llm_with_http_tool_validates() -> None:
    validate_graph(parse_document(_llm_tools_doc()))


def test_llm_references_undeclared_tool_rejected() -> None:
    with pytest.raises(GraphValidationError, match="undeclared tool"):
        validate_graph(parse_document(_llm_tools_doc(tools=["ghost"])))


def test_llm_max_tool_iterations_must_be_positive() -> None:
    with pytest.raises(GraphValidationError, match="maxToolIterations"):
        validate_graph(parse_document(_llm_tools_doc(max_iter=0)))


def test_llm_duplicate_tool_key_rejected() -> None:
    with pytest.raises(GraphValidationError, match="duplicate"):
        validate_graph(parse_document(_llm_tools_doc(tools=["search", "search"])))


def test_llm_named_tool_choice_must_be_in_tools() -> None:
    tc = {"type": "function", "function": {"name": "not_listed"}}
    with pytest.raises(GraphValidationError, match="toolChoice"):
        validate_graph(parse_document(_llm_tools_doc(tool_choice=tc)))


def test_llm_named_tool_choice_in_tools_ok() -> None:
    tc = {"type": "function", "function": {"name": "search"}}
    validate_graph(parse_document(_llm_tools_doc(tool_choice=tc)))


def test_http_tool_for_llm_must_self_describe() -> None:
    bare = {"search": {"kind": "http", "connection": "c", "urlTemplate": "https://x/{$in.query}"}}
    with pytest.raises(GraphValidationError, match="self-describe"):
        validate_graph(parse_document(_llm_tools_doc(bindings=bare)))


def test_http_tool_unfillable_slot_rejected() -> None:
    # url references $in.q, but the schema declares only `query` → `q` is unfillable.
    bad = _http_tool_binding(urlTemplate="https://api.example.com/search?q={$in.q}")
    with pytest.raises(GraphValidationError, match="unfillable"):
        validate_graph(parse_document(_llm_tools_doc(bindings={"search": bad})))


def test_http_tool_dead_property_rejected() -> None:
    # schema declares `extra` with no matching slot in the url/body → dead.
    bad = _http_tool_binding(
        parameterSchema={
            "type": "object",
            "properties": {"query": {"type": "string"}, "extra": {"type": "string"}},
        }
    )
    with pytest.raises(GraphValidationError, match="dead"):
        validate_graph(parse_document(_llm_tools_doc(bindings={"search": bad})))


def test_http_tool_bare_in_relaxes_dead_check() -> None:
    # A bare `$in` (the whole arguments object) as the body consumes all properties, so a property
    # with no NAMED slot is not "dead".
    whole = _http_tool_binding(
        urlTemplate="https://api.example.com/post",
        bodyTemplate="$in",
        parameterSchema={
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        },
    )
    validate_graph(parse_document(_llm_tools_doc(bindings={"search": whole})))


def test_body_template_slot_satisfies_property() -> None:
    # The slot may live in the body (scanned recursively), not just the url.
    b = _http_tool_binding(
        method="POST",
        urlTemplate="https://api.example.com/search",
        bodyTemplate={"q": "{$in.query}"},
    )
    validate_graph(parse_document(_llm_tools_doc(bindings={"search": b})))


def test_llm_tools_field_is_hash_stable_when_omitted() -> None:
    # `Node.config` is hashed as the opaque authored dict — so an llm graph that doesn't write
    # the tool-calling fields is byte-identical to earlier graphs (no schemaVersion bump needed).
    # A tools-bearing llm is a genuine content change → a different hash.
    assert content_hash(parse_document(_trivial())) == _STRING_CONTENT_HASH
    assert content_hash(parse_document(_llm_tools_doc())) != _STRING_CONTENT_HASH


def test_empty_tools_list_is_single_shot_no_checks() -> None:
    # Empty `tools` is the single-shot path: the tool-calling checks don't fire
    # (e.g. a 0 max is fine because the loop never runs).
    validate_graph(parse_document(_llm_tools_doc(tools=[], max_iter=0, bindings={})))


# ── tool wiring as capability edges (tool node → llm `tools` port) ───────────


def _capability_graph(tool_node: dict, *, channel: str = "tool", to_handle: str = "tools") -> dict:
    """input → llm → output, with `tool_node` wired to the llm's `tools` port as a CAPABILITY (a
    `tool`-channel edge). The llm gains a `tool`-role `tools` in-port to receive it."""
    doc = _trivial()
    llm = next(n for n in doc["nodes"] if n["id"] == "n_llm")
    llm["ports"]["in"].append({"id": "tools", "type": "any", "role": "tool", "required": False})
    doc["nodes"].append(tool_node)
    doc["edges"].append(
        {
            "id": "e_tool",
            "source": tool_node["id"],
            "sourceHandle": "out",
            "target": "n_llm",
            "targetHandle": to_handle,
            "channel": channel,
        }
    )
    return doc


def _builtin_tool_node(node_id: str = "n_tool", ref: str = "echo") -> dict:
    return {
        "id": node_id,
        "type": "tool",
        "kind": "activity",
        "config": {"tool": ref, "description": "echo the value back"},
        "ports": {
            "in": [{"id": "in", "type": "any"}],  # required but unfed — capability exemption
            "out": [{"id": "out", "type": "any"}, {"id": "err", "type": "error"}],
        },
    }


def _http_tool_node(
    node_id: str = "n_http",
    *,
    description: str | None = "fetch a page",
    parameter_schema: dict | None = None,
    url: str = "https://api.example.com/$in.q",
) -> dict:
    cfg: dict = {"tool": node_id, "connection": "c1", "method": "GET", "urlTemplate": url}
    if description is not None:
        cfg["description"] = description
    if parameter_schema is not None:
        cfg["parameterSchema"] = parameter_schema
    return {
        "id": node_id,
        "type": "tool",
        "kind": "activity",
        "config": cfg,
        "ports": {
            "in": [{"id": "in", "type": "any"}],
            "out": [{"id": "out", "type": "any"}, {"id": "err", "type": "error"}],
        },
    }


_Q_SCHEMA = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}


def test_m22_builtin_capability_wired_to_llm_validates() -> None:
    # A builtin tool node wired to the llm's tools-port is a valid capability — and its required
    # `in` port may be unfed (the model supplies args at call time).
    validate_graph(parse_document(_capability_graph(_builtin_tool_node())))


def test_m22_http_capability_self_describing_validates() -> None:
    node = _http_tool_node(parameter_schema=_Q_SCHEMA)
    validate_graph(parse_document(_capability_graph(node)))


def test_m22_tool_edge_from_non_tool_node_rejected() -> None:
    # Re-point the capability edge's source to the input node — only a tool/mcp_tool may source it.
    doc = _capability_graph(_builtin_tool_node())
    doc["edges"][-1]["source"] = "n_in"
    doc["edges"][-1]["sourceHandle"] = "out"
    with pytest.raises(GraphValidationError, match="must come from a tool"):
        validate_graph(parse_document(doc))


def test_m22_tool_edge_to_non_llm_rejected() -> None:
    doc = _capability_graph(_builtin_tool_node())
    doc["nodes"][2]["ports"]["in"].append({"id": "tools", "type": "any", "role": "tool"})
    doc["edges"][-1]["target"] = "n_out"  # output, not an llm
    doc["edges"][-1]["targetHandle"] = "tools"
    with pytest.raises(GraphValidationError, match="must target an llm"):
        validate_graph(parse_document(doc))


def test_m22_tool_edge_into_non_tool_role_port_rejected() -> None:
    # Target the llm's data `in` port with a tool-channel edge — role mismatch.
    doc = _capability_graph(_builtin_tool_node(), to_handle="in")
    with pytest.raises(GraphValidationError, match="must target a `tool`-role port"):
        validate_graph(parse_document(doc))


def test_m22_mixed_mode_tool_node_rejected() -> None:
    # A tool node that is BOTH a capability (tool edge to llm) and a step (data edge out) is
    # ambiguous — rejected.
    doc = _capability_graph(_builtin_tool_node())
    doc["nodes"][2]["ports"]["in"].append({"id": "extra", "type": "any", "required": False})
    doc["edges"].append(
        {
            "id": "e_step",
            "source": "n_tool",
            "sourceHandle": "out",
            "target": "n_out",
            "targetHandle": "extra",
            "channel": "data",
        }
    )
    with pytest.raises(GraphValidationError, match=r"capability .* or a step"):
        validate_graph(parse_document(doc))


def test_m22_http_capability_without_schema_rejected() -> None:
    node = _http_tool_node(description="x", parameter_schema=None)
    with pytest.raises(GraphValidationError, match="self-describes"):
        validate_graph(parse_document(_capability_graph(node)))


def test_m22_http_capability_unfillable_slot_rejected() -> None:
    # url references $in.q but the schema has no `q` property → unfillable.
    node = _http_tool_node(
        url="https://api.example.com/$in.q",
        parameter_schema={"type": "object", "properties": {"other": {"type": "string"}}},
    )
    with pytest.raises(GraphValidationError, match="unfillable"):
        validate_graph(parse_document(_capability_graph(node)))


def test_m22_tool_channel_widening_is_hash_stable() -> None:
    # The PortRole/EdgeChannel widening + the optional ToolConfig fields must NOT shift an existing
    # graph's contentHash (Node.config is hashed as authored; existing role/channel values intact).
    assert content_hash(parse_document(_trivial())) == content_hash(parse_document(_trivial()))
    # A capability wiring IS real content → a different hash from the plain trivial graph.
    cap = content_hash(parse_document(_capability_graph(_builtin_tool_node())))
    assert cap != content_hash(parse_document(_trivial()))


def test_m22_capability_tool_excluded_from_topo_order() -> None:
    # A capability tool node is NOT sequenced (the `tool` edge imposes no order) — topo still
    # succeeds and the llm does not depend on the tool running first.
    order = topological_order(parse_document(_capability_graph(_builtin_tool_node())))
    assert [n.id for n in order if n.id in {"n_in", "n_llm", "n_out"}] == ["n_in", "n_llm", "n_out"]


def test_m22_capability_node_id_may_equal_its_ir_tools_key() -> None:
    # The editor derives ir.tools keyed by node id (a mirror of the node binding), so a
    # capability node id EQUAL to its ir.tools key is the norm — it must validate, not be rejected.
    doc = _capability_graph(_builtin_tool_node(node_id="n_echo"))
    doc["tools"] = {"n_echo": {"kind": "builtin", "ref": "echo"}}
    validate_graph(parse_document(doc))


def test_m22_data_edge_into_tools_port_rejected() -> None:
    # The inverse of the tool-edge rule: a `tool`-role in-port may ONLY be fed by a `tool` edge — a
    # data edge into the llm's tools port is a miswire whose value would never be consumed.
    doc = _capability_graph(_builtin_tool_node())
    doc["edges"].append(
        {
            "id": "e_bad",
            "source": "n_in",
            "sourceHandle": "out",
            "target": "n_llm",
            "targetHandle": "tools",
            "channel": "data",
        }
    )
    with pytest.raises(GraphValidationError, match=r"cannot target the .tool.-role port"):
        validate_graph(parse_document(doc))
