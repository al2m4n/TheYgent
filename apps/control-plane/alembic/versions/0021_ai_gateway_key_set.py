"""ai gateway key set — the version moves with every change to who holds a live gateway key

AI gateways re-fetch developer keys only when ``ai_gateway_state.key_set_version`` moves, so a
revocation that fails to move it would stay live in every gateway. Triggers move it in the same
transaction as the change, whichever code path (or hand-run SQL) made it:

- a gateway key (``api_key.kind = 'gateway'``) inserted, deleted (including by its owner's
  deletion cascading), or changed in kind, revocation, owner or token;
- an account enabled or disabled;
- any change to a developer's credential profile (``ai_gateway_developer``).

Writes that do not change the key set (a personal key's ``last_used_at`` touch, a role change)
leave it alone. The bump upserts the state row, so a missing row cannot stall the version.

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0021_ai_gateway_key_set"
down_revision: str | None = "0020_ai_gateway"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BUMP = "ai_gateway_key_set_changed"

#: name → (events, table, row filter): each runs the bump after a change to the key set.
_TRIGGERS: dict[str, tuple[str, str, str]] = {
    "ai_gateway_key_inserted": ("INSERT", "api_key", "FOR EACH ROW WHEN (NEW.kind = 'gateway')"),
    "ai_gateway_key_deleted": ("DELETE", "api_key", "FOR EACH ROW WHEN (OLD.kind = 'gateway')"),
    "ai_gateway_key_updated": (
        "UPDATE OF kind, revoked_at, user_id, token_hash",
        "api_key",
        "FOR EACH ROW WHEN ((OLD.kind = 'gateway' OR NEW.kind = 'gateway') AND "
        "(OLD.kind, OLD.revoked_at, OLD.user_id, OLD.token_hash) IS DISTINCT FROM "
        "(NEW.kind, NEW.revoked_at, NEW.user_id, NEW.token_hash))",
    ),
    "ai_gateway_owner_enabled_changed": (
        "UPDATE OF disabled",
        "user_account",
        "FOR EACH ROW WHEN (OLD.disabled IS DISTINCT FROM NEW.disabled)",
    ),
    "ai_gateway_profile_changed": (
        "INSERT OR UPDATE OR DELETE",
        "ai_gateway_developer",
        "FOR EACH STATEMENT",
    ),
}


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {_BUMP}() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            INSERT INTO ai_gateway_state (id, key_set_version) VALUES (1, 2)
            ON CONFLICT (id) DO UPDATE
                SET key_set_version = ai_gateway_state.key_set_version + 1;
            RETURN NULL;
        END;
        $$
        """
    )
    for name, (events, table, rows) in _TRIGGERS.items():
        op.execute(
            f"CREATE TRIGGER {name} AFTER {events} ON {table} {rows} EXECUTE FUNCTION {_BUMP}()"
        )


def downgrade() -> None:
    for name, (_, table, _) in reversed(_TRIGGERS.items()):
        op.execute(f"DROP TRIGGER {name} ON {table}")
    op.execute(f"DROP FUNCTION {_BUMP}()")
