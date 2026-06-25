"""connection + secret — the tool/MCP auth seam (M19 §1.1)

M19's #1 hard-to-reverse decision: tool/MCP auth never lives inline in the IR. A node references a
logical tool key in ``ir.tools``; that binding references a ``connection`` by id; the connection
carries NON-SECRET config in JSONB and a ``secret_ref`` pointing at the encrypted ``secret`` row.
The handler resolves connection → secret_ref → plaintext SERVER-SIDE, inside the step, at runtime —
raw secrets never reach the IR, the canvas, a span, or the journal. Rotating the secret keeps every
agent's ``contentHash`` stable (the connection id is hashed content; the secret behind it is not).

* ``secret`` — encrypted-at-rest secret material (Fernet token text; ``secrets.SecretStore``). The
  plaintext is NEVER stored here. A real KMS/vault is the deferred upgrade (``theygent-stack.md``).
* ``connection`` — ``kind`` ∈ {http_auth, mcp_server}; ``config`` (JSONB) is non-secret only;
  ``secret_ref`` → ``secret.id``. The IR references this row by ``id`` from its ``tools`` block.

Hand-written and fully reversible — the §6 round-trip test exercises upgrade head -> downgrade base
including both new tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_connection"
down_revision: str | None = "0009_bench"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "secret",
        sa.Column("id", sa.String(), primary_key=True),  # the secret_ref
        sa.Column("ciphertext", sa.Text(), nullable=False),  # Fernet token (never plaintext)
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )

    op.create_table(
        "connection",
        sa.Column("id", sa.String(), primary_key=True),  # con_<ulid>
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),  # http_auth | mcp_server
        sa.Column("config", postgresql.JSONB(), nullable=False),  # non-secret config only
        sa.Column("secret_ref", sa.String(), nullable=True),  # → secret.id
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )
    op.create_index("ix_connection_kind", "connection", ["kind"])
    op.create_index("ix_connection_created", "connection", ["created_at", "id"])


def downgrade() -> None:
    # Reverse creation order for a clean round-trip.
    op.drop_index("ix_connection_created", table_name="connection")
    op.drop_index("ix_connection_kind", table_name="connection")
    op.drop_table("connection")
    op.drop_table("secret")
