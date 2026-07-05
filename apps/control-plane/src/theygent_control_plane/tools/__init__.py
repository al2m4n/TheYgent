"""Built-in tools for the control-plane walker.

Deliberate fork: tools are **built-in Python callables registered in code**, not a runtime
tool-registration API and not shelled-out processes. External/third-party tools are an MCP
concern, which reuses this contract over a different transport. See
``apps/control-plane/CLAUDE.md`` for the recorded decision.
"""

from theygent_control_plane.tools.registry import (
    DEFAULT_REGISTRY,
    ToolNotFound,
    ToolRegistry,
)

__all__ = ["DEFAULT_REGISTRY", "ToolNotFound", "ToolRegistry"]
