"""graph run fields on run (M5 §4)

Revision ID: 0002_graph_run
Revises: 0001_initial
Create Date: 2026-06-18

A deliberate, additive contract extension (m5.md §3.4): three nullable columns on ``run`` so a
``/graphs/runs`` run records the IR's registry coordinate (``graph_id`` + ``graph_version`` —
§8.2) and its content-addressed identity (``content_hash``). NULL for a non-graph ``/runs`` run,
so the M3/M4 path is untouched. Recorded now, not yet gated on (§3.3) — correct when the
registry consumes it. Hand-written and fully reversible (the §6 round-trip test still holds).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_graph_run"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("graph_id", sa.String(), nullable=True))
    op.add_column("run", sa.Column("graph_version", sa.String(), nullable=True))
    op.add_column("run", sa.Column("content_hash", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("run", "content_hash")
    op.drop_column("run", "graph_version")
    op.drop_column("run", "graph_id")
