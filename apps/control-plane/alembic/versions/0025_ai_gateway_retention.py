"""ai gateway retention — archive old request rows without losing what reports show

Request rows older than the retention period move to ``ai_gateway_request_archive`` (the records
as gateways sent them, gzip-compressed JSON lines), and reports read those days from the per-day
aggregates. For that to lose nothing, the aggregates gain the two report groupings they lacked —
the session and the decision's first reason — and the time span of the requests each row sums.
``ai_gateway_state.archived_before`` marks the days already archived.

Nothing has been archived when this runs, so the aggregates are rebuilt exactly from the request
rows. The rebuild's SQL is ``GatewayTotals.of`` as it stands at this revision, written out here
rather than imported, so a later change to the totals cannot change what this migration does.

Hand-written and reversible: the downgrade restores the narrower key (re-summing the rows) and
drops the archive, so downgrade only before anything has been archived.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_ai_gateway_retention"
down_revision: str | None = "0024_ai_gateway_shadows"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KEY = (
    "day",
    "developer_id",
    "client",
    "request_class",
    "requested_model",
    "served_model",
    "decision_target",
)
_SUMS = (
    "calls",
    "errors",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cost_usd",
    "unpriced_calls",
    "baseline_usd",
    "with_routing_usd",
    "routed_calls",
    "applied_calls",
    "routed_tokens",
    "routed_local_calls",
    "routed_local_tokens",
    "shadow_calls",
    "shadow_answered_calls",
    "shadow_cost_usd",
    "shadow_output_tokens",
    "shadowed_output_tokens",
)

#: The daily rows, rebuilt from the request rows: ``GatewayTotals.of`` per row, summed per key.
_REBUILD = """
INSERT INTO ai_gateway_daily_usage (
    day, developer_id, client, request_class, requested_model, served_model, decision_target,
    session_id, reason, first_at, last_at,
    calls, errors, input_tokens, output_tokens, cache_read_tokens, cache_write_5m_tokens,
    cache_write_1h_tokens, cost_usd, unpriced_calls, baseline_usd, with_routing_usd, routed_calls,
    applied_calls, routed_tokens, routed_local_calls, routed_local_tokens, shadow_calls,
    shadow_answered_calls, shadow_cost_usd, shadow_output_tokens, shadowed_output_tokens,
    updated_at
)
SELECT
    (started_at AT TIME ZONE 'UTC')::date,
    COALESCE(developer_id, ''), COALESCE(client, ''), request_class,
    COALESCE(requested_model, ''), COALESCE(served_model, ''),
    CASE WHEN decision_action = 'route' THEN COALESCE(decision_target_model, '') ELSE '' END,
    COALESCE(session_id, ''), COALESCE(decision_reasons->>0, ''),
    MIN(started_at), MAX(started_at),
    COUNT(*),
    COUNT(*) FILTER (WHERE outcome <> 'complete' OR status >= 400),
    SUM(input_tokens), SUM(output_tokens), SUM(cache_read_tokens),
    SUM(cache_write_5m_tokens), SUM(cache_write_1h_tokens),
    SUM(COALESCE(cost_usd, 0)),
    COUNT(*) FILTER (WHERE cost_usd IS NULL AND (input_tokens + output_tokens + cache_read_tokens
        + cache_write_5m_tokens + cache_write_1h_tokens) > 0),
    SUM(COALESCE(baseline_usd, cost_usd, 0)),
    SUM(CASE WHEN decision_action = 'route' AND counterfactual_usd IS NOT NULL
             THEN counterfactual_usd ELSE COALESCE(cost_usd, 0) END),
    COUNT(*) FILTER (WHERE decision_action = 'route'),
    COUNT(*) FILTER (WHERE decision_action = 'route' AND decision_applied),
    SUM(CASE WHEN decision_action = 'route' THEN input_tokens + output_tokens + cache_read_tokens
             + cache_write_5m_tokens + cache_write_1h_tokens ELSE 0 END),
    COUNT(*) FILTER (WHERE decision_action = 'route' AND decision_target_kind = 'local'),
    SUM(CASE WHEN decision_action = 'route' AND decision_target_kind = 'local'
             THEN input_tokens + output_tokens + cache_read_tokens + cache_write_5m_tokens
                  + cache_write_1h_tokens ELSE 0 END),
    COUNT(*) FILTER (WHERE shadow IS NOT NULL),
    COUNT(*) FILTER (WHERE shadow->>'outcome' = 'answered'),
    SUM(CASE WHEN shadow IS NOT NULL THEN COALESCE((shadow->>'cost_usd')::float, 0) ELSE 0 END),
    SUM(CASE WHEN shadow->>'outcome' = 'answered'
             THEN COALESCE((shadow->'usage'->>'output_tokens')::bigint, 0) ELSE 0 END),
    SUM(CASE WHEN shadow->>'outcome' = 'answered' THEN output_tokens ELSE 0 END),
    now()
FROM ai_gateway_request
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
"""


def upgrade() -> None:
    op.add_column("ai_gateway_daily_usage", sa.Column("session_id", sa.String(), nullable=True))
    op.add_column("ai_gateway_daily_usage", sa.Column("reason", sa.String(), nullable=True))
    op.add_column(
        "ai_gateway_daily_usage", sa.Column("first_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.add_column(
        "ai_gateway_daily_usage", sa.Column("last_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )
    op.execute("DELETE FROM ai_gateway_daily_usage")
    for name in ("session_id", "reason", "first_at", "last_at"):
        op.alter_column("ai_gateway_daily_usage", name, nullable=False)
    op.drop_constraint("pk_ai_gateway_daily_usage", "ai_gateway_daily_usage", type_="primary")
    op.create_primary_key(
        "pk_ai_gateway_daily_usage",
        "ai_gateway_daily_usage",
        [*_KEY, "session_id", "reason"],
    )
    op.create_index("ix_ai_gateway_daily_usage_session", "ai_gateway_daily_usage", ["session_id"])
    op.execute(_REBUILD)

    op.create_table(
        "ai_gateway_request_archive",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("records", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_ai_gateway_request_archive_day", "ai_gateway_request_archive", ["day"])
    op.add_column("ai_gateway_state", sa.Column("archived_before", sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_gateway_state", "archived_before")
    op.drop_index("ix_ai_gateway_request_archive_day", table_name="ai_gateway_request_archive")
    op.drop_table("ai_gateway_request_archive")
    op.drop_index("ix_ai_gateway_daily_usage_session", table_name="ai_gateway_daily_usage")
    sums = ", ".join(f"SUM({c}) AS {c}" for c in _SUMS)
    key = ", ".join(_KEY)
    op.execute(
        f"CREATE TEMP TABLE _daily AS SELECT {key}, {sums}, MAX(updated_at) AS updated_at "
        f"FROM ai_gateway_daily_usage GROUP BY {key}"
    )
    op.execute("DELETE FROM ai_gateway_daily_usage")
    op.drop_constraint("pk_ai_gateway_daily_usage", "ai_gateway_daily_usage", type_="primary")
    for name in ("last_at", "first_at", "reason", "session_id"):
        op.drop_column("ai_gateway_daily_usage", name)
    op.create_primary_key("pk_ai_gateway_daily_usage", "ai_gateway_daily_usage", list(_KEY))
    columns = ", ".join([*_KEY, *_SUMS, "updated_at"])
    op.execute(f"INSERT INTO ai_gateway_daily_usage ({columns}) SELECT {columns} FROM _daily")
    op.execute("DROP TABLE _daily")
