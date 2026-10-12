"""ai gateway shadow runs — measuring what a counterfactual only estimates

A shadow run sends a copy of a request, after the real exchange, to the target its decision named
but did not apply. A record carries the run (``shadow``: outcome, usage, cost, time), and the
per-day aggregates sum the runs, those whose answer the route could have used, what they cost,
and the output tokens the targets wrote against the requested models' for the same requests.
Existing rows had no shadow: every new count starts at zero.

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0024_ai_gateway_shadows"
down_revision: str | None = "0023_ai_gateway_router"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DAILY = (
    ("shadow_calls", sa.BigInteger()),
    ("shadow_answered_calls", sa.BigInteger()),
    ("shadow_cost_usd", sa.Float()),
    ("shadow_output_tokens", sa.BigInteger()),
    ("shadowed_output_tokens", sa.BigInteger()),
)


def upgrade() -> None:
    op.add_column("ai_gateway_request", sa.Column("shadow", JSONB(), nullable=True))
    for name, kind in _DAILY:
        op.add_column(
            "ai_gateway_daily_usage", sa.Column(name, kind, nullable=False, server_default="0")
        )
        op.alter_column("ai_gateway_daily_usage", name, server_default=None)


def downgrade() -> None:
    for name, _ in reversed(_DAILY):
        op.drop_column("ai_gateway_daily_usage", name)
    op.drop_column("ai_gateway_request", "shadow")
