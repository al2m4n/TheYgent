"""agent registry: agent + agent_version

Revision ID: 0005_agent
Revises: 0004_mcp_server
Create Date: 2026-06-22

The first genuinely paid control-plane surface: turns "paste the whole IR every run"
into "an agent is a saved, named, versioned thing you invoke". Two tables, both plain
conventional rows (no new heavy deps):

* ``agent`` — the stable agent identity (``id``), constant across versions. ``owner_id`` /
  ``workspace_id`` are deliberately omitted: a scoping column slots in later WITHOUT a
  reshape for the Team-tier shared registry — single-user localhost until then.
* ``agent_version`` — immutable, content-addressed versions. The ``(agent_id, version)`` UNIQUE
  index is the immutability guard: one ``content_hash`` per coordinate, forever — the promise
  that pinned deploys depend on. ``ir`` is the canonical, view-stripped IR document; ``view`` is
  stored alongside but NEVER hashed. ``seq`` is the monotonic-per-agent ordering key — order
  by it, never by ULID/timestamp (clock skew across instances makes those unreliable).

This is the CONTROL-PLANE registry (stores IR + metadata, never inference data — plane boundary).
Model bindings inside the IR still resolve at the inference plane at run time; nothing here forces
inference data across the seam. Hand-written and fully reversible (the round-trip test exercises
upgrade head -> downgrade base, now including these).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_agent"
down_revision: str | None = "0004_mcp_server"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "agent",
        # The agent id (ULID), stable across versions — the IR document's own id.
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        # owner_id / workspace_id deliberately omitted now; column slots in later.
    )
    op.create_table(
        "agent_version",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        # The canonical, view-stripped IR document; the (never-hashed) view block alongside.
        sa.Column("ir", postgresql.JSONB(), nullable=False),
        sa.Column("view", postgresql.JSONB(), nullable=True),
        # Monotonic per agent; THE ordering key — not ULID/timestamp
        # (clock skew makes those unreliable).
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
    )
    # The immutability guard: one content per (agent, version), forever.
    op.create_index(
        "ix_agent_version_agent_version", "agent_version", ["agent_id", "version"], unique=True
    )
    # Order versions by seq, never by time/ULID.
    op.create_index("ix_agent_version_agent_seq", "agent_version", ["agent_id", "seq"])
    # Content-addressed lookup (enables pin-by-hash deploys).
    op.create_index("ix_agent_version_content_hash", "agent_version", ["content_hash"])


def downgrade() -> None:
    # Reverse dependency order (agent_version -> agent) for a clean round-trip.
    op.drop_index("ix_agent_version_content_hash", table_name="agent_version")
    op.drop_index("ix_agent_version_agent_seq", table_name="agent_version")
    op.drop_index("ix_agent_version_agent_version", table_name="agent_version")
    op.drop_table("agent_version")
    op.drop_table("agent")
