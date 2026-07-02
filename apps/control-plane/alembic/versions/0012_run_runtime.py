"""run.runtime — which runtime owns a run's lifecycle (interpreter vs durable).

The startup reconcile sweep exists to terminalize runs the in-process interpreter can never
finish after a restart. Durable runs are the opposite case: their workflow engine recovers and
resumes them after a crash, so sweeping them to ``failed`` would report a false terminal state
for a run that is about to complete (and, in the split API/worker topology, would fail runs a
healthy sibling process is actively executing). A nullable marker column keeps the sweep honest:
``'durable'`` rows are excluded; NULL (all pre-existing rows) and ``'inproc'`` stay swept.

Hand-written + reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_run_runtime"
down_revision: str | None = "0011_gate_counter"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("run", sa.Column("runtime", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("run", "runtime")
