"""Local-only credential resolution + a user-side credential store for reachable
(`openai-compatible`) bindings.

The sovereignty invariant is enforced here, at the interface: a ``credentialRef`` resolves in the
user's trust domain and NEVER crosses a cloud boundary. A ``secret://NAME`` reference resolves NAME
against the LOCAL credential store first, then the process environment (a desktop build can back the
store with the OS keychain behind the same interface). The raw secret is read here and handed
straight to the upstream call — it is never logged, echoed over the wire, or forwarded to the
control plane.

The store lives in the inference plane's own state dir (the user's machine), symmetric with the
model registry: a small local JSON file, never the control-plane's Postgres. Values are
**write-only** over the ``/admin/credentials`` wire — a listing returns names + ``hasValue`` only,
never a value — and the file is written user-only (0600). ``state_path=None`` keeps the store purely
in memory (the fast suite never touches disk).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from pathlib import Path

SCHEME = "secret://"
#: A credential name is env-var-shaped (it doubles as the env fallback key): a letter/underscore
#: start, then letters/digits/underscore/dot/dash. Keeps ``secret://NAME`` refs unambiguous.
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class CredentialResolutionError(RuntimeError):
    pass


class InvalidCredentialName(ValueError):
    pass


class CredentialStore:
    """User-side named-secret store, local to the inference plane (never the control plane).

    Values are write-only over the wire: :meth:`names` never returns a value. Persistence is a
    user-only (0600) JSON file in the plane's state dir; ``state_path=None`` is pure in-memory. A
    desktop build can swap the on-disk file for the OS keychain behind this same interface.
    """

    def __init__(self, state_path: Path | None = None) -> None:
        self._values: dict[str, str] = {}
        self._state_path = state_path
        if state_path is not None:
            self._load()

    # ── local persistence (0600 JSON state file, inference plane only) ───────────

    def _load(self) -> None:
        """Rehydrate from the local state file, if present. A missing file is first-run (empty); a
        corrupt file is ignored, not fatal, on boot (the store is recoverable, not canonical)."""
        assert self._state_path is not None
        try:
            raw = json.loads(self._state_path.read_text())
        except (OSError, ValueError):
            return
        creds = raw.get("credentials", {})
        if isinstance(creds, dict):
            self._values = {str(k): v for k, v in creds.items() if isinstance(v, str)}

    def _persist(self) -> None:
        """Atomically write the store (write-temp-then-rename), user-only. ``mkstemp`` creates the
        temp file 0600 and ``os.replace`` preserves it, so the file is never world-readable."""
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._state_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump({"credentials": self._values}, fh)
            os.replace(tmp, self._state_path)
            with contextlib.suppress(OSError):  # belt-and-suspenders; no-op where unsupported
                os.chmod(self._state_path, 0o600)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    # ── store operations (values in; names out — never values) ───────────────────

    def names(self) -> list[str]:
        return sorted(self._values)

    def set(self, name: str, value: str) -> None:
        if not _NAME_RE.match(name):
            raise InvalidCredentialName(
                f"credential name must match {_NAME_RE.pattern!r}, got {name!r}"
            )
        self._values[name] = value
        self._persist()

    def delete(self, name: str) -> bool:
        existed = self._values.pop(name, None) is not None
        if existed:
            self._persist()
        return existed

    def get(self, name: str) -> str | None:
        return self._values.get(name)


def resolve_credential(
    credential_ref: str | None, store: CredentialStore | None = None
) -> str | None:
    """Resolve a ``secret://NAME`` reference to a raw secret, in the user's trust domain only.

    Order: the local :class:`CredentialStore` (Settings-managed), then the process environment (a
    bare exported env var). Either is user-side — the value never crosses to the control plane."""
    if credential_ref is None:
        return None
    if not credential_ref.startswith(SCHEME):
        raise CredentialResolutionError(
            f"credentialRef must use the {SCHEME!r} scheme, got {credential_ref!r}"
        )
    name = credential_ref[len(SCHEME) :]
    secret = (store.get(name) if store is not None else None) or os.environ.get(name)
    if secret is None:
        raise CredentialResolutionError(
            f"no local secret for {credential_ref!r} "
            f"(not in the credential store, and env var {name!r} is unset)"
        )
    return secret
