"""agent_draft — server-side autosaved editor drafts

A draft is a mutable, autosaved, possibly-INVALID agent graph — the deliberate opposite of
``agent_version`` (immutable, content-addressed, validated). The visual editor autosaves
work-in-progress here so a half-wired graph survives a tab close; publishing stays on the
``/agents`` registry, which is why this table lives OUTSIDE ``agent_version``'s immutability
invariants: rows are updated in place, the ``ir`` is never validated or hashed, and ``view``
(layout) is split off exactly like ``agent_version.ir``/``view`` so the document column mirrors
the registry's shape.

* ``agent_id`` — the registry agent this draft edits (NULL for a never-published graph). A plain
  breadcrumb, NO foreign key: a draft's origin pointer must not couple lifecycles (deleting the
  agent must not take the draft with it), mirroring ``run.graph_id``/``run.trigger_id``.
* ``owner_id`` — the deferred per-user scoping slot (NULL until identity lands), the same
  slot-in pattern as ``agent_io_policy.updated_by``.
* ``name``/``node_count`` — derived list-view labels, re-derived from the ir on every write.

``ix_agent_draft_agent`` is non-unique on purpose: the client avoids duplicate per-agent drafts;
the DB doesn't enforce it, so a multi-tab race tolerably yields two drafts instead of an error.

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_agent_draft"
down_revision: str | None = "0015_rag"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "agent_draft",
        sa.Column("id", sa.String(), primary_key=True),  # drf_<ulid>
        sa.Column("agent_id", sa.String(), nullable=True),  # breadcrumb, no FK
        sa.Column("owner_id", sa.String(), nullable=True),  # deferred per-user scoping slot
        sa.Column("name", sa.String(), nullable=False),  # derived from the ir on every write
        sa.Column("node_count", sa.Integer(), nullable=False),  # derived from the ir
        sa.Column("ir", postgresql.JSONB(), nullable=False),  # unvalidated, unhashed document
        sa.Column("view", postgresql.JSONB(), nullable=True),  # layout, split off like the registry
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )
    op.create_index("ix_agent_draft_agent", "agent_draft", ["agent_id"])


def downgrade() -> None:
    op.drop_index("ix_agent_draft_agent", table_name="agent_draft")
    op.drop_table("agent_draft")
