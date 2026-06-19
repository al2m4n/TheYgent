"""theygent IR — single source of truth for the cross-plane data contract.

Milestone 1 froze the inference-plane registration payload (``registration.py`` —
theygent-stack-9.1.md §9.1.3) and the capabilities response (§9.1.2). Milestone 5 lands the
rest of the Agent Graph IR (``graph.py`` / ``contenthash.py`` — theygent-graph-schema.md §8):
the document envelope, nodes/edges, model bindings, the ``kind`` taxonomy, and the
content-hash rule. This package stays pure — Pydantic models + static graph algorithms, no
engine SDK, no gateway client. Execution (the walker) lives in the control-plane.
"""

from theygent_ir.contenthash import content_hash
from theygent_ir.graph import (
    EXECUTABLE_TYPES,
    NODE_TYPE_KIND,
    BuiltinTool,
    Edge,
    GraphValidationError,
    IRDocument,
    LlmConfig,
    McpTool,
    McpToolConfig,
    ModelBinding,
    Node,
    NodeKind,
    Port,
    Ports,
    RouterConfig,
    ToolConfig,
    parse_document,
    topological_order,
    validate_graph,
)
from theygent_ir.registration import (
    BINDING_NAMES,
    MANAGED_BINDINGS,
    Capabilities,
    Lifecycle,
    ManagedBinding,
    ReachableBinding,
    RegistrationPayload,
    parse_registration,
)

__all__ = [
    # registration (M1)
    "BINDING_NAMES",
    # agent graph IR (M5)
    "EXECUTABLE_TYPES",
    "MANAGED_BINDINGS",
    "NODE_TYPE_KIND",
    "BuiltinTool",
    "Capabilities",
    "Edge",
    "GraphValidationError",
    "IRDocument",
    "Lifecycle",
    "LlmConfig",
    "ManagedBinding",
    "McpTool",
    "McpToolConfig",
    "ModelBinding",
    "Node",
    "NodeKind",
    "Port",
    "Ports",
    "ReachableBinding",
    "RegistrationPayload",
    "RouterConfig",
    "ToolConfig",
    "content_hash",
    "parse_document",
    "parse_registration",
    "topological_order",
    "validate_graph",
]
