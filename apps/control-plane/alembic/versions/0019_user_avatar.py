"""user_avatar — the sign-in provider's avatar URL on an account

A nullable ``avatar_url`` column on ``user_account``: the OIDC ``picture`` claim from a
provider's userinfo, refreshed on each SSO sign-in. NULL for a local (password) account — the
UI falls back to initials. A plain http(s) CDN URL, never a secret.

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019_user_avatar"
down_revision: str | None = "0018_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("user_account", sa.Column("avatar_url", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("user_account", "avatar_url")
