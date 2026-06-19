"""A real, minimal MCP server for the fast suite (m7.md §8) — a genuine stdio subprocess speaking
the JSON-RPC protocol, everything real except the *content*. Same posture as ``_fake_inference``
and ``_http``: spawn a real process so the ``StdioMcpClient`` actually does the protocol handshake
across real pipes, not a mock.

Run as ``python _fake_mcp_server.py`` (the manager registration's ``command``/``args``). Tools:
  * ``echo(value)`` — return the value unchanged (proves dispatch + arg templating).
  * ``boom()``      — raise, so the server reports a tool-level error (``isError``) -> walker err.
  * ``pid()``       — return this process's pid, so a test can prove connection reuse (same pid on
                      a second call) and reconnection (a new pid after the process is killed).
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

server = FastMCP("theygent-fake-mcp")


@server.tool()
def echo(value: str) -> str:
    return value


@server.tool()
def boom() -> str:
    raise ValueError("boom from server")


@server.tool()
def pid() -> str:
    return str(os.getpid())


if __name__ == "__main__":
    server.run()  # stdio transport (the M7 default)
