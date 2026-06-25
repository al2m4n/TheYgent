"""gate_counter — the lean per-key ratelimit counter (M19 §2.8/§1.6)

M19 adds a ``gate`` seam (``ratelimit``/``quota``), NOT a metering/billing engine (§1.6).
``ratelimit`` is backed by this one trivial counter table: ``id`` is a composite scope+key+window
string, ``count`` the hits in the current fixed window (reset when it rolls). ``quota`` builds no
new table — it READS accumulated token usage off the M17 ``span`` rows (§1.6: read, not re-metered).

Per-key/per-caller only (per-USER is the deferred identity milestone — §1.6). No metering API.
Hand-written + reversible — the §6 round-trip test covers it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_gate_counter"
down_revision: str | None = "0010_connection"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "gate_counter",
        sa.Column("id", sa.String(), primary_key=True),  # scope:key:window_id
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("window_start", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("gate_counter")
