"""The one authorization chokepoint — built as a seam now, enforcement deferred.

Even when I/O *is* persisted, **who may view it** is a separate axis from whether it was captured.
This module freezes the seam every sensitive trace/io read flows through — a single
``authorize(principal, permission, resource) -> bool`` — so the future Governance/Identity milestone
fills the function and **every read surface is gated with zero retrofit**. In single-user localhost
it returns allow; the endpoints route through it *from day one* (retrofitting auth onto N endpoints
later is the expensive path this step avoids).

**This builds NO identity, roles, SSO, or RBAC enforcement** (the Do-NOT). That needs an auth
layer the platform doesn't have, is the paid Team-tier / Phase-4 governance feature, and the
exit-strategy is explicitly "build the interface, document the seam, let the acquirer implement."
A working chokepoint every sensitive read already passes through is the acquirer-legible artifact; a
half-built RBAC system is not. So: a ``Principal`` placeholder, three permission strings, and one
no-op-allow function. The shape — ``authorize(principal, permission, resource)`` — is the
hard-to-reverse part (it is what lets roles/grants slot in behind it unchanged), not the body.

Mirrors the ``require_token`` discipline: the single global token there is the degenerate
single-tenant case of a future token→principal lookup; this authorizer is the degenerate
single-tenant case of a future principal→roles→grants check. Both are the seam, not the system.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger("theygent.control_plane.governance")

#: The permissions this module gates on. ``trace:read`` and ``io:read`` gate the two read surfaces
#: (timing vs. payloads — deliberately distinct so "not captured" and "captured but not visible to
#: you" can be different answers); ``agent:configure`` gates editing an agent's capture policy;
#: ``settings:read``/``settings:write`` gate the platform-settings surface (read and write are
#: separate axes for the same reason trace/io are).
Permission = Literal["trace:read", "io:read", "agent:configure", "settings:read", "settings:write"]


@dataclass(frozen=True)
class Principal:
    """The caller's identity placeholder. In single-user localhost there is exactly one,
    anonymous principal; the Governance/Identity milestone fills ``id``/roles/workspace from the
    resolved bearer (the ``require_token`` attach-point) WITHOUT reshaping the call sites. Built
    as a slot, not a system — nothing consumes ``id`` yet, deliberately (a speculative principal
    that nothing reads would erode the seam — the same discipline as keeping other deferred
    identifiers unconsumed until the feature that needs them is built)."""

    id: str | None = None


#: The single-user principal every request resolves to today (no auth layer yet). The future
#: Governance milestone replaces this with a real resolution off the request credential.
LOCAL_PRINCIPAL = Principal(id=None)


def authorize(principal: Principal, permission: Permission, resource: Any) -> bool:
    """THE authorization chokepoint. Returns whether ``principal`` may exercise ``permission``
    on ``resource``. **Single-user localhost: always allow** — but every sensitive read/config
    endpoint routes through HERE, so the Governance/Identity milestone makes this one function
    resolve real principals → roles → grants (and the ``agent``/``run`` rows gain the deferred
    ``owner_id``/``workspace_id`` columns) and every gate tightens at once, with no endpoint change.

    ``resource`` is the thing being acted on (a ``Run`` for trace/io reads, an agent id for config)
    — accepted as ``Any`` because the seam must not assume a resource shape the auth layer hasn't
    designed yet. Do NOT add role checks / ownership logic here: a no-op allow that
    everything passes through is the deliverable; a fake gate is not."""
    return True
