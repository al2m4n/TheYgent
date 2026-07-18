"""mcp_server http transport — persist url/headers, relax command.

The registration payload (``McpServerConfig``) supports an ``http`` transport (a remote
streamable-HTTP server reached by ``url`` + ``headers``). Make ``command`` nullable (an http
registration has none) and add the ``url``/``headers`` columns so any registration round-trips
a restart intact.

Hand-written + reversible (downgrade restores NOT NULL by backfilling '' for http rows).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0013_mcp_http"
down_revision: str | None = "0012_run_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("mcp_server", "command", existing_type=sa.String(), nullable=True)
    op.add_column("mcp_server", sa.Column("url", sa.String(), nullable=True))
    op.add_column("mcp_server", sa.Column("headers", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column("mcp_server", "headers")
    op.drop_column("mcp_server", "url")
    op.execute("UPDATE mcp_server SET command = '' WHERE command IS NULL")
    op.alter_column("mcp_server", "command", existing_type=sa.String(), nullable=False)
