"""theygent IR — single source of truth for the cross-plane data contract.

Milestone 1 covers the inference-plane registration payload
(theygent-stack-9.1.md §9.1.3) and the capabilities response (§9.1.2).
The full Agent Graph IR (theygent-graph-schema.md §8) lands in later milestones.
"""

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
    "BINDING_NAMES",
    "MANAGED_BINDINGS",
    "Capabilities",
    "Lifecycle",
    "ManagedBinding",
    "ReachableBinding",
    "RegistrationPayload",
    "parse_registration",
]
