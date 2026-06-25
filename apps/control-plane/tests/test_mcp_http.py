"""M19 §2.4 — mcp_tool made real over BOTH transports (stdio + HTTP) via a connection.

Guards:
* ``HttpMcpClient`` does the real streamable-HTTP handshake against a real MCP server (the same
  ``_fake_mcp_server`` FastMCP instance, served over HTTP) — list_tools + call_tool round-trip;
* a ``connection(kind=mcp_server)`` (stdio OR http) drives an ``mcp_tool`` node end-to-end through
  the walker, with auth resolved server-side (the IR carries the connection id, never a secret);
* the transport factory dispatches by ``transport``.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi.testclient import TestClient
from mcp.server.fastmcp import FastMCP
from theygent_control_plane.mcp import HttpMcpClient, McpServerConfig
from theygent_control_plane.mcp.manager import _default_factory

_FAKE_SERVER = str(Path(__file__).parent / "_fake_mcp_server.py")


@contextmanager
def http_mcp_server() -> Iterator[str]:
    """Serve a fresh fake FastMCP server over streamable-HTTP on an ephemeral port (real server +
    real transport — the same posture as ``FakeInference``). A FRESH instance per call: a FastMCP's
    streamable-HTTP session manager runs once, so two servers can't share one instance. Yields the
    ``/mcp`` endpoint url."""
    fake = FastMCP("theygent-fake-mcp-http")

    @fake.tool()
    def echo(value: str) -> str:
        return value

    app = fake.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    srv = uvicorn.Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not srv.started:
            if time.monotonic() > deadline:
                raise RuntimeError("http mcp server did not start")
            time.sleep(0.01)
        port = srv.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        srv.should_exit = True
        thread.join(timeout=5)


# ── the HttpMcpClient transport (the new piece) ───────────────────────────────────────────────────


def test_http_mcp_client_lists_and_calls() -> None:
    async def _run() -> None:
        with http_mcp_server() as url:
            client = HttpMcpClient(url=url)
            await client.connect()
            try:
                tools = {t.name for t in await client.list_tools()}
                assert "echo" in tools
                result = await client.call_tool("echo", {"value": "over-http"})
                assert result.value == "over-http"
                assert result.is_error is False
            finally:
                await client.close()

    asyncio.run(_run())


def test_default_factory_dispatches_by_transport() -> None:
    http = _default_factory(McpServerConfig(transport="http", url="http://x/mcp"))
    assert isinstance(http, HttpMcpClient)
    from theygent_control_plane.mcp import StdioMcpClient

    stdio = _default_factory(McpServerConfig(transport="stdio", command="python"))
    assert isinstance(stdio, StdioMcpClient)


# ── connection-backed mcp_tool end-to-end through the walker (both transports) ──────────


def _mcp_agent(connection_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "id": "agt_mcp",
        "name": "mcp-agent",
        "version": "0.1.0",
        "models": {},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_mcp",
                "type": "mcp_tool",
                "kind": "activity",
                "config": {"connection": connection_id, "tool": "echo", "args": {"value": "$in"}},
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
                "target": "n_mcp",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_mcp",
                "sourceHandle": "ok",
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }


def test_mcp_tool_via_stdio_connection(client: TestClient) -> None:
    con = client.post(
        "/connections",
        json={
            "name": "fs",
            "kind": "mcp_server",
            "config": {"transport": "stdio", "command": sys.executable, "args": [_FAKE_SERVER]},
        },
    ).json()["id"]
    out = client.post(
        "/graphs/runs", json={"ir": _mcp_agent(con), "input": "echo-me", "stream": False}
    ).json()
    assert out["status"] == "completed", out
    assert out["output"] == "echo-me"  # the connection-backed stdio MCP echoed the input


def test_mcp_tool_via_http_connection(client: TestClient) -> None:
    with http_mcp_server() as url:
        con = client.post(
            "/connections",
            json={
                "name": "remote-mcp",
                "kind": "mcp_server",
                "config": {"transport": "http", "url": url},
            },
        ).json()["id"]
        out = client.post(
            "/graphs/runs", json={"ir": _mcp_agent(con), "input": "echo-http", "stream": False}
        ).json()
        assert out["status"] == "completed", out
        assert out["output"] == "echo-http"  # reached the remote MCP over streamable-HTTP


def test_mcp_tool_missing_connection_binds_err(client: TestClient) -> None:
    ir = _mcp_agent("con_nope")
    # err isn't wired to output → the run completes with no output (honest empty), not a crash.
    out = client.post("/graphs/runs", json={"ir": ir, "input": "x", "stream": False}).json()
    assert out["status"] == "completed"
