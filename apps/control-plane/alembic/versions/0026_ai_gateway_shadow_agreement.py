"""ai gateway shadow agreement — how often a target picks the same next action

A shadow run of an agent turn now records whether the target picked the same next action as the
real answer (the same tool first, or an answer where it answered). The per-day aggregates sum the
shadows compared and those that agreed, so the report can say how far a model — a local one in
particular — can be trusted with agent work. Earlier shadows recorded no verdict: both counts start
at zero.

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026_ai_gateway_shadow_agreement"
down_revision: str | None = "0025_ai_gateway_retention"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = ("shadow_compared_calls", "shadow_agreed_calls")


def upgrade() -> None:
    for name in _COLUMNS:
        op.add_column(
            "ai_gateway_daily_usage",
            sa.Column(name, sa.BigInteger(), nullable=False, server_default="0"),
        )
        op.alter_column("ai_gateway_daily_usage", name, server_default=None)


def downgrade() -> None:
    for name in reversed(_COLUMNS):
        op.drop_column("ai_gateway_daily_usage", name)
