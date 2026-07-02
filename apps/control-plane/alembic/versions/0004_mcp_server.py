"""mcp server registry table

Revision ID: 0004_mcp_server
Revises: 0003_run_output
Create Date: 2026-06-22

Persist the control-plane's MCP server registrations so they survive a restart — previously they
lived only in the in-memory ``McpManager`` and were lost on every restart, an asymmetry with
runs/threads that already persist. A real table, an Alembic migration, domain/ORM split (the
manager's ``McpServerConfig`` stays the domain shape).

This is the CONTROL-PLANE registry (its own trust domain → its own Postgres). The INFERENCE-plane
model registry is deliberately NOT here — it persists locally to the inference plane (the plane
boundary). Do not add a model table to the control-plane DB.

``env`` carries the user's secrets/paths that the registration must round-trip to respawn the
server after a restart, so it is stored here (this control-plane's own store — distinct from
"transiting theygent cloud telemetry", which the plane boundary forbids); it is never *logged*
with its values (``_mcp_view`` lists keys only). The live process handle is never persisted — only
the registration; connections re-establish lazily on next use. Hand-written/reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_mcp_server"
down_revision: str | None = "0003_run_output"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "mcp_server",
        sa.Column("name", sa.String(), primary_key=True),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("command", sa.String(), nullable=False),
        # args/env are JSONB: a list of args and an opaque key→value secret map respectively.
        sa.Column("args", postgresql.JSONB(), nullable=False),
        sa.Column("env", postgresql.JSONB(), nullable=True),
        sa.Column("cwd", sa.String(), nullable=True),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("mcp_server")
