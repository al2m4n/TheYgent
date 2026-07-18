"""run output column

Revision ID: 0003_run_output
Revises: 0002_graph_run
Create Date: 2026-06-22

An additive, nullable ``output`` column on ``run`` so a run's final accumulated output is durable
regardless of threading: ``GET /runs/{id}`` returns the output after the live SSE stream and the
tab are gone. Threaded runs persist the assistant turn to ``message``; this gives un-threaded runs
a durable home too, without changing the ``POST /runs`` / ``/graphs/runs`` wire contract (it is
additive read surface). NULL for runs that never reached a terminal output (e.g. failed
runs). Hand-written and reversible — the alembic round-trip test covers it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_run_output"
down_revision: str | None = "0002_graph_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("output", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("run", "output")
