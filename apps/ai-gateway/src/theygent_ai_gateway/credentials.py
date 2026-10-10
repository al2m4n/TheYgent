"""The organization's Anthropic key, as the gateway holds it.

The key is a deployment secret: normally ``THEYGENT_AI_GATEWAY_ORG_API_KEY`` (a Kubernetes
Secret, or the environment). A single-instance install may instead write it through the gateway's
admin surface; it is then kept in the gateway's state directory (``0600``), never in the control
plane, and applies at once. A key the deployment set wins: the admin surface cannot replace it.

Nothing here logs, returns or reprs the key; ``status`` says only whether it is set and where it
came from.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

ORG_KEY_FILE = "org-api-key.json"
#: An API key is one opaque token: printable, no whitespace, of a plausible length.
_KEY = re.compile(r"[\x21-\x7e]{16,512}")

KeySource = Literal["env", "stored"]


class OrgKeyError(Exception):
    """A write the gateway refuses. ``code`` is the stable name the admin surface returns."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class OrgKeyStatus:
    set: bool
    source: KeySource | None
    updated_at: datetime | None
    #: Whether the admin surface may write it: a key from the deployment cannot be replaced, and
    #: personal mode forwards the caller's own credential.
    writable: bool


class OrgKey:
    def __init__(self, *, env_value: str | None, state_dir: Path | None, applicable: bool) -> None:
        self._env = env_value
        self._path = state_dir / ORG_KEY_FILE if state_dir is not None else None
        self._applicable = applicable
        self._stored: str | None = None
        self._updated_at: datetime | None = None

    def __repr__(self) -> str:
        return f"OrgKey(status={self.status()!r})"

    @property
    def value(self) -> str | None:
        """The key to send upstream for org-billed developers."""
        return self._env if self._env is not None else self._stored

    def status(self) -> OrgKeyStatus:
        source: KeySource | None = (
            "env" if self._env is not None else "stored" if self._stored is not None else None
        )
        return OrgKeyStatus(
            set=source is not None,
            source=source,
            updated_at=self._updated_at if source == "stored" else None,
            writable=self._applicable and self._env is None,
        )

    def load(self) -> None:
        """Adopt the stored key, if any. An unreadable file is no key, never a crash."""
        if self._path is None or not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            key, updated = data["api_key"], datetime.fromisoformat(data["updated_at"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("ignoring the stored org key: %s", type(exc).__name__)
            return
        if isinstance(key, str) and _KEY.fullmatch(key):
            self._stored, self._updated_at = key, updated

    def _writable(self) -> None:
        if not self._applicable:
            raise OrgKeyError(
                "org_key_not_applicable",
                "personal mode forwards the caller's own credential; it uses no org key",
            )
        if self._env is not None:
            raise OrgKeyError(
                "org_key_pinned",
                "the org key is set by the deployment (THEYGENT_AI_GATEWAY_ORG_API_KEY); "
                "change it there",
            )

    def set(self, key: str, *, now: datetime | None = None) -> None:
        self._writable()
        if not _KEY.fullmatch(key):
            raise OrgKeyError(
                "invalid_api_key", "an API key is 16-512 printable characters, no spaces"
            )
        when = now or datetime.now(UTC)
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"api_key": key, "updated_at": when.isoformat()}, handle)
            temporary.replace(self._path)
        self._stored, self._updated_at = key, when

    def clear(self) -> None:
        self._writable()
        if self._path is not None:
            self._path.unlink(missing_ok=True)
        self._stored, self._updated_at = None, None
