"""Who is calling, and how the gateway authenticates upstream on their behalf.

Personal mode has no identities: the gateway listens on loopback only and forwards the caller's own
credential. Team mode authenticates every request with a developer's gateway key and fails closed:
no key, an unknown key, or no key set loaded yet is a refusal, never a pass.

A gateway key reaches the gateway three ways, matching how Claude Code can be configured:

* the ``x-theygent-gateway-key`` header (``ANTHROPIC_CUSTOM_HEADERS``) — subscription seats, whose
  ``Authorization`` header must keep carrying their claude.ai login;
* ``Authorization: Bearer tyg_…`` (``ANTHROPIC_AUTH_TOKEN``) or ``x-api-key: tyg_…``
  (``ANTHROPIC_API_KEY``) — org-billed developers, who hold no Anthropic credential at all.

The gateway compares SHA-256 digests only: the key set it syncs from the control plane carries no
token, and the set it keeps on disk (so authentication survives a control-plane outage) is the same
digests. A ``tyg_`` token is never forwarded upstream.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import ValidationError
from theygent_ir.gateway import CredentialProfile, GatewayKey, GatewayKeySet

log = logging.getLogger(__name__)

GATEWAY_KEY_HEADER = "x-theygent-gateway-key"
#: Prefix of a gateway key token, as the control plane mints them.
GATEWAY_KEY_PREFIX = "tyg_"
KEY_SET_FILE = "keyset.json"


@dataclass(frozen=True)
class Identity:
    """An authenticated developer (team mode)."""

    developer_id: str
    key_id: str
    credential_profile: CredentialProfile


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def presented_gateway_key(headers: dict[str, str]) -> str | None:
    """The gateway key a request carries, if any. The dedicated header wins; otherwise a
    ``tyg_`` token in ``Authorization`` or ``x-api-key``."""
    explicit = headers.get(GATEWAY_KEY_HEADER, "").strip()
    if explicit:
        return explicit
    authorization = headers.get("authorization", "").strip()
    if authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
        if bearer.startswith(GATEWAY_KEY_PREFIX):
            return bearer
    api_key = headers.get("x-api-key", "").strip()
    if api_key.startswith(GATEWAY_KEY_PREFIX):
        return api_key
    return None


class KeyRing:
    """The current key set, looked up by token digest. Only ever moves forward in version, and
    persists each accepted set so a restart without the control plane still authenticates."""

    def __init__(self, state_dir: Path | None = None) -> None:
        self._path = state_dir / KEY_SET_FILE if state_dir is not None else None
        self._set: GatewayKeySet | None = None
        self._by_digest: dict[str, GatewayKey] = {}

    @property
    def loaded(self) -> bool:
        return self._set is not None

    @property
    def version(self) -> int | None:
        return self._set.version if self._set is not None else None

    def lookup(self, token: str) -> Identity | None:
        key = self._by_digest.get(token_digest(token))
        if key is None:
            return None
        return Identity(key.developer_id, key.key_id, key.credential_profile)

    def replace(self, key_set: GatewayKeySet) -> bool:
        """Adopt ``key_set`` unless it is older than the current one. Returns whether it was
        adopted."""
        if self._set is not None and key_set.version < self._set.version:
            return False
        self._set = key_set
        self._by_digest = {key.token_sha256: key for key in key_set.keys}
        self._persist(key_set)
        return True

    def load_from_disk(self) -> bool:
        """Adopt the last persisted set. A missing or unreadable file is no set, never a crash."""
        if self._path is None or not self._path.exists():
            return False
        try:
            key_set = GatewayKeySet.model_validate_json(self._path.read_text())
        except (OSError, ValueError, ValidationError) as exc:
            log.warning("ignoring the persisted key set: %s", type(exc).__name__)
            return False
        return self.replace(key_set)

    def _persist(self, key_set: GatewayKeySet) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(key_set.model_dump_json())
            temporary.replace(self._path)
        except OSError as exc:
            log.warning("could not persist the key set: %s", type(exc).__name__)


class KeySource(Protocol):
    async def fetch(self, since_version: int | None) -> GatewayKeySet | None:
        """The current key set, or ``None`` when it has not changed since ``since_version``."""
        ...


class ControlPlaneKeySource:
    """Fetches the key set from the control plane's ``GET /gateway/keys/sync``, authenticated
    with the gateway's service key. ``304`` means unchanged."""

    def __init__(self, client: httpx.AsyncClient, control_plane_url: str, service_key: str) -> None:
        self._client = client
        self._url = f"{control_plane_url}/gateway/keys/sync"
        self._service_key = service_key

    async def fetch(self, since_version: int | None) -> GatewayKeySet | None:
        params = {"since": str(since_version)} if since_version is not None else None
        response = await self._client.get(
            self._url,
            params=params,
            headers={"authorization": f"Bearer {self._service_key}"},
        )
        if response.status_code == 304:
            return None
        response.raise_for_status()
        return GatewayKeySet.model_validate_json(response.content)


class StaticKeySource:
    """A key source whose set a caller sets directly (tests, and single-machine setups)."""

    def __init__(self, key_set: GatewayKeySet | None = None) -> None:
        self.key_set = key_set

    async def fetch(self, since_version: int | None) -> GatewayKeySet | None:
        if self.key_set is None or self.key_set.version == since_version:
            return None
        return self.key_set


async def sync_once(ring: KeyRing, source: KeySource) -> None:
    """One sync. A failed fetch keeps the current set: the last known keys keep working, and a
    revoked key is refused as soon as a sync succeeds."""
    try:
        key_set = await source.fetch(ring.version)
    except (httpx.HTTPError, ValueError, ValidationError) as exc:
        log.warning("key set sync failed: %s", type(exc).__name__)
        return
    if key_set is not None and ring.replace(key_set):
        log.info("key set version %s adopted (%d keys)", key_set.version, len(key_set.keys))


async def sync_forever(ring: KeyRing, source: KeySource, interval_seconds: float) -> None:
    while True:
        await sync_once(ring, source)
        await asyncio.sleep(interval_seconds)


def key_set_summary(ring: KeyRing) -> dict[str, object]:
    """For ``/readyz``: whether keys are loaded and which version — never the keys."""
    return {"keySetLoaded": ring.loaded, "keySetVersion": ring.version}
