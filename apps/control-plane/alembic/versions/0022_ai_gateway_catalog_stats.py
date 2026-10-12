"""ai gateway catalog — observation stats only; review status lives in the config

A fingerprint's review status (and label) is part of the gateway config: the versioned, immutable
document every record's decision was made under. Keeping a second copy on the catalog row would
let the two disagree, so the catalog keeps what was observed (first and last seen, count, client
versions, shape) and reads status from the latest config.

Hand-written and fully reversible: a downgrade restores the columns with every kind
``unreviewed``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022_ai_gateway_catalog_stats"
down_revision: str | None = "0021_ai_gateway_key_set"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.drop_column("ai_gateway_fingerprint", "reviewed_by")
    op.drop_column("ai_gateway_fingerprint", "reviewed_at")
    op.drop_column("ai_gateway_fingerprint", "label")
    op.drop_column("ai_gateway_fingerprint", "status")


def downgrade() -> None:
    op.add_column(
        "ai_gateway_fingerprint",
        sa.Column("status", sa.String(), nullable=False, server_default="unreviewed"),
    )
    op.alter_column("ai_gateway_fingerprint", "status", server_default=None)
    op.add_column("ai_gateway_fingerprint", sa.Column("label", sa.String(), nullable=True))
    op.add_column("ai_gateway_fingerprint", sa.Column("reviewed_at", _TZ, nullable=True))
    op.add_column("ai_gateway_fingerprint", sa.Column("reviewed_by", sa.String(), nullable=True))
