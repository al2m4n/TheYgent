"""Fast suite for the ``mcp_tool`` node + the MCP server lifecycle.

Most of the surface runs against a *real* fake MCP server (``_fake_mcp_server.py``): a genuine
stdio subprocess speaking JSON-RPC, everything real except the content. The
``StdioMcpClient`` does the real protocol handshake; the ``McpManager`` does real lazy-connect,
reuse, and reconnect. Real Postgres via testcontainers.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from pathlib import Path

import pytest
from _ir import mcp_tool_ir, mcp_tool_ok_err_ir
from fastapi.testclient import TestClient

_FAKE_SERVER = str(Path(__file__).parent / "_fake_mcp_server.py")


def _register(client: TestClient, name: str = "fake", *, command: str | None = None, args=None):
    body = {
        "transport": "stdio",
        "command": command or sys.executable,
        "args": [_FAKE_SERVER] if args is None else args,
    }
    return client.put(f"/admin/mcp/servers/{name}", json=body)


def _run(client: TestClient, ir: dict, *, input_: str = "go") -> dict:
    resp = client.post("/graphs/runs", json={"ir": ir, "input": input_, "stream": False})
    return resp.json()


def _skipped(caplog: pytest.LogCaptureFixture, run_id: str) -> set[str]:
    return {
        str(getattr(r, "node_id", ""))
        for r in caplog.records
        if r.getMessage() == "graph.node"
        and getattr(r, "run_id", None) == run_id
        and getattr(r, "skipped", False)
    }


def _connected(client: TestClient, name: str = "fake") -> bool:
    servers = client.get("/admin/mcp/servers").json()["servers"]
    return next(s["connected"] for s in servers if s["name"] == name)


# ── admin: registration + capability probe ────────────────────────────────────


def test_register_list_and_capability_probe(client: TestClient) -> None:
    assert _register(client).status_code == 200
    # Registered, listed, NOT yet connected (lazy).
    assert not _connected(client)
    # The capability probe connects and returns the server's tools (cached).
    tools = client.get("/admin/mcp/servers/fake/tools").json()["tools"]
    assert {t["name"] for t in tools} >= {"echo", "boom", "pid"}
    assert _connected(client)  # the probe connected it


def test_get_unknown_server_404(client: TestClient) -> None:
    assert client.get("/admin/mcp/servers/ghost").status_code == 404
    assert client.get("/admin/mcp/servers/ghost").json()["error"]["code"] == "mcp_server_not_found"


def test_warm_close_delete_lifecycle(client: TestClient) -> None:
    _register(client)
    assert client.post("/admin/mcp/servers/fake:warm").status_code == 200
    assert _connected(client)
    assert client.post("/admin/mcp/servers/fake:close").status_code == 200
    assert not _connected(client)  # registered, just not connected
    assert client.delete("/admin/mcp/servers/fake").status_code == 204
    assert client.get("/admin/mcp/servers/fake").status_code == 404


def test_env_values_not_echoed(client: TestClient) -> None:
    # Sovereignty: env keys may be listed, values never returned.
    body = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [_FAKE_SERVER],
        "env": {"SECRET_TOKEN": "hunter2"},
    }
    r = client.put("/admin/mcp/servers/fake", json=body)
    assert r.status_code == 200
    view = r.json()
    assert view["envKeys"] == ["SECRET_TOKEN"]
    assert "hunter2" not in str(view)


# ── the mcp_tool node ─────────────────────────────────────────────────────────


def test_mcp_tool_happy_path(client: TestClient) -> None:
    _register(client)
    body = _run(client, mcp_tool_ir("fake", "echo", {"value": "$in"}), input_="hello")
    assert body["status"] == "completed"
    assert body["output"] == "hello"


def test_lazy_connect_then_reuse(client: TestClient) -> None:
    _register(client)
    assert not _connected(client)  # registering did NOT connect
    first = _run(client, mcp_tool_ir("fake", "pid", {}))
    assert first["status"] == "completed"
    assert _connected(client)  # first invocation connected
    second = _run(client, mcp_tool_ir("fake", "pid", {}))
    # Same pid => the connection (subprocess) was reused, not re-spawned (singleflight).
    assert second["output"] == first["output"]


def test_server_not_registered_rejected_no_run(client: TestClient) -> None:
    resp = client.post(
        "/graphs/runs",
        json={"ir": mcp_tool_ir("ghost", "echo", {"value": "$in"}), "input": "x", "stream": False},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "mcp_server_not_found"
    assert "runId" not in resp.json()


def test_tool_not_in_list_validation_when_connected(client: TestClient) -> None:
    _register(client)
    client.post("/admin/mcp/servers/fake:warm")  # connect, so the cache is available
    resp = client.post(
        "/graphs/runs",
        json={"ir": mcp_tool_ir("fake", "nope", {}), "input": "x", "stream": False},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "mcp_tool_not_found"


def test_tool_not_in_list_binds_err_when_not_connected(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # Not connected at validation -> IR accepted -> runtime connects, finds no such tool -> err.
    _register(client)
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _run(client, mcp_tool_ok_err_ir("fake", "nope", {}))
    assert body["status"] == "completed"
    assert "n_ok" in _skipped(caplog, body["runId"])  # err branch ran, ok skipped
    # The err payload (the n_err output value) is actionable: it names the server and the bad tool.
    assert "fake" in body["output"] and "nope" in body["output"]


def test_mcp_tool_error_binds_err(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    # The server reports a tool-level error (isError) -> bind err, run continues.
    _register(client)
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _run(client, mcp_tool_ok_err_ir("fake", "boom", {}))
    assert body["status"] == "completed"
    assert "n_ok" in _skipped(caplog, body["runId"])
    # The err payload carries the server's own message AND identifies it as a server-side error
    # (vs a transport failure) — the agent can tell "the tool ran and failed" from "couldn't reach".
    assert "boom" in body["output"]
    assert "fake" in body["output"] and "error" in body["output"].lower()


def test_arg_templating_through_mcp(client: TestClient) -> None:
    # $in.<port>.<field> reference resolution proved through the mcp_tool path.
    _register(client)
    body = _run(
        client,
        mcp_tool_ir("fake", "echo", {"value": "$in.in.payload"}),
        input_='{"payload": "deep"}',
    )
    assert body["status"] == "completed"
    assert body["output"] == "deep"


def test_transport_failure_reconnects(client: TestClient) -> None:
    # The server process dies between calls; the next invocation reconnects and completes.
    # Proven by the pid changing — a fresh subprocess was spawned.
    _register(client)
    first = _run(client, mcp_tool_ir("fake", "pid", {}))
    assert first["status"] == "completed"
    os.kill(int(first["output"]), signal.SIGKILL)
    second = _run(client, mcp_tool_ir("fake", "pid", {}))
    assert second["status"] == "completed"
    assert second["output"] != first["output"]  # reconnected to a fresh process


def test_connect_failure_binds_actionable_err(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    # A server that can't be (re)connected (bad command) -> after the reconnect-retry, bind err
    # cleanly, no hang, run continues. This is the same code
    # path a post-retry transport failure takes, so it's where we assert the payload is ACTIONABLE:
    # an empty string or a raw OS blob would pass "err bound" but leave the agent unable to act.
    _register(client, name="bad", command="theygent-no-such-binary-zzz")
    with caplog.at_level(logging.INFO, logger="theygent.control_plane.walker"):
        body = _run(client, mcp_tool_ok_err_ir("bad", "echo", {"value": "$in"}))
    assert body["status"] == "completed"
    assert "n_ok" in _skipped(caplog, body["runId"])  # err branch ran, ok skipped
    out = body["output"].lower()
    assert out.strip()  # not an empty payload
    assert "bad" in out  # the SERVER is identified (so the agent knows which dependency failed)
    assert "unavailable" in out  # the KIND is recognizable: a connection failure, not a tool error


def test_readyz_ready_with_unconnected_server(client: TestClient) -> None:
    # A registered-but-unconnected MCP server does NOT make the control-plane not-ready.
    _register(client)
    resp = client.get("/readyz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert any(s["name"] == "fake" and not s["connected"] for s in body["mcp"]["servers"])


# ── registry persistence across restart ───────────────────────────────────────


def test_mcp_registration_persists_across_restart(fake_inference, pg_url: str) -> None:
    # The McpManager persists registrations to Postgres and rehydrates on startup
    # (connections stay lazy — only registration survives).
    from theygent_control_plane.app import create_app

    app1 = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app1) as c1:
        assert _register(c1, name="fs-local").status_code == 200
        assert any(s["name"] == "fs-local" for s in c1.get("/admin/mcp/servers").json()["servers"])

    # Simulate a restart: new app + manager, SAME database. The registration is rehydrated.
    app2 = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app2) as c2:
        servers = c2.get("/admin/mcp/servers").json()["servers"]
        assert any(s["name"] == "fs-local" for s in servers)
        # And it round-tripped enough to be usable — the command/args came back (not connected).
        detail = c2.get("/admin/mcp/servers/fs-local").json()
        assert detail["command"] and not detail["connected"]


def test_mcp_deletion_persists_across_restart(fake_inference, pg_url: str) -> None:
    from theygent_control_plane.app import create_app

    app1 = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app1) as c1:
        _register(c1, name="gone")
        assert c1.delete("/admin/mcp/servers/gone").status_code == 204

    app2 = create_app(inference_base_url=fake_inference.v1_url, database_url=pg_url)
    with TestClient(app2) as c2:
        servers = c2.get("/admin/mcp/servers").json()["servers"]
        assert not any(s["name"] == "gone" for s in servers)  # the delete survived too
