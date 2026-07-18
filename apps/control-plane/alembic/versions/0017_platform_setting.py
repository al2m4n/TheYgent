"""platform_setting — stored platform configuration (the settings surface)

One row per *stored* setting. The catalog of valid keys, their types, defaults, and env
precedence lives in code (``settings.py``) — this table holds only the values a user has
explicitly set through the API, so "reset to default" is a row DELETE, never a sentinel value.
``value`` is the JSON value itself (bool/number/string/list/object); for a sensitive key it is
the ``{"secret_ref", "names"}`` envelope pointing at an encrypted ``secret`` row — plaintext
never lands here. Rows whose key has left the catalog (an upgrade/downgrade skew) are ignored
at resolution and surfaced as "orphaned", so this table never gates a boot.

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_platform_setting"
down_revision: str | None = "0016_agent_draft"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "platform_setting",
        sa.Column("key", sa.Text(), primary_key=True),  # dotted catalog key ("telemetry.…")
        sa.Column("value", postgresql.JSONB(), nullable=False),  # the stored JSON value
        sa.Column("updated_at", _TZ, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("platform_setting")
