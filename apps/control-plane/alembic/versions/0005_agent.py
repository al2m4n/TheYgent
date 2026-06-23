"""agent registry: agent + agent_version (M11 §2)

Revision ID: 0005_agent
Revises: 0004_mcp_server
Create Date: 2026-06-22

The first genuinely paid control-plane surface (plan §5): turns "paste the whole IR every run"
into "an agent is a saved, named, versioned thing you invoke" (M11 §0). Two tables, both plain
M4-convention rows (no new heavy deps — M11 §9):

* ``agent`` — the stable §8.2 identity (``id``), constant across versions. ``owner_id`` /
  ``workspace_id`` are deliberately omitted (M11 §1.3): a scoping column slots in later WITHOUT a
  reshape for the Team-tier shared registry — single-user localhost until then.
* ``agent_version`` — immutable, content-addressed versions. The ``(agent_id, version)`` UNIQUE
  index is the immutability guard (§1.2): one ``content_hash`` per coordinate, forever — the §8.2
  promise M12's deploys depend on. ``ir`` is the canonical, view-stripped IR document; ``view`` is
  stored alongside but NEVER hashed (§1.2). ``seq`` is the monotonic-per-agent ordering key — order
  by it, never by ULID/timestamp (M4 §3: clock skew across instances makes those unreliable).

This is the CONTROL-PLANE registry (stores IR + metadata, never inference data — M11 §1.4 / the
plane boundary, theygent-stack.md §10). Model bindings inside the IR still resolve at the inference
plane at run time; nothing here forces inference data across the seam. Hand-written and fully
reversible (the §6 round-trip test exercises upgrade head -> downgrade base, now including these).
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
        # The §8.2 agent id (ULID), stable across versions — the IR document's own id (§1.1).
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        # owner_id / workspace_id deliberately omitted now; column slots in later (§1.3).
    )
    op.create_table(
        "agent_version",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agent.id"), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        # The canonical, view-stripped §8.2 IR document; the (never-hashed) view block alongside.
        sa.Column("ir", postgresql.JSONB(), nullable=False),
        sa.Column("view", postgresql.JSONB(), nullable=True),
        # Monotonic per agent; THE ordering key (M4 §3 — not ULID/timestamp).
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
    )
    # The immutability guard (§1.2): one content per (agent, version), forever.
    op.create_index(
        "ix_agent_version_agent_version", "agent_version", ["agent_id", "version"], unique=True
    )
    # Order versions by seq, never by time/ULID (M4 §3).
    op.create_index("ix_agent_version_agent_seq", "agent_version", ["agent_id", "seq"])
    # Content-addressed lookup (pin-by-hash deploys in M12).
    op.create_index("ix_agent_version_content_hash", "agent_version", ["content_hash"])


def downgrade() -> None:
    # Reverse dependency order (agent_version -> agent) for a clean round-trip.
    op.drop_index("ix_agent_version_content_hash", table_name="agent_version")
    op.drop_index("ix_agent_version_agent_seq", table_name="agent_version")
    op.drop_index("ix_agent_version_agent_version", table_name="agent_version")
    op.drop_table("agent_version")
    op.drop_table("agent")
