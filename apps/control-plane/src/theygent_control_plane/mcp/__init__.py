"""MCP (Model Context Protocol) support — the ``mcp_tool`` node's lifecycle layer.

The IR seam is identical to a local ``tool`` (call something with args, get a value back); the
lifecycle underneath is new — external server processes with connection state and reconnection.
``McpClient`` is the transport seam (stdio initially); ``McpManager`` owns the named-server
registry and connections.
"""

from theygent_control_plane.mcp.client import (
    HttpMcpClient,
    McpClient,
    McpConnectionError,
    McpResult,
    McpServerConfig,
    McpToolDescriptor,
    McpToolNotFound,
    SseMcpClient,
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
    "SseMcpClient",
    "StdioMcpClient",
]
