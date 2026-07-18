"""trigger registry + run.trigger_id + run.completed_at

Revision ID: 0006_trigger
Revises: 0005_agent
Create Date: 2026-06-23

Gives a saved agent stable, non-interactive entry points so it runs unattended — the minimum viable
meaning of "deploy". The hard-to-reverse seam is the trigger *definition* (what fires which saved,
pinned agent, on what condition); the dispatcher that reads it and fires runs is a swappable
in-process detail that a durable worker can replace WITHOUT reshaping this table. Schedules must
survive a restart, so definitions live here and rehydrate by being re-read on every dispatcher
tick.

* ``trigger`` — ``{id, agent_id, version|content_hash pin, kind ∈ http|schedule|webhook, config,
  enabled}``. Exactly one of ``version`` / ``content_hash`` is set (the app enforces it): an
  unattended deploy runs an IMMUTABLE artifact and never silently changes because a graph was
  edited. ``config`` (JSONB) holds the kind-specific knobs — a cron expression, a webhook signing
  secret, an optional input template. ``last_fired_at`` is dispatcher bookkeeping (NOT the frozen
  contract): the dispatcher computes due-ness from it so a restart resumes cleanly.
* ``run.trigger_id`` — additive: which trigger fired this run. NULL = interactive; populated for a
  schedule-/webhook-fired run, giving an unattended run lineage for the run list and debugging.
* ``run.completed_at`` — a real terminal timestamp. Paired with ``created_at`` it makes duration
  exact and turns every run into a ``[created_at, completed_at]`` INTERVAL, so system-wide
  concurrency (how many fired at once) becomes a sweep-line/overlap query the per-trigger
  ``last_fired_at`` could never answer. Stamped on a real-time terminal transition
  (``completed``/``failed``); LEFT NULL by the startup reconciliation sweep — a zombie from a
  dead process has an unknown end time, and ``now()`` there would be reconcile-time garbage that
  skews duration/cost stats. NULL ``completed_at`` on a ``failed`` run thus reads honestly as
  "crashed, end time unknown".

``owner_id`` / ``workspace_id`` are omitted: the per-workspace token scoping slots a
column in later WITHOUT a reshape — single-user localhost until then. Hand-written and fully
reversible (the round-trip test exercises upgrade head -> downgrade base).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_trigger"
down_revision: str | None = "0005_agent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "trigger",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agent.id"), nullable=False),
        # The version pin: exactly one of version / content_hash — enforced by the app.
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),  # http | schedule | webhook
        # kind-specific knobs: {"cron": …} | {"secret": …} | {"input": …}.
        sa.Column("config", postgresql.JSONB(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        # Dispatcher bookkeeping (NOT the frozen contract): the last instant a schedule fired.
        sa.Column("last_fired_at", _TZ, nullable=True),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        # owner_id / workspace_id omitted; a scoping column slots in later.
    )
    op.create_index("ix_trigger_agent", "trigger", ["agent_id"])
    # The run lineage column: which trigger fired this run (NULL = interactive). Added to the
    # existing run table — additive. A plain breadcrumb, NOT an enforced FK: a trigger can be
    # DELETEd while its historical runs keep recording what fired them (the lineage must outlive the
    # definition), so no referential constraint is imposed.
    op.add_column("run", sa.Column("trigger_id", sa.String(), nullable=True))
    # A real terminal-completion instant. Nullable —
    # set on a real-time terminal transition, left NULL for swept zombies and in-flight runs.
    op.add_column("run", sa.Column("completed_at", _TZ, nullable=True))


def downgrade() -> None:
    # Reverse add order: drop the run columns first, then the trigger table.
    op.drop_column("run", "completed_at")
    op.drop_column("run", "trigger_id")
    op.drop_index("ix_trigger_agent", table_name="trigger")
    op.drop_table("trigger")
