"""M22 — visual tool wiring (the interactive walker).

A configured tool node wired to an llm's ``tools`` port via a ``channel:"tool"`` edge is a
CAPABILITY the model may call. The available-tool set is derived from those edges (the function name
= the tool NODE id), so the M21 autonomous loop runs without a graph-level ``config.tools`` entry.
The capability node is NOT executed as a standalone step. Driven by the fake's ``tool_call`` mode
(calls the named function on turn 1, answers once a tool result is in the transcript). Durable
parity lives in test_durable.py; the IR rules are pinned in packages/ir/tests/test_graph.py.
"""

from __future__ import annotations

import json

from _fake_inference import FakeInference
from _ir import llm_ir
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_control_plane.mcp import McpManager
from theygent_control_plane.tools import DEFAULT_REGISTRY
from theygent_control_plane.walker import (
    _capability_binding,
    build_capability_schemas,
    build_tool_schemas,
)
from theygent_ir import HttpTool, McpTool, parse_document


def _builtin_tool_node(node_id: str = "n_echo", ref: str = "echo") -> dict:
    return {
        "id": node_id,
        "type": "tool",
        "kind": "activity",
        "config": {"tool": ref, "description": "echo the value back"},
        "ports": {
            "in": [{"id": "in", "type": "any", "required": True}],
            "out": [{"id": "out", "type": "any"}, {"id": "err", "type": "error"}],
        },
    }


def _capability_graph(tool_node: dict, *, max_iter: int = 4) -> dict:
    """input → llm → output, with `tool_node` wired to the llm's `tools` port (a `channel:"tool"`
    capability edge). The llm declares a `tool`-role `tools` in-port to receive it; config.tools is
    EMPTY (the capability comes from the wire, not a graph-level key)."""
    ir = llm_ir("$in")
    llm = ir["nodes"][1]
    llm["ports"]["in"].append({"id": "tools", "type": "any", "required": False, "role": "tool"})
    llm["config"]["maxToolIterations"] = max_iter
    ir["nodes"].append(tool_node)
    ir["edges"].append(
        {
            "id": "e_tool",
            "source": tool_node["id"],
            "sourceHandle": "out",
            "target": "n_llm",
            "targetHandle": "tools",
            "channel": "tool",
        }
    )
    return ir


def _run(client: TestClient, ir: dict, *, input_: str = "do the thing") -> dict:
    return client.post("/graphs/runs", json={"ir": ir, "input": input_, "stream": False}).json()


def test_m22_wired_capability_tool_then_answers(pg_url: str) -> None:
    # The headline: a tool node wired to the llm (no config.tools) is callable — the model calls it
    # by NODE id, the walker resolves the binding from the node's config, runs it, feeds the result
    # back, and the model answers. All from the visual wire.
    with FakeInference(
        mode="tool_call",
        tool_name="n_echo",
        tool_args={"value": "hi"},
        response="answer after tool",
    ) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = _run(client, _capability_graph(_builtin_tool_node()))
    assert body["status"] == "completed"
    assert body["output"] == "answer after tool"
    msgs = server.captured["messages"]
    assert any(isinstance(m, dict) and m.get("role") == "tool" for m in msgs)
    assert any(
        isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls") for m in msgs
    )


def test_m22_capability_tool_call_streams_the_node_id(pg_url: str) -> None:
    with FakeInference(
        mode="tool_call", tool_name="n_echo", tool_args={"value": "hi"}, response="streamed answer"
    ) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            with client.stream(
                "POST",
                "/graphs/runs",
                json={"ir": _capability_graph(_builtin_tool_node()), "input": "go", "stream": True},
            ) as resp:
                text = "".join(resp.iter_text())
    events: list[tuple[str | None, str]] = []
    ev: str | None = None
    for line in text.splitlines():
        if line.startswith("event:"):
            ev = line[len("event:") :].strip()
        elif line.startswith("data:"):
            events.append((ev, line[len("data:") :].strip()))
            ev = None
    tool_calls = [json.loads(d)["toolCall"] for e, d in events if e == "tool_call"]
    deltas = [json.loads(d)["delta"] for e, d in events if e == "delta"]
    assert tool_calls == ["n_echo"]  # the function name is the wired tool NODE id (M22)
    assert "".join(deltas) == "streamed answer"


def test_m22_capability_node_does_not_run_as_a_step(pg_url: str) -> None:
    # A capability tool node is NOT executed in topo order — only called by the model. Proof: the
    # tool node's own ok/err handle never binds a node-level output (it is skipped from the walk),
    # yet the run still completes via the llm's answer. We assert the run output is the llm answer,
    # not an echoed step result, and the run is clean.
    with FakeInference(
        mode="tool_call", tool_name="n_echo", tool_args={"value": "x"}, response="final"
    ) as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = _run(client, _capability_graph(_builtin_tool_node()))
            persisted = client.get(f"/runs/{body['runId']}").json()
    assert body["status"] == "completed"
    assert body["output"] == "final"
    assert not persisted.get("error")


def test_m22_plain_llm_no_wires_is_single_shot(pg_url: str) -> None:
    # An llm with a `tools` port but NO wired tool node (and empty config.tools) is the pre-M21
    # single-shot path — the capability set is empty, so the loop runs zero iterations.
    ir = llm_ir("$in")
    ir["nodes"][1]["ports"]["in"].append(
        {"id": "tools", "type": "any", "required": False, "role": "tool"}
    )
    with FakeInference(response="just an answer") as server:
        app = create_app(inference_base_url=server.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            body = _run(client, ir)
    assert body["status"] == "completed"
    assert body["output"] == "just an answer"


# ── unit: capability binding + schema building from a node's inline config (review gaps) ─────────


def _http_tool_node(node_id: str = "n_http") -> dict:
    return {
        "id": node_id,
        "type": "tool",
        "kind": "activity",
        "config": {
            "tool": node_id,
            "connection": "c1",
            "method": "GET",
            "urlTemplate": "https://api.example.com/$in.q",
            "timeoutSeconds": 7.5,
            "description": "search the api",
            "parameterSchema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        },
        "ports": {
            "in": [{"id": "in", "type": "any", "required": True}],
            "out": [{"id": "out", "type": "any"}, {"id": "err", "type": "error"}],
        },
    }


def _mcp_tool_node(node_id: str = "n_mcp") -> dict:
    return {
        "id": node_id,
        "type": "mcp_tool",
        "kind": "activity",
        "config": {"server": "files", "tool": "read_file", "description": "read a file"},
        "ports": {
            "in": [{"id": "in", "type": "any", "required": True}],
            "out": [{"id": "out", "type": "any"}, {"id": "err", "type": "error"}],
        },
    }


def test_m22_http_tool_binding_has_timeout_seconds() -> None:
    # Regression (review #9): HttpTool gained `timeout_seconds`, so the autonomous loop's
    # `binding.timeout_seconds` (legacy M21 http binding) no longer AttributeErrors — and a
    # capability http tool can carry its configured timeout.
    assert "timeout_seconds" in HttpTool.model_fields


def test_m22_capability_binding_http_carries_inline_config() -> None:
    # review #2/#8: a capability http tool node's binding is built from its INLINE config — and the
    # configured timeoutSeconds is NOT dropped (it flows through HttpTool.timeout_seconds).
    ir = parse_document(_capability_graph(_http_tool_node()))
    binding = _capability_binding(ir, "n_http")
    assert isinstance(binding, HttpTool)
    assert binding.connection == "c1"
    assert binding.description == "search the api"
    assert binding.parameter_schema is not None
    assert binding.timeout_seconds == 7.5


def test_m22_capability_binding_mcp() -> None:
    # review #12: a capability mcp_tool node resolves to an McpTool from its inline server/tool.
    ir = parse_document(_capability_graph(_mcp_tool_node()))
    binding = _capability_binding(ir, "n_mcp")
    assert isinstance(binding, McpTool)
    assert binding.server == "files"
    assert binding.tool == "read_file"


async def test_m22_union_offers_legacy_key_and_capability_node_id() -> None:
    # review #11: one llm with a legacy ir.tools key (config.tools=["echo"]) AND a wired capability
    # node → the model is offered BOTH function names, keyed differently (the ir.tools key vs the
    # node id). This is the M22 union the loop concatenates.
    doc = _capability_graph(_http_tool_node())
    doc["tools"] = {"echo": {"kind": "builtin", "ref": "echo"}}
    doc["nodes"][1]["config"]["tools"] = ["echo"]
    ir = parse_document(doc)
    legacy = await build_tool_schemas(ir, ["echo"], registry=DEFAULT_REGISTRY, mcp=McpManager())
    cap = await build_capability_schemas(
        [n for n in ir.nodes if n.id == "n_http"], registry=DEFAULT_REGISTRY, mcp=McpManager()
    )
    names = {s["function"]["name"] for s in [*legacy, *cap]}
    assert names == {"echo", "n_http"}
