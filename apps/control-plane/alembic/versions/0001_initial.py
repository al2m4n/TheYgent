"""initial: thread, run, message

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-17

The control-plane's first tables. ULID string ids throughout (consistency with the earlier
Run.id). Ordering keys off explicit columns (message.position), never timestamps —
clock skew across horizontally-scaled instances makes those unreliable. Hand-written
and fully reversible; the round-trip test exercises upgrade head -> downgrade base.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "thread",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.Column("metadata", postgresql.JSONB(), nullable=True),
    )
    op.create_table(
        "run",
        sa.Column("id", sa.String(), primary_key=True),
        # NULL = one-shot run, no thread/memory.
        sa.Column("thread_id", sa.String(), sa.ForeignKey("thread.id"), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )
    op.create_table(
        "message",
        sa.Column("id", sa.String(), primary_key=True),
        # Denormalized thread_id for direct ordered thread reads.
        sa.Column("thread_id", sa.String(), sa.ForeignKey("thread.id"), nullable=False),
        sa.Column("run_id", sa.String(), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
    )
    # UNIQUE: positions are dense+monotonic per thread; also the concurrency safety net
    # behind append_turn's SELECT … FOR UPDATE (a colliding write fails loudly, never
    # silently loses a turn). See models.MessageRow.
    op.create_index("ix_message_thread_position", "message", ["thread_id", "position"], unique=True)


def downgrade() -> None:
    # Reverse dependency order (message -> run -> thread) for a clean round-trip.
    op.drop_index("ix_message_thread_position", table_name="message")
    op.drop_table("message")
    op.drop_table("run")
    op.drop_table("thread")
