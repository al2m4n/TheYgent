"""Identity & access — accounts, roles, sessions, API keys, and OIDC sign-in.

This module fills the three seams the platform froze long before it had users:
``require_auth`` (app.py) resolves a bearer to a ``governance.Principal``
through :class:`AuthStore`; ``require_token`` accepts API keys minted here; and
``governance.authorize`` ranks the resolved role. Everything credential-shaped follows two rules:

- **Tokens are opaque and stored hashed.** A session bearer (``tys_…``) or API key (``tyk_…``)
  is high-entropy random material; the DB keeps only its sha256, so a database leak exposes no
  usable credential. The raw token exists exactly once — in the response that minted it.
- **Passwords are scrypt.** Stdlib ``hashlib.scrypt`` (memory-hard, zero new dependencies) in a
  self-describing ``scrypt$N$r$p$salt$hash`` string, so parameters can be raised later without a
  migration — old hashes verify under their recorded parameters.

Sign-in federation is **OIDC authorization-code + PKCE**, resolved server-side: the control plane
exchanges the code and reads the provider's ``userinfo`` endpoint over TLS (the issuer answering
an authenticated userinfo request is the authoritative identity source — no JWT/JWKS machinery to
misconfigure). Plain-OAuth2 providers without discovery (e.g. GitHub) work by configuring the
three endpoints explicitly. In-flight flow state (PKCE verifier, nonce, return URL) travels as a
Fernet-sealed, TTL-bound ``state`` parameter — stateless, so it survives replicas and restarts.

Roles are a closed, ordered set — ``viewer < editor < admin`` — and every widening is explicit:
an API key's role is capped at its owner's role when minted AND meet-narrowed at use time, so
demoting an account instantly narrows every credential hanging off it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets as pysecrets
from datetime import datetime, timedelta
from typing import Any, Literal, cast
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from pydantic import BaseModel
from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from theygent_control_plane.models import (
    ApiKeyRow,
    AuthIdentityRow,
    AuthProviderRow,
    AuthSessionRow,
    UserAccountRow,
)
from theygent_control_plane.run import now

logger = logging.getLogger("theygent.control_plane.auth")

# ── Roles ─────────────────────────────────────────────────────────────────────────────────────────

Role = Literal["viewer", "editor", "admin"]

#: The closed role set, weakest first. ``viewer`` uses agents (chat, run published agents, own
#: sessions); ``editor`` builds them (drafts, connections, MCP, RAG, triggers, bench); ``admin``
#: additionally owns the install (settings, users, sign-in providers, import/export).
ROLES: tuple[Role, ...] = ("viewer", "editor", "admin")

_ROLE_RANK: dict[str, int] = {"viewer": 0, "editor": 1, "admin": 2}


def role_at_least(role: str, minimum: Role) -> bool:
    """Whether ``role`` meets ``minimum`` in the viewer < editor < admin order. An unknown
    role ranks below everything — a corrupted row must fail closed, never open."""
    return _ROLE_RANK.get(role, -1) >= _ROLE_RANK[minimum]


def narrower_role(a: str, b: str) -> Role:
    """The meet (weaker) of two roles — what an API key actually exercises: min(key, owner).
    Unknown values collapse to ``viewer`` (fail closed)."""
    ranked = min(_ROLE_RANK.get(a, 0), _ROLE_RANK.get(b, 0))
    return ROLES[ranked]


# ── Password hashing (stdlib scrypt) ─────────────────────────────────────────────────────────────

# Interactive-login cost: 16 MiB memory-hard, ~50 ms. Parameters ride IN the hash string, so
# raising them later re-hashes lazily at next login — no migration, old hashes stay verifiable.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32

#: Login is refused below this — a speed bump against trivial passwords, not a policy engine.
MIN_PASSWORD_LENGTH = 8


def hash_password(password: str) -> str:
    """Hash a password into the self-describing ``scrypt$N$r$p$salt$hash`` string (base64url
    salt + digest). Fresh 16-byte salt per call."""
    salt = pysecrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    b64 = base64.urlsafe_b64encode
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${b64(salt).decode('ascii')}${b64(digest).decode('ascii')}"
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify against a stored ``scrypt$…`` string. Any malformed stored value
    is a clean ``False`` — a corrupted row must deny, never crash the login path."""
    try:
        scheme, n_s, r_s, p_s, salt_s, digest_s = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_s.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_s.encode("ascii"))
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_s),
            r=int(r_s),
            p=int(p_s),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


# A real hash verified for unknown usernames so login timing does not reveal whether an
# account exists. Built lazily — scrypt at import time would tax every process start.
_dummy_hash: str | None = None


def _dummy_verify(password: str) -> None:
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(pysecrets.token_urlsafe(16))
    verify_password(password, _dummy_hash)


# ── Opaque bearer tokens ─────────────────────────────────────────────────────────────────────────

#: Interactive session bearer prefix ("theygent session").
SESSION_TOKEN_PREFIX = "tys_"
#: API-key bearer prefix ("theygent key").
API_KEY_TOKEN_PREFIX = "tyk_"


def new_session_token() -> str:
    return SESSION_TOKEN_PREFIX + pysecrets.token_urlsafe(32)


def new_api_key_token() -> str:
    return API_KEY_TOKEN_PREFIX + pysecrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    """sha256 hex of a bearer — what the DB stores and lookups key on. Plain sha256 (no salt/KDF)
    is correct here: the input is 256-bit random material, not a guessable password, and the
    digest must be a deterministic index key."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── Domain entities (wire/logic shapes — never ORM rows) ─────────────────────────────────────────


class User(BaseModel):
    """An account as the API returns it. Deliberately hash-free — the password hash never
    leaves the store layer, so no serializer can leak it."""

    id: str
    username: str
    display_name: str
    email: str | None = None
    role: Role
    disabled: bool = False
    # Whether a password login exists (False = SSO-only account). Derived, never the hash.
    has_password: bool = False
    # An avatar image URL from a sign-in provider (None = local account → the UI shows initials).
    avatar_url: str | None = None
    created_at: datetime
    updated_at: datetime
    last_login_at: datetime | None = None


class ApiKey(BaseModel):
    """A programmatic credential's public shape. The raw token is returned exactly once, by the
    minting endpoint; afterwards only ``token_prefix`` identifies it."""

    id: str
    user_id: str
    name: str
    role: Role
    token_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None


class AuthProvider(BaseModel):
    """A sign-in provider's public shape. ``config`` is the non-secret envelope
    (issuer/endpoints/scopes/provisioning policy); the client secret surfaces only as
    ``has_client_secret``."""

    id: str
    name: str
    slug: str
    config: dict[str, Any]
    enabled: bool = True
    has_client_secret: bool = False
    created_at: datetime
    updated_at: datetime


def _to_user(row: UserAccountRow) -> User:
    return User(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        email=row.email,
        # DB column is untyped str; fail closed to viewer on a corrupt row.
        role=cast(Role, row.role) if row.role in _ROLE_RANK else "viewer",
        disabled=row.disabled,
        has_password=row.password_hash is not None,
        avatar_url=row.avatar_url,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_login_at=row.last_login_at,
    )


def _to_api_key(row: ApiKeyRow) -> ApiKey:
    return ApiKey(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        role=cast(Role, row.role) if row.role in _ROLE_RANK else "viewer",
        token_prefix=row.token_prefix,
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


def _to_provider(row: AuthProviderRow) -> AuthProvider:
    return AuthProvider(
        id=row.id,
        name=row.name,
        slug=row.slug,
        config=dict(row.config or {}),
        enabled=row.enabled,
        has_client_secret=row.client_secret_ref is not None,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ── Validation helpers ───────────────────────────────────────────────────────────────────────────

# Usernames are lowercase handles, not display names (those are free text): letters/digits with
# ._- separators, 2-64 chars, must start alphanumeric.
_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
# Provider slugs are URL path fragments (/auth/oidc/{slug}/start).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def normalize_username(raw: str) -> str | None:
    """Lowercase + validate a username; ``None`` when unusable. Case-folding at every write and
    lookup is what makes logins case-insensitive without citext."""
    candidate = raw.strip().lower()
    return candidate if _USERNAME_RE.fullmatch(candidate) else None


def normalize_slug(raw: str) -> str | None:
    """Lowercase + validate a provider slug; ``None`` when unusable as a URL fragment."""
    candidate = raw.strip().lower()
    return candidate if _SLUG_RE.fullmatch(candidate) else None


def validate_provider_config(
    raw: dict[str, Any],
) -> dict[str, Any]:
    """Normalize the non-secret provider config, raising ``ValueError`` (message is
    user-facing) on anything unusable. Either ``issuer_url`` (OIDC discovery) or all three
    explicit endpoints must be present — explicit endpoints also override discovery, which is
    how plain-OAuth2 providers without a discovery document are configured."""
    config: dict[str, Any] = {}
    for key in ("issuer_url", "authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
        value = raw.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            raise ValueError(f"{key} must be an http(s) URL")
        config[key] = value.rstrip("/")
    has_explicit = all(
        k in config for k in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint")
    )
    if "issuer_url" not in config and not has_explicit:
        raise ValueError(
            "provide issuer_url (OIDC discovery) or all of authorization_endpoint, "
            "token_endpoint and userinfo_endpoint"
        )
    client_id = raw.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise ValueError("client_id is required")
    config["client_id"] = client_id.strip()
    scopes = raw.get("scopes", "openid profile email")
    if not isinstance(scopes, str) or not scopes.strip():
        raise ValueError("scopes must be a non-empty string")
    config["scopes"] = " ".join(scopes.split())
    auto = raw.get("auto_provision", True)
    if not isinstance(auto, bool):
        raise ValueError("auto_provision must be a boolean")
    config["auto_provision"] = auto
    default_role = raw.get("default_role", "viewer")
    if default_role not in _ROLE_RANK:
        raise ValueError("default_role must be one of viewer, editor, admin")
    config["default_role"] = default_role
    allow_list = raw.get("allowed_domains")
    if allow_list is not None:
        if not isinstance(allow_list, list) or not all(
            isinstance(d, str) and d.strip() for d in allow_list
        ):
            raise ValueError("allowed_domains must be a list of email or domain strings")
        # Each entry is a full email (exact-address allow) or a bare domain (whole-org allow).
        # Lowercase; a leading '@gmail.com' shorthand normalizes to the 'gmail.com' domain.
        config["allowed_domains"] = [d.strip().lower().lstrip("@") for d in allow_list]
    return config


# ── The store ────────────────────────────────────────────────────────────────────────────────────

# last_seen/last_used are telemetry, throttled to one write per interval so hot request paths
# do not turn every read into a row update.
_TOUCH_INTERVAL = timedelta(minutes=5)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


class AuthStore:
    """Postgres-backed identity storage — stateless ops, caller owns the session/transaction
    (house store discipline: methods flush, never commit)."""

    # -- users --

    async def count_users(self, session: AsyncSession) -> int:
        result = await session.execute(select(func.count()).select_from(UserAccountRow))
        return int(result.scalar_one())

    async def count_active_admins(
        self, session: AsyncSession, *, excluding: str | None = None
    ) -> int:
        """Active (not disabled) admins, optionally excluding one account — the guard input for
        every "would this strand the install without an admin?" check."""
        stmt = (
            select(func.count())
            .select_from(UserAccountRow)
            .where(UserAccountRow.role == "admin", UserAccountRow.disabled.is_(False))
        )
        if excluding is not None:
            stmt = stmt.where(UserAccountRow.id != excluding)
        result = await session.execute(stmt)
        return int(result.scalar_one())

    async def get_user(self, session: AsyncSession, user_id: str) -> User | None:
        row = await session.get(UserAccountRow, user_id)
        return _to_user(row) if row is not None else None

    async def get_user_by_username(self, session: AsyncSession, username: str) -> User | None:
        row = await self._user_row_by_username(session, username)
        return _to_user(row) if row is not None else None

    async def get_user_by_email(self, session: AsyncSession, email: str) -> User | None:
        """Exact (lowercased) email match — the link-by-email input for pre-provisioned
        SSO accounts. Returns the OLDEST match if duplicates exist (email is not unique)."""
        result = await session.execute(
            select(UserAccountRow)
            .where(UserAccountRow.email == email.strip().lower())
            .order_by(UserAccountRow.created_at, UserAccountRow.id)
        )
        row = result.scalars().first()
        return _to_user(row) if row is not None else None

    async def _user_row_by_username(
        self, session: AsyncSession, username: str
    ) -> UserAccountRow | None:
        result = await session.execute(
            select(UserAccountRow).where(UserAccountRow.username == username.strip().lower())
        )
        return result.scalar_one_or_none()

    async def list_users(self, session: AsyncSession) -> list[User]:
        # No pagination: this is the install's staff list, not an unbounded feed.
        result = await session.execute(
            select(UserAccountRow).order_by(UserAccountRow.created_at, UserAccountRow.id)
        )
        return [_to_user(row) for row in result.scalars()]

    async def create_user(
        self,
        session: AsyncSession,
        *,
        username: str,
        display_name: str,
        role: Role,
        password_hash: str | None,
        email: str | None = None,
    ) -> User:
        """Insert an account. The caller pre-checks username availability for a friendly 409;
        the unique index stays the race-proof backstop."""
        ts = now()
        row = UserAccountRow(
            id=_new_id("usr"),
            username=username.strip().lower(),
            display_name=display_name.strip() or username,
            email=email,
            role=role,
            password_hash=password_hash,
            disabled=False,
            created_at=ts,
            updated_at=ts,
            last_login_at=None,
        )
        session.add(row)
        await session.flush()
        return _to_user(row)

    async def update_user(
        self,
        session: AsyncSession,
        user_id: str,
        *,
        display_name: str | None = None,
        email: str | None = None,
        role: Role | None = None,
        disabled: bool | None = None,
    ) -> User | None:
        row = await session.get(UserAccountRow, user_id)
        if row is None:
            return None
        if display_name is not None and display_name.strip():
            row.display_name = display_name.strip()
        if email is not None:
            # Lowercase like every other email write path (create + OIDC provisioning) so the
            # case-insensitive get_user_by_email link lookup keeps matching this account.
            row.email = email.strip().lower() or None
        if role is not None:
            row.role = role
        if disabled is not None:
            row.disabled = disabled
        row.updated_at = now()
        await session.flush()
        return _to_user(row)

    async def set_password(self, session: AsyncSession, user_id: str, password_hash: str) -> bool:
        row = await session.get(UserAccountRow, user_id)
        if row is None:
            return False
        row.password_hash = password_hash
        row.updated_at = now()
        await session.flush()
        return True

    async def set_avatar_url(self, session: AsyncSession, user_id: str, url: str | None) -> None:
        """Refresh the account's avatar from a sign-in provider. A no-op when unchanged (so a
        returning-user sign-in doesn't churn ``updated_at``). ``None`` is ignored, never a clear —
        a provider that omits the picture on one sign-in must not wipe a previously-set one."""
        if url is None:
            return
        row = await session.get(UserAccountRow, user_id)
        if row is not None and row.avatar_url != url:
            row.avatar_url = url
            row.updated_at = now()
            await session.flush()

    async def delete_user(self, session: AsyncSession, user_id: str) -> bool:
        """Delete an account; identities/sessions/API keys go with it (FK CASCADE). Runs and
        chat sessions keep their ``user_id`` breadcrumb — history outlives the account."""
        row = await session.get(UserAccountRow, user_id)
        if row is None:
            return False
        await session.delete(row)
        await session.flush()
        return True

    async def verify_login(
        self, session: AsyncSession, username: str, password: str
    ) -> User | None:
        """Resolve username+password to a user, or ``None`` — one answer for unknown user, wrong
        password and password-less (SSO-only) account, with a dummy scrypt verify on the miss
        paths so response timing does not reveal which it was. The caller still checks
        ``disabled`` (a correct password on a disabled account is its own, honest error)."""
        row = await self._user_row_by_username(session, username)
        if row is None or row.password_hash is None:
            _dummy_verify(password)
            return None
        if not verify_password(password, row.password_hash):
            return None
        return _to_user(row)

    async def touch_last_login(self, session: AsyncSession, user_id: str) -> None:
        row = await session.get(UserAccountRow, user_id)
        if row is not None:
            row.last_login_at = now()
            await session.flush()

    # -- federated identities --

    async def find_identity_user(
        self, session: AsyncSession, provider_id: str, subject: str
    ) -> User | None:
        result = await session.execute(
            select(UserAccountRow)
            .join(AuthIdentityRow, AuthIdentityRow.user_id == UserAccountRow.id)
            .where(
                AuthIdentityRow.provider_id == provider_id,
                AuthIdentityRow.subject == subject,
            )
        )
        row = result.scalar_one_or_none()
        return _to_user(row) if row is not None else None

    async def count_identities(self, session: AsyncSession, user_id: str) -> int:
        """How many federated identities are already linked to an account. Zero means the
        account was created but never signed in via a provider — the ONLY state in which
        email-linking a fresh provider sign-in to it is safe (an admin pre-provisioned it).
        A non-zero count means an active SSO account, which must never be claimable by a second
        provider asserting the same email."""
        result = await session.execute(
            select(func.count())
            .select_from(AuthIdentityRow)
            .where(AuthIdentityRow.user_id == user_id)
        )
        return int(result.scalar_one())

    async def link_identity(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        provider_id: str,
        subject: str,
        email: str | None,
    ) -> None:
        session.add(
            AuthIdentityRow(
                id=_new_id("idn"),
                user_id=user_id,
                provider_id=provider_id,
                subject=subject,
                email=email,
                created_at=now(),
            )
        )
        await session.flush()

    # -- interactive sessions --

    async def create_session(
        self, session: AsyncSession, user_id: str, *, ttl_hours: int
    ) -> tuple[str, str]:
        """Mint a sign-in: returns ``(raw bearer, session id)``. The raw token is never stored
        and never reconstructable — hand it to the caller now or lose it."""
        token = new_session_token()
        ts = now()
        row = AuthSessionRow(
            id=_new_id("ses"),
            user_id=user_id,
            token_hash=token_digest(token),
            created_at=ts,
            expires_at=ts + timedelta(hours=ttl_hours),
            last_seen_at=ts,
        )
        session.add(row)
        await session.flush()
        return token, row.id

    async def resolve_session(self, session: AsyncSession, token: str) -> User | None:
        """Resolve a ``tys_…`` bearer to its (active, unexpired, enabled) user; ``None``
        otherwise. Refreshes ``last_seen_at`` at most once per throttle interval."""
        ts = now()
        result = await session.execute(
            select(AuthSessionRow, UserAccountRow)
            .join(UserAccountRow, UserAccountRow.id == AuthSessionRow.user_id)
            .where(AuthSessionRow.token_hash == token_digest(token))
        )
        pair = result.first()
        if pair is None:
            return None
        sess_row, user_row = pair
        if sess_row.expires_at <= ts or user_row.disabled:
            return None
        if ts - sess_row.last_seen_at > _TOUCH_INTERVAL:
            sess_row.last_seen_at = ts
            await session.flush()
        return _to_user(user_row)

    async def revoke_session(self, session: AsyncSession, token: str) -> bool:
        result = await session.execute(
            delete(AuthSessionRow).where(AuthSessionRow.token_hash == token_digest(token))
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def revoke_user_sessions(
        self, session: AsyncSession, user_id: str, *, keep_token: str | None = None
    ) -> int:
        """Sign an account out everywhere — after a password change or an admin disable.
        ``keep_token`` preserves the session performing the change."""
        stmt = delete(AuthSessionRow).where(AuthSessionRow.user_id == user_id)
        if keep_token is not None:
            stmt = stmt.where(AuthSessionRow.token_hash != token_digest(keep_token))
        result = await session.execute(stmt)
        return int(cast("CursorResult[Any]", result).rowcount or 0)

    # -- API keys --

    async def create_api_key(
        self, session: AsyncSession, *, user_id: str, name: str, role: Role
    ) -> tuple[str, ApiKey]:
        """Mint a programmatic bearer for ``user_id`` at ``role`` (caller enforces the ≤-owner
        cap). Returns ``(raw token, public shape)`` — the raw token is shown once."""
        token = new_api_key_token()
        row = ApiKeyRow(
            id=_new_id("key"),
            user_id=user_id,
            name=name.strip() or "api key",
            role=role,
            token_hash=token_digest(token),
            token_prefix=token[:12],
            created_at=now(),
            last_used_at=None,
            revoked_at=None,
        )
        session.add(row)
        await session.flush()
        return token, _to_api_key(row)

    async def list_api_keys(self, session: AsyncSession, user_id: str) -> list[ApiKey]:
        result = await session.execute(
            select(ApiKeyRow)
            .where(ApiKeyRow.user_id == user_id)
            .order_by(ApiKeyRow.created_at.desc(), ApiKeyRow.id.desc())
        )
        return [_to_api_key(row) for row in result.scalars()]

    async def get_api_key(self, session: AsyncSession, key_id: str) -> ApiKey | None:
        row = await session.get(ApiKeyRow, key_id)
        return _to_api_key(row) if row is not None else None

    async def revoke_api_key(self, session: AsyncSession, key_id: str) -> bool:
        """Soft-revoke (the row stays as an audit breadcrumb). Idempotent."""
        row = await session.get(ApiKeyRow, key_id)
        if row is None:
            return False
        if row.revoked_at is None:
            row.revoked_at = now()
            await session.flush()
        return True

    async def resolve_api_key(self, session: AsyncSession, token: str) -> tuple[User, Role] | None:
        """Resolve a ``tyk_…`` bearer to ``(owner, effective role)`` — the meet of the key's
        role and the owner's CURRENT role, so a demotion needs no key rotation to bite.
        ``None`` for unknown/revoked keys and disabled owners."""
        result = await session.execute(
            select(ApiKeyRow, UserAccountRow)
            .join(UserAccountRow, UserAccountRow.id == ApiKeyRow.user_id)
            .where(ApiKeyRow.token_hash == token_digest(token))
        )
        pair = result.first()
        if pair is None:
            return None
        key_row, user_row = pair
        if key_row.revoked_at is not None or user_row.disabled:
            return None
        ts = now()
        if key_row.last_used_at is None or ts - key_row.last_used_at > _TOUCH_INTERVAL:
            key_row.last_used_at = ts
            await session.flush()
        return _to_user(user_row), narrower_role(key_row.role, user_row.role)

    # -- sign-in providers --

    async def list_providers(
        self, session: AsyncSession, *, enabled_only: bool = False
    ) -> list[AuthProvider]:
        stmt = select(AuthProviderRow).order_by(AuthProviderRow.created_at, AuthProviderRow.id)
        if enabled_only:
            stmt = stmt.where(AuthProviderRow.enabled.is_(True))
        result = await session.execute(stmt)
        return [_to_provider(row) for row in result.scalars()]

    async def get_provider(self, session: AsyncSession, provider_id: str) -> AuthProvider | None:
        row = await session.get(AuthProviderRow, provider_id)
        return _to_provider(row) if row is not None else None

    async def get_provider_by_slug(self, session: AsyncSession, slug: str) -> AuthProvider | None:
        result = await session.execute(
            select(AuthProviderRow).where(AuthProviderRow.slug == slug.strip().lower())
        )
        row = result.scalar_one_or_none()
        return _to_provider(row) if row is not None else None

    async def provider_secret_ref(self, session: AsyncSession, provider_id: str) -> str | None:
        row = await session.get(AuthProviderRow, provider_id)
        return row.client_secret_ref if row is not None else None

    async def create_provider(
        self,
        session: AsyncSession,
        *,
        name: str,
        slug: str,
        config: dict[str, Any],
        client_secret_ref: str | None,
        enabled: bool = True,
    ) -> AuthProvider:
        ts = now()
        row = AuthProviderRow(
            id=_new_id("idp"),
            name=name.strip(),
            slug=slug.strip().lower(),
            config=config,
            client_secret_ref=client_secret_ref,
            enabled=enabled,
            created_at=ts,
            updated_at=ts,
        )
        session.add(row)
        await session.flush()
        return _to_provider(row)

    async def update_provider(
        self,
        session: AsyncSession,
        provider_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        enabled: bool | None = None,
        client_secret_ref: str | None = None,
        set_client_secret_ref: bool = False,
    ) -> AuthProvider | None:
        row = await session.get(AuthProviderRow, provider_id)
        if row is None:
            return None
        if name is not None and name.strip():
            row.name = name.strip()
        if config is not None:
            row.config = config
        if enabled is not None:
            row.enabled = enabled
        if set_client_secret_ref:
            row.client_secret_ref = client_secret_ref
        row.updated_at = now()
        await session.flush()
        return _to_provider(row)

    async def delete_provider(self, session: AsyncSession, provider_id: str) -> str | None:
        """Delete a provider config, returning its ``client_secret_ref`` (or ``None``) so the
        caller can drop the secret row in the same transaction. Existing linked identities keep
        their breadcrumb ``provider_id`` and simply stop resolving — accounts survive."""
        row = await session.get(AuthProviderRow, provider_id)
        if row is None:
            return None
        ref = row.client_secret_ref
        await session.delete(row)
        await session.flush()
        return ref


# ── Sealed OIDC flow state ───────────────────────────────────────────────────────────────────────


class StateSealer:
    """Fernet-sealed, TTL-bound carrier for in-flight OIDC state (PKCE verifier, nonce, return
    URL). Stateless by design: the ``state`` query parameter IS the storage, so the flow
    survives restarts and lands on any replica. Keys come from ``THEYGENT_SECRET_KEY`` (same
    rotation semantics as the secret store); unset falls back to an ephemeral in-process key —
    fine on a dev box, sign-ins just break across a restart mid-flow."""

    def __init__(self, keys: list[str] | None) -> None:
        if keys:
            self._fernet = MultiFernet([Fernet(k.encode("ascii")) for k in keys])
        else:
            self._fernet = MultiFernet([Fernet(Fernet.generate_key())])

    def seal(self, payload: dict[str, Any]) -> str:
        import json

        return self._fernet.encrypt(json.dumps(payload, separators=(",", ":")).encode()).decode(
            "ascii"
        )

    def unseal(self, token: str, *, max_age_s: int) -> dict[str, Any] | None:
        """Decrypt + age-check; ``None`` for anything invalid, tampered or expired (one
        indistinguishable outcome — the state is attacker-supplied)."""
        import json

        try:
            raw = self._fernet.decrypt(token.encode("ascii"), ttl=max_age_s)
            payload = json.loads(raw)
        except (InvalidToken, ValueError, UnicodeDecodeError):
            return None
        return payload if isinstance(payload, dict) else None


# ── OIDC / OAuth2 code flow (server side) ────────────────────────────────────────────────────────


class OidcError(RuntimeError):
    """A sign-in flow failed at the provider boundary (discovery, exchange, or userinfo). The
    message is operator-facing; the browser only ever sees a stable error code."""


_HTTP_TIMEOUT = httpx.Timeout(10.0)


async def discover_endpoints(config: dict[str, Any]) -> dict[str, str]:
    """Resolve the three flow endpoints. Explicit config wins; anything missing comes from the
    issuer's ``/.well-known/openid-configuration``. Raises :class:`OidcError` when the set
    cannot be completed — a half-configured provider must fail loudly at sign-in, not guess."""
    endpoints = {
        k: config[k]
        for k in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint")
        if config.get(k)
    }
    if len(endpoints) < 3:
        issuer = config.get("issuer_url")
        if not issuer:
            raise OidcError("provider config lacks endpoints and an issuer_url")
        url = f"{issuer.rstrip('/')}/.well-known/openid-configuration"
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
                resp = await http.get(url)
                resp.raise_for_status()
                document = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(f"OIDC discovery failed for {issuer}: {exc}") from exc
        for key in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint"):
            if key not in endpoints:
                value = document.get(key)
                if isinstance(value, str) and value.startswith(("http://", "https://")):
                    endpoints[key] = value
    missing = [
        k
        for k in ("authorization_endpoint", "token_endpoint", "userinfo_endpoint")
        if k not in endpoints
    ]
    if missing:
        raise OidcError(f"provider is missing endpoints: {', '.join(missing)}")
    return endpoints


def new_state_binding() -> str:
    """A random value tying an in-flight OIDC ``state`` to the browser that started it. Sealed
    into the state AND set as a cookie on leg 1; leg 2 requires the two to match, so an attacker
    who obtained a valid (state, code) from their OWN flow cannot replay it into a victim's
    browser (login CSRF / session fixation) — the victim's browser carries no matching cookie."""
    return pysecrets.token_urlsafe(24)


def new_pkce_pair() -> tuple[str, str]:
    """(code_verifier, S256 code_challenge) per RFC 7636."""
    verifier = pysecrets.token_urlsafe(48)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def build_authorize_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    scopes: str,
    state: str,
    nonce: str,
    code_challenge: str,
) -> str:
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
    )
    separator = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{separator}{query}"


async def exchange_code(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    client_id: str,
    client_secret: str | None,
    code_verifier: str,
) -> str:
    """Exchange the authorization code for an access token (server-side, over TLS). Returns the
    access token; everything else in the response is deliberately unused — identity comes from
    the userinfo endpoint, not from parsing an id_token locally."""
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        data["client_secret"] = client_secret
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
            # Accept: application/json — some plain-OAuth2 providers (GitHub) default to
            # form-encoded responses without it.
            resp = await http.post(
                token_endpoint, data=data, headers={"Accept": "application/json"}
            )
            resp.raise_for_status()
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"token exchange failed: {exc}") from exc
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise OidcError("token endpoint returned no access_token")
    return token


async def fetch_userinfo(*, userinfo_endpoint: str, access_token: str) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as http:
            resp = await http.get(
                userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            resp.raise_for_status()
            claims = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise OidcError(f"userinfo request failed: {exc}") from exc
    if not isinstance(claims, dict):
        raise OidcError("userinfo endpoint returned a non-object body")
    return claims


def extract_identity(claims: dict[str, Any]) -> tuple[str, str | None, str, str]:
    """Map provider claims to ``(subject, email, display_name, username_hint)``, shaped to what
    real servers return: OIDC providers send ``sub``/``email``/``name``; GitHub's plain-OAuth2
    userinfo sends ``id``/``login``. Raises :class:`OidcError` when no stable subject exists."""
    subject = claims.get("sub") or claims.get("id")
    if subject is None or str(subject) == "":
        raise OidcError("userinfo claims carry no stable subject (sub/id)")
    subject = str(subject)
    email_raw = claims.get("email")
    email = email_raw.strip().lower() if isinstance(email_raw, str) and email_raw.strip() else None
    display = next(
        (
            str(claims[k]).strip()
            for k in ("name", "preferred_username", "login", "nickname")
            if isinstance(claims.get(k), str) and str(claims[k]).strip()
        ),
        email or f"user {subject[:8]}",
    )
    username_hint = next(
        (
            str(claims[k]).strip()
            for k in ("preferred_username", "login", "nickname")
            if isinstance(claims.get(k), str) and str(claims[k]).strip()
        ),
        (email.split("@", 1)[0] if email else subject),
    )
    return subject, email, display, username_hint


def picture_url(claims: dict[str, Any]) -> str | None:
    """The avatar URL from userinfo, or ``None``. OIDC providers send ``picture`` (Google, Okta,
    Entra); GitHub's plain-OAuth2 userinfo sends ``avatar_url``. Only an http(s) URL is accepted —
    a value with any other scheme is dropped so nothing but a fetchable image URL is ever stored
    or rendered (an ``<img src>`` must never carry e.g. a ``javascript:`` payload)."""
    raw = claims.get("picture") or claims.get("avatar_url")
    if isinstance(raw, str) and raw.strip().startswith(("http://", "https://")):
        return raw.strip()
    return None


def email_verified(claims: dict[str, Any]) -> bool:
    """Whether the provider AFFIRMATIVELY verified the email. Fail closed: an absent claim or a
    non-boolean value is NOT verified — the plain-OAuth2 userinfo path (GitHub-style) omits
    ``email_verified`` entirely, so trusting absence would let an unverified email drive account
    linking. Only a true boolean, or the string forms real providers emit, count."""
    value = claims.get("email_verified")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return False


def email_allowed(email: str | None, allow_list: list[str] | None) -> bool:
    """Whether an OIDC email passes the provider's sign-in allowlist. Each entry is either a
    FULL EMAIL (contains ``@`` — an exact-address allow, e.g. one person on a personal instance)
    or a BARE DOMAIN (no ``@`` — a whole-organization allow, e.g. ``acme.com``). An empty list
    allows any email; a non-empty list with no email denies (the list exists precisely to gate).
    Compared lowercased. This is why listing a domain lets EVERYONE at that domain in — list the
    specific address instead to allow just yourself."""
    if not allow_list:
        return True
    if not email or "@" not in email:
        return False
    email = email.strip().lower()
    domain = email.rsplit("@", 1)[1]
    return any(email == entry if "@" in entry else domain == entry for entry in allow_list)
