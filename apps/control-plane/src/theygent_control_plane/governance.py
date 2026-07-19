"""The one authorization chokepoint — now enforcing the three-role model.

Every sensitive read/config surface has routed through ``authorize(principal, permission,
resource)`` since long before an identity layer existed — the seam was frozen precisely so
enforcement could land here once, with zero endpoint retrofit. That day is now: ``Principal``
carries the role the auth layer (``auth.py`` + ``require_auth`` in ``app.py``) resolved from the
presented bearer, and ``authorize`` ranks it against a per-permission floor.

The mapping stays deliberately coarse — a permission maps to a minimum role, nothing
resource-instance-specific. Ownership checks (a viewer reading only their own chat sessions) live
at the endpoints that carry an owner column; this module answers only "may this ROLE exercise
this PERMISSION at all?". Keep it that way: a second, hidden ownership system inside the
chokepoint would be impossible to audit from the route table.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from theygent_control_plane.auth import Role, role_at_least

logger = logging.getLogger("theygent.control_plane.governance")

#: The permissions this module gates on. ``trace:read`` and ``io:read`` gate the two read surfaces
#: (timing vs. payloads — distinct so "not captured" and "captured but not visible to
#: you" can be different answers); ``agent:configure`` gates editing an agent's capture policy;
#: ``settings:read``/``settings:write`` gate the platform-settings surface (read and write are
#: separate axes for the same reason trace/io are); ``transfer:export``/``transfer:import`` gate
#: the whole-install bundle surface (an export aggregates every sensitive read at once, an import
#: writes across every table — both must tighten with the rest when RBAC lands).
Permission = Literal[
    "trace:read",
    "io:read",
    "agent:configure",
    "settings:read",
    "settings:write",
    "transfer:export",
    "transfer:import",
]

#: Minimum role per permission. Viewers keep trace/io read: the chat surface embeds the run
#: waterfall (and what payloads exist there is already governed by the per-agent IO policy);
#: configuring agents is builder work; settings and whole-install transfer are the install
#: owner's — an export aggregates every secret-adjacent read at once.
_PERMISSION_FLOOR: dict[Permission, Role] = {
    "trace:read": "viewer",
    "io:read": "viewer",
    "agent:configure": "editor",
    "settings:read": "admin",
    "settings:write": "admin",
    "transfer:export": "admin",
    "transfer:import": "admin",
}


@dataclass(frozen=True)
class Principal:
    """The caller's resolved identity. ``id`` is the account id (``None`` for the process
    itself); ``role`` is the EFFECTIVE role of the presented credential — an API key's meet
    with its owner, never the account ceiling blindly."""

    id: str | None = None
    role: Role = "admin"


#: The process acting as itself — internal paths (dispatcher-fired runs, boot reconciles) that
#: never traverse the HTTP auth layer. Full authority by construction, not by token.
SYSTEM_PRINCIPAL = Principal(id=None, role="admin")


def authorize(principal: Principal, permission: Permission, resource: Any) -> bool:
    """THE authorization chokepoint: whether ``principal`` may exercise ``permission`` on
    ``resource``. Role-vs-floor only — ``resource`` stays in the signature (and is deliberately
    unused) so instance-level policy can land here later without touching a call site."""
    del resource  # coarse role gate today; the parameter is the seam for finer policy
    return role_at_least(principal.role, _PERMISSION_FLOOR[permission])
