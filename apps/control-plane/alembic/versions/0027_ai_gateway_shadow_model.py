"""ai gateway shadow model — group shadow runs by the model they ran on

A local trial runs a shadow on a local model the request's decision did not name, so a shadow's
model is no longer the decision's target, and the report gains a grouping by it
(``shadow-model``). The per-day aggregates are keyed by it too (``''``: no shadow), so a report
reads the same once its days are archived.

The days still kept in detail are rebuilt exactly from the request rows. The rebuild's SQL is
``GatewayTotals.of`` as it stands at this revision, written out here rather than imported, so a
later change to the totals cannot change what this migration does. An archived day has no request
rows left to split by: before this revision every shadow ran on its decision's target, so a row
with shadow runs is keyed by that target, the calls without one in the same row included.

Hand-written and reversible: the downgrade re-sums the rows under the narrower key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027_ai_gateway_shadow_model"
down_revision: str | None = "0026_ai_gateway_shadow_agreement"
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
    "session_id",
    "reason",
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
    "shadow_compared_calls",
    "shadow_agreed_calls",
)

#: The first day kept in detail: every day when nothing has been archived.
_KEPT = "COALESCE((SELECT archived_before FROM ai_gateway_state WHERE id = 1), DATE '0001-01-01')"

#: The kept days' rows, rebuilt from the request rows: ``GatewayTotals.of`` per row, summed per key.
_REBUILD = f"""
INSERT INTO ai_gateway_daily_usage (
    day, developer_id, client, request_class, requested_model, served_model, decision_target,
    session_id, reason, shadow_model, first_at, last_at,
    calls, errors, input_tokens, output_tokens, cache_read_tokens, cache_write_5m_tokens,
    cache_write_1h_tokens, cost_usd, unpriced_calls, baseline_usd, with_routing_usd, routed_calls,
    applied_calls, routed_tokens, routed_local_calls, routed_local_tokens, shadow_calls,
    shadow_answered_calls, shadow_cost_usd, shadow_output_tokens, shadowed_output_tokens,
    shadow_compared_calls, shadow_agreed_calls, updated_at
)
SELECT
    (started_at AT TIME ZONE 'UTC')::date,
    COALESCE(developer_id, ''), COALESCE(client, ''), request_class,
    COALESCE(requested_model, ''), COALESCE(served_model, ''),
    CASE WHEN decision_action = 'route' THEN COALESCE(decision_target_model, '') ELSE '' END,
    COALESCE(session_id, ''), COALESCE(decision_reasons->>0, ''),
    COALESCE(shadow->'target'->>'model', ''),
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
    COUNT(*) FILTER (WHERE shadow->>'agreed' IN ('true', 'false')),
    COUNT(*) FILTER (WHERE shadow->>'agreed' = 'true'),
    now()
FROM ai_gateway_request
WHERE (started_at AT TIME ZONE 'UTC')::date >= {_KEPT}
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10
"""


def upgrade() -> None:
    op.add_column("ai_gateway_daily_usage", sa.Column("shadow_model", sa.String(), nullable=True))
    op.execute(
        "UPDATE ai_gateway_daily_usage SET shadow_model = "
        "CASE WHEN shadow_calls > 0 THEN decision_target ELSE '' END"
    )
    op.execute(f"DELETE FROM ai_gateway_daily_usage WHERE day >= {_KEPT}")
    op.alter_column("ai_gateway_daily_usage", "shadow_model", nullable=False)
    op.drop_constraint("pk_ai_gateway_daily_usage", "ai_gateway_daily_usage", type_="primary")
    op.create_primary_key(
        "pk_ai_gateway_daily_usage", "ai_gateway_daily_usage", [*_KEY, "shadow_model"]
    )
    op.execute(_REBUILD)


def downgrade() -> None:
    key = ", ".join(_KEY)
    sums = ", ".join(f"SUM({c}) AS {c}" for c in _SUMS)
    op.execute(
        f"CREATE TEMP TABLE _daily AS SELECT {key}, {sums}, MIN(first_at) AS first_at, "
        f"MAX(last_at) AS last_at, MAX(updated_at) AS updated_at "
        f"FROM ai_gateway_daily_usage GROUP BY {key}"
    )
    op.execute("DELETE FROM ai_gateway_daily_usage")
    op.drop_constraint("pk_ai_gateway_daily_usage", "ai_gateway_daily_usage", type_="primary")
    op.drop_column("ai_gateway_daily_usage", "shadow_model")
    op.create_primary_key("pk_ai_gateway_daily_usage", "ai_gateway_daily_usage", list(_KEY))
    columns = ", ".join([*_KEY, *_SUMS, "first_at", "last_at", "updated_at"])
    op.execute(f"INSERT INTO ai_gateway_daily_usage ({columns}) SELECT {columns} FROM _daily")
    op.execute("DROP TABLE _daily")
