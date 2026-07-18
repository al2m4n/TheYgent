"""graph run fields on run

Revision ID: 0002_graph_run
Revises: 0001_initial
Create Date: 2026-06-18

An additive contract extension: three nullable columns on ``run`` so a
``/graphs/runs`` run records the IR's registry coordinate (``graph_id`` + ``graph_version``)
and its content-addressed identity (``content_hash``). NULL for a non-graph ``/runs`` run,
so the plain-run path is untouched. Recorded but not yet gated on — the registry consumes it.
Hand-written and fully reversible (the round-trip test covers it).
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
