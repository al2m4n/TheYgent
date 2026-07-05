"""run.awaiting_node for the durable human wait

Revision ID: 0007_human_waiting
Revises: 0006_trigger
Create Date: 2026-06-23

The additive-lowering node types (``human``/``subgraph``/``loop``/``map``) were introduced as
durable-runtime lowerings. Only the ``human`` node touches the persistence layer, and even that
touch is minimal:

* The ``waiting`` run status is **additive at the value level only** — ``run.status`` is a plain
  ``String`` column (no CHECK constraint / enum), so a new status value needs NO schema change. What
  matters for ``waiting`` is behavioral: the reconciliation sweep already touches only
  ``created``/``streaming``, so a ``waiting`` run is excluded from it for free.
* ``run.awaiting_node`` — the one new column. When a run pauses at a ``human`` node on
  ``DBOS.recv``, this records WHICH node it is paused at (the "run bookkeeping" the waiting status
  needs). ``POST /runs/{id}/resume`` reads it to validate the awaited input against that node's
  declared schema and to deliver it. NULL except while ``status == 'waiting'``. Additive and
  nullable, exactly like the earlier ``graph_id`` and ``trigger_id`` columns — a plain breadcrumb,
  not an FK.

Hand-written and fully reversible (the round-trip test exercises upgrade head → downgrade base,
now including this column).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_human_waiting"
down_revision: str | None = "0006_trigger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The waiting-node breadcrumb: which human node a `waiting` run is paused at. NULL when
    # the run is not waiting. The `waiting` status itself needs no migration — `run.status` is a
    # free String, so the new value is purely additive at the application layer.
    op.add_column("run", sa.Column("awaiting_node", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("run", "awaiting_node")
