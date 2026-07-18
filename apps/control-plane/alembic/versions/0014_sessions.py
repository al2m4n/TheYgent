"""sessions: rename thread → chat_session, message.run_id nullable.

Revision ID: 0014_sessions
Revises: 0013_mcp_http
Create Date: 2026-07-07

A conversation is a *session*, and sessions are client-writable
(a direct-to-inference chat persists its history without a run). Renames only — table
``thread`` → ``chat_session``, columns ``run.thread_id``/``message.thread_id`` →
``session_id``, index ``ix_message_thread_position`` → ``ix_message_session_position``
(still UNIQUE(session_id, position)) — plus one semantic change: ``message.run_id`` becomes
nullable (NULL = a client-appended turn with no run behind it). FK constraint names are left
as Postgres carries them through the renames (functional; no churn).

Hand-written + reversible. Downgrade restores ``run_id`` NOT NULL, which fails on
client-written rows — the standard downgrade caveat, accepted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_sessions"
down_revision: str | None = "0013_mcp_http"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("thread", "chat_session")
    op.alter_column("run", "thread_id", new_column_name="session_id")
    op.alter_column("message", "thread_id", new_column_name="session_id")
    op.alter_column("message", "run_id", existing_type=sa.String(), nullable=True)
    op.execute("ALTER INDEX ix_message_thread_position RENAME TO ix_message_session_position")


def downgrade() -> None:
    op.execute("ALTER INDEX ix_message_session_position RENAME TO ix_message_thread_position")
    op.alter_column("message", "run_id", existing_type=sa.String(), nullable=False)
    op.alter_column("message", "session_id", new_column_name="thread_id")
    op.alter_column("run", "session_id", new_column_name="thread_id")
    op.rename_table("chat_session", "thread")
