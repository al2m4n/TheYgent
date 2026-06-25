"""MCP (Model Context Protocol) support — the ``mcp_tool`` node's lifecycle layer (m7.md §3).

The IR seam is identical to an M6 ``tool`` (call something with args, get a value back); the
lifecycle underneath is new — external server processes with connection state and reconnection.
``McpClient`` is the transport seam (stdio only in M7); ``McpManager`` owns the named-server
registry and connections. See ``apps/control-plane/CLAUDE.md`` for the two recorded M7 forks.
"""

from theygent_control_plane.mcp.client import (
    HttpMcpClient,
    McpClient,
    McpConnectionError,
    McpResult,
    McpServerConfig,
    McpToolDescriptor,
    McpToolNotFound,
    StdioMcpClient,
)
from theygent_control_plane.mcp.manager import McpManager, McpServerNotFound

__all__ = [
    "HttpMcpClient",
    "McpClient",
    "McpConnectionError",
    "McpManager",
    "McpResult",
    "McpServerConfig",
    "McpServerNotFound",
    "McpToolDescriptor",
    "McpToolNotFound",
    "StdioMcpClient",
]
