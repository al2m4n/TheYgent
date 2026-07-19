"""identity — accounts, sessions, API keys, sign-in providers, and run/session ownership

The identity layer behind the three long-standing auth seams (`require_auth`,
`require_token`, `governance.authorize`). Five new tables plus two breadcrumb columns:

- ``user_account`` — accounts (``user`` is a reserved word in Postgres; the longer name keeps
  every raw-SQL helper quote-free). ``password_hash`` is a self-describing scrypt string and
  NULL for SSO-only accounts; ``role`` is the closed app-enforced set admin|editor|viewer.
- ``auth_identity`` — federated (provider, subject) links; CASCADE with the account.
- ``auth_session`` — interactive sign-ins; ``token_hash`` = sha256 of the opaque bearer, so a
  DB leak exposes no usable credential. Absolute expiry.
- ``api_key`` — programmatic bearers, hashed the same way; ``revoked_at`` soft-deletes so the
  row stays an audit breadcrumb; ``token_prefix`` is the only recoverable fragment.
- ``auth_provider`` — OIDC/OAuth2 sign-in config; the client secret lives behind
  ``client_secret_ref`` in the encrypted ``secret`` table (same discipline as connections).
- ``run.user_id`` / ``chat_session.user_id`` — plain-string ownership breadcrumbs (no FK:
  history must outlive the account; pre-auth rows stay NULL).

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_identity"
down_revision: str | None = "0017_platform_setting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "user_account",
        sa.Column("id", sa.String(), primary_key=True),  # usr_<ulid>
        sa.Column("username", sa.String(), nullable=False),  # lowercased at write time
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=False),  # admin | editor | viewer
        sa.Column("password_hash", sa.Text(), nullable=True),  # NULL = SSO-only account
        sa.Column("disabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.Column("last_login_at", _TZ, nullable=True),
    )
    op.create_index("uq_user_account_username", "user_account", ["username"], unique=True)

    op.create_table(
        "auth_identity",
        sa.Column("id", sa.String(), primary_key=True),  # idn_<ulid>
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Breadcrumb to auth_provider.id — stays meaningful after the provider is deleted.
        sa.Column("provider_id", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),  # the provider's stable sub claim
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("created_at", _TZ, nullable=False),
        sa.UniqueConstraint("provider_id", "subject", name="uq_auth_identity_provider_subject"),
    )
    op.create_index("ix_auth_identity_user", "auth_identity", ["user_id"])

    op.create_table(
        "auth_session",
        sa.Column("id", sa.String(), primary_key=True),  # ses_<ulid>
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_hash", sa.String(), nullable=False),  # sha256 hex, never the token
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("expires_at", _TZ, nullable=False),
        sa.Column("last_seen_at", _TZ, nullable=False),
    )
    op.create_index("uq_auth_session_token_hash", "auth_session", ["token_hash"], unique=True)
    op.create_index("ix_auth_session_user", "auth_session", ["user_id"])

    op.create_table(
        "api_key",
        sa.Column("id", sa.String(), primary_key=True),  # key_<ulid>
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user_account.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),  # ≤ owner's role at mint time
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("token_prefix", sa.String(), nullable=False),  # "tyk_ab12…" display fragment
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("last_used_at", _TZ, nullable=True),
        sa.Column("revoked_at", _TZ, nullable=True),  # soft delete (audit breadcrumb)
    )
    op.create_index("uq_api_key_token_hash", "api_key", ["token_hash"], unique=True)
    op.create_index("ix_api_key_user", "api_key", ["user_id"])

    op.create_table(
        "auth_provider",
        sa.Column("id", sa.String(), primary_key=True),  # idp_<ulid>
        sa.Column("name", sa.String(), nullable=False),  # sign-in button label
        sa.Column("slug", sa.String(), nullable=False),  # url-safe stable handle
        sa.Column("config", postgresql.JSONB(), nullable=False),  # non-secret config only
        sa.Column("client_secret_ref", sa.String(), nullable=True),  # → secret.id
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )
    op.create_index("uq_auth_provider_slug", "auth_provider", ["slug"], unique=True)

    # Ownership breadcrumbs — nullable, no FK (history outlives accounts; pre-auth rows NULL).
    op.add_column("run", sa.Column("user_id", sa.String(), nullable=True))
    op.add_column("chat_session", sa.Column("user_id", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_session", "user_id")
    op.drop_column("run", "user_id")
    op.drop_index("uq_auth_provider_slug", table_name="auth_provider")
    op.drop_table("auth_provider")
    op.drop_index("ix_api_key_user", table_name="api_key")
    op.drop_index("uq_api_key_token_hash", table_name="api_key")
    op.drop_table("api_key")
    op.drop_index("ix_auth_session_user", table_name="auth_session")
    op.drop_index("uq_auth_session_token_hash", table_name="auth_session")
    op.drop_table("auth_session")
    op.drop_index("ix_auth_identity_user", table_name="auth_identity")
    op.drop_table("auth_identity")
    op.drop_index("uq_user_account_username", table_name="user_account")
    op.drop_table("user_account")
