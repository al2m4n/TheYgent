"""Encrypted-at-rest secret storage — the ``secret_ref`` indirection.

A ``connection`` (``ConnectionRow`` / ``run.Connection``) references a ``secret_ref``; the raw
secret material (api key, bearer token, OAuth client secret, basic password, MCP auth) is stored
**encrypted** in the ``secret`` table — Fernet (AES-128-CBC + HMAC-SHA256) — and decrypted only
server-side, inside the durable step that calls the tool/MCP. The plaintext NEVER reaches the IR,
the canvas, a span, the journal, or any wire response (server-side-only resolution posture).

This is the **minimal honest indirection**, not a production KMS: the key is a process-level env
secret (``THEYGENT_SECRET_KEY``), not a per-tenant envelope key. The hard-to-reverse half is the
*shape* — secrets behind a ``secret_ref``, never inline in the IR — which a real vault slots behind
unchanged. Migrating the at-rest mechanism (env Fernet key → KMS/HSM/Vault) is a swap of THIS
module's internals; the ``secret_ref`` contract the rest of the system depends on does not move.

Key rotation rides on ``MultiFernet``: ``THEYGENT_SECRET_KEY`` may be a comma-separated list — the
FIRST key encrypts new/rotated material, ALL keys can decrypt, so an operator rotates by prepending
a fresh key and re-encrypting lazily. If the env is unset, an **ephemeral** key is generated and a
loud warning logged: secrets then work for the process lifetime but do NOT survive a restart and are
NOT shared across instances — fine for a dev box, never production (deny-by-default would break the
cockpit's create-a-connection flow, so this degrades loudly rather than refusing).
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from theygent_control_plane.models import SecretRow
from theygent_control_plane.run import now

logger = logging.getLogger("theygent.control_plane.secrets")

#: Env var carrying the Fernet key(s). Comma-separated for rotation: ``key1,key2`` encrypts with
#: ``key1`` and decrypts with either. Generate one with ``Fernet.generate_key().decode()``.
SECRET_KEY_ENV = "THEYGENT_SECRET_KEY"


def new_secret_ref() -> str:
    """A fresh ``secret_ref`` (``sec_<ulid>``) — the id of a ``secret`` row, what a connection
    points at. The ``sec_`` prefix makes the ref recognizable in logs/config (never the secret)."""
    return f"sec_{ULID()}"


def secret_keys_from_env() -> list[str] | None:
    """Resolve the Fernet key(s) from ``THEYGENT_SECRET_KEY`` (comma-separated for rotation —
    first encrypts, all decrypt). ``None`` when unset (:meth:`SecretStore.from_keys` then warns
    and uses an ephemeral key — dev-only). Shared by every process that opens the secret store
    (API and worker), so the two can never disagree about how keys are read."""
    raw = os.environ.get(SECRET_KEY_ENV)
    if not raw:
        return None
    return [k.strip() for k in raw.split(",") if k.strip()]


class SecretDecryptError(RuntimeError):
    """A stored ciphertext could not be decrypted with any configured key (a wrong/rotated-away key,
    or a corrupted row). Raised at resolve time, inside the step — surfaced as a tool ``err``
    binding, never a crash, so a misconfigured secret fails the call honestly, not the process."""


class SecretStore:
    """Postgres-backed encrypted secret storage (stateless ops, caller owns the
    session/transaction). The plaintext is encrypted on the way in and decrypted only at resolve
    time; the row stores a Fernet token, never the raw value. Construct via :meth:`from_keys` so the
    key material is resolved once at app startup."""

    def __init__(self, fernet: MultiFernet) -> None:
        self._fernet = fernet

    @classmethod
    def from_keys(cls, keys: list[str] | None) -> SecretStore:
        """Build from a list of Fernet keys (first encrypts, all decrypt). An empty/None list
        generates an EPHEMERAL key with a loud warning — dev-only (no restart/cross-instance
        survival). Production sets ``THEYGENT_SECRET_KEY``."""
        if keys:
            fernets = [Fernet(k.encode() if isinstance(k, str) else k) for k in keys]
        else:
            logger.warning(
                "secrets.ephemeral_key: %s unset — generating an in-process key. Stored secrets "
                "will NOT survive a restart and are NOT shared across instances. Set %s in "
                "production (a real KMS is the deferred upgrade).",
                SECRET_KEY_ENV,
                SECRET_KEY_ENV,
            )
            fernets = [Fernet(Fernet.generate_key())]
        return cls(MultiFernet(fernets))

    async def create(self, session: AsyncSession, plaintext: str) -> str:
        """Encrypt ``plaintext`` and store it under a fresh ``secret_ref`` — returns the ref. The
        plaintext is consumed here and never persisted; the caller (a connection route) holds only
        the ref afterwards."""
        ref = new_secret_ref()
        ts = now()
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        session.add(SecretRow(id=ref, ciphertext=token, created_at=ts, updated_at=ts))
        await session.flush()
        return ref

    async def set(self, session: AsyncSession, secret_ref: str, plaintext: str) -> None:
        """Rotate the material behind an existing ``secret_ref`` (re-encrypts with the primary key).
        Keeps the SAME ref so every connection — and every agent's ``contentHash`` — is unaffected
        by the rotation. Creates the row if absent."""
        row = await session.get(SecretRow, secret_ref)
        token = self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")
        if row is None:
            ts = now()
            session.add(SecretRow(id=secret_ref, ciphertext=token, created_at=ts, updated_at=ts))
        else:
            row.ciphertext = token
            row.updated_at = now()
        await session.flush()

    async def resolve(self, session: AsyncSession, secret_ref: str | None) -> str | None:
        """Decrypt the secret behind ``secret_ref`` — the ONLY place plaintext exists, called inside
        the step that uses it (server-side resolution only). ``None`` ref → ``None`` (an
        auth-less connection). A missing row → ``None`` (the connection lost its secret). A decrypt
        failure raises :class:`SecretDecryptError` so the caller binds an honest ``err``."""
        if secret_ref is None:
            return None
        row = await session.get(SecretRow, secret_ref)
        if row is None:
            return None
        try:
            return self._fernet.decrypt(row.ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise SecretDecryptError(
                f"secret {secret_ref!r} could not be decrypted (wrong or rotated-away key)"
            ) from exc

    async def delete(self, session: AsyncSession, secret_ref: str | None) -> None:
        """Delete the secret row (called when its owning connection is deleted, or its secret
        cleared). Idempotent — a missing row is a no-op."""
        if secret_ref is None:
            return
        row = await session.get(SecretRow, secret_ref)
        if row is not None:
            await session.delete(row)
            await session.flush()
