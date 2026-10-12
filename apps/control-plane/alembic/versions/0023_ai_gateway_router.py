"""ai gateway router mode — what a request would have cost without routing

Router mode serves some requests on another model than the one Claude Code asked for. A record
then carries ``baseline_usd`` (the cost on the requested model) beside ``cost_usd`` (what ran),
and the per-day aggregates sum it, with the number of routes actually applied, so a report can
show what routing saved. Existing rows ran on the requested model: their baseline is their cost.

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_ai_gateway_router"
down_revision: str | None = "0022_ai_gateway_catalog_stats"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ai_gateway_request", sa.Column("baseline_usd", sa.Float(), nullable=True))
    op.add_column(
        "ai_gateway_daily_usage",
        sa.Column("baseline_usd", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "ai_gateway_daily_usage",
        sa.Column("applied_calls", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.execute("UPDATE ai_gateway_daily_usage SET baseline_usd = cost_usd")
    op.alter_column("ai_gateway_daily_usage", "baseline_usd", server_default=None)
    op.alter_column("ai_gateway_daily_usage", "applied_calls", server_default=None)


def downgrade() -> None:
    op.drop_column("ai_gateway_daily_usage", "applied_calls")
    op.drop_column("ai_gateway_daily_usage", "baseline_usd")
    op.drop_column("ai_gateway_request", "baseline_usd")
