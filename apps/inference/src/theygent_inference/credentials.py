"""Local-only credential resolution for reachable (`openai-compatible`) bindings.

The §10 invariant is enforced here, at the interface: a ``credentialRef`` resolves
in the user's trust domain and NEVER crosses a cloud boundary. M1 resolves
``secret://NAME`` from the local environment; a desktop build resolves the same
ref against the OS keychain. The raw secret is read here and handed straight to
the upstream call — it is never logged, stored, or forwarded to the control plane.
"""

from __future__ import annotations

import os

SCHEME = "secret://"


class CredentialResolutionError(RuntimeError):
    pass


def resolve_credential(credential_ref: str | None) -> str | None:
    if credential_ref is None:
        return None
    if not credential_ref.startswith(SCHEME):
        raise CredentialResolutionError(
            f"credentialRef must use the {SCHEME!r} scheme, got {credential_ref!r}"
        )
    name = credential_ref[len(SCHEME) :]
    secret = os.environ.get(name)
    if secret is None:
        raise CredentialResolutionError(
            f"no local secret found for {credential_ref!r} (env var {name!r} unset)"
        )
    return secret
