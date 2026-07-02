"""A `tool` node resolves its binding by shape, and the up-front check catches the wrong shape.

Three cases the runtime used to get subtly wrong for a `tool` node whose ``config.tool`` is a key
in ``ir.tools`` (rather than a directly-registered builtin name):

* a ``builtin`` binding names its callable via ``ref`` — the step must resolve the ref, not look
  up the ir.tools KEY in the registry (which would fail with a cryptic ``err``);
* an ``mcp`` binding is the wrong shape for a step-mode ``tool`` node (that is the ``mcp_tool``
  node's job) — the up-front check rejects it with a clear pointer, not a runtime ``err``;
* a directly-registered builtin name (not in ir.tools) still runs unchanged.
"""

from __future__ import annotations

from typing import Any

from _ir import _doc, _edge, _node
from fastapi.testclient import TestClient


def _tool_agent(aid: str, tool_key: str, tools: dict[str, Any], args: dict[str, Any]) -> dict:
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_t",
                "tool",
                "activity",
                config={"tool": tool_key, "args": args},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_t"), _edge("e2", "n_t", "ok", "n_out")],
    )
    ir["id"] = aid
    ir["tools"] = tools
    return ir


def _run(client: TestClient, ir: dict, value: Any) -> dict:
    r = client.post("/graphs/runs", json={"ir": ir, "input": value, "stream": False})
    return r.status_code, r.json()


def test_builtin_binding_resolves_its_ref(client: TestClient) -> None:
    # config.tool is an ir.tools KEY ("myecho") whose builtin binding refs the registered "echo".
    # The step must run "echo" (the ref), not look up "myecho" in the registry.
    ir = _tool_agent(
        "agt_builtin_ref",
        "myecho",
        {"myecho": {"kind": "builtin", "ref": "echo"}},
        {"value": "$in"},
    )
    status, body = _run(client, ir, "resolve-me")
    assert status == 200
    assert body["status"] == "completed"
    assert body["output"] == "resolve-me"  # echo ran; no cryptic 'myecho' err


def test_mcp_binding_on_a_plain_tool_node_is_a_clear_400(client: TestClient) -> None:
    # An mcp binding referenced by a plain `tool` node is the wrong shape — rejected up front with
    # a clear code, not a Run that binds a cryptic err at step time.
    ir = _tool_agent(
        "agt_tool_mcp_mismatch",
        "mymcp",
        {"mymcp": {"kind": "mcp", "tool": "echo", "server": "some-server"}},
        {"message": "$in"},
    )
    status, body = _run(client, ir, "x")
    assert status == 400
    assert body["error"]["code"] == "tool_binding_mismatch"
    assert "mcp_tool" in body["error"]["message"]


def test_directly_registered_builtin_still_runs(client: TestClient) -> None:
    # The common path: config.tool is a registered builtin name, not an ir.tools key — unchanged.
    ir = _tool_agent("agt_direct_builtin", "echo", {}, {"value": "$in"})
    status, body = _run(client, ir, "direct")
    assert status == 200
    assert body["output"] == "direct"
