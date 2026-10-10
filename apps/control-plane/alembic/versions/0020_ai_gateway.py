"""ai gateway — request records, daily usage, instances, configs, fingerprints, developers

The control plane's side of the AI gateway (``apps/ai-gateway``), which proxies coding agents'
Anthropic-format traffic and reports metadata about every exchange. Nothing stored here is
content: the gateway's wire types (``theygent_ir.gateway``) refuse free text, and these tables
hold what those types carry. Tables are prefixed ``ai_gateway_`` because "gateway" already names
the inference gateway client in this service.

- ``api_key.kind`` — ``personal`` (the existing keys: management API access), ``gateway`` (a
  developer's ``tyg_`` key, presented to a gateway), ``gateway_service`` (a gateway's own key
  for ``/gateway/*``). Existing rows become ``personal``. Only ``personal`` keys authenticate the
  management API.
- ``ai_gateway_request`` — one row per exchange; ``id`` is the gateway's record id, the
  idempotency key a re-sent batch dedupes on. Usage and the decision are columns so reports
  aggregate in SQL; the request shape, timing and seat reading stay JSONB.
- ``ai_gateway_daily_usage`` — per-day aggregates by developer, client, class, requested and
  served model, and decision, maintained as records arrive; reports over long ranges read these,
  and they outlive the request rows' retention. Missing dimension values are ``''`` (part of the
  primary key, which cannot hold NULL).
- ``ai_gateway_instance`` — the latest heartbeat of each gateway instance.
- ``ai_gateway_config`` — immutable, versioned configs; the version is the gateway's ETag.
- ``ai_gateway_fingerprint`` — the auxiliary-request catalog; unseen kinds are ``unreviewed``.
- ``ai_gateway_developer`` — a developer's credential profile (``passthrough`` | ``org_key``).
- ``ai_gateway_state`` — one row holding the key-set version, bumped in the same transaction as
  any change to the live gateway keys, so a gateway never reads a version before its change.

Hand-written and fully reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_ai_gateway"
down_revision: str | None = "0019_user_avatar"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.add_column(
        "api_key",
        sa.Column("kind", sa.String(), nullable=False, server_default="personal"),
    )

    op.create_table(
        "ai_gateway_request",
        sa.Column("id", sa.String(), primary_key=True),  # the gateway's record id
        sa.Column("gateway_id", sa.String(), nullable=False),
        # Breadcrumb to the api_key that delivered it (kind gateway_service).
        sa.Column("service_key_id", sa.String(), nullable=True),
        sa.Column("received_at", _TZ, nullable=False),
        # The gateway's clock: when the exchange started. Reports filter on it; a session's
        # timeline orders by it, ties broken by id.
        sa.Column("started_at", _TZ, nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("status", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("error_type", sa.String(), nullable=True),
        sa.Column("developer_id", sa.String(), nullable=True),  # user_account.id breadcrumb
        sa.Column("credential_kind", sa.String(), nullable=False),
        sa.Column("credential_profile", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=True),
        sa.Column("client_version", sa.String(), nullable=True),
        sa.Column("session_id", sa.String(), nullable=True),
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("parent_agent_id", sa.String(), nullable=True),
        sa.Column("agent_type", sa.String(), nullable=True),
        sa.Column("prompt_id", sa.String(), nullable=True),
        sa.Column("request_class", sa.String(), nullable=False),
        sa.Column("request_class_raw", sa.String(), nullable=True),
        sa.Column("compaction", sa.String(), nullable=True),
        sa.Column("context_compacted", sa.String(), nullable=True),
        sa.Column("fingerprint", sa.String(), nullable=True),
        sa.Column("shares_main_prefix", sa.Boolean(), nullable=True),
        sa.Column("shape", postgresql.JSONB(), nullable=True),
        sa.Column("requested_model", sa.String(), nullable=True),
        sa.Column("served_model", sa.String(), nullable=True),
        sa.Column("stop_reason", sa.String(), nullable=True),
        sa.Column("tool_use_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_5m_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_1h_tokens", sa.BigInteger(), nullable=False),
        sa.Column("timing", postgresql.JSONB(), nullable=False),
        sa.Column("seat_allowance", postgresql.JSONB(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("pricing_version", sa.String(), nullable=True),
        sa.Column("decision_action", sa.String(), nullable=False),  # stay | route
        sa.Column("decision_target_kind", sa.String(), nullable=True),  # anthropic | local
        sa.Column("decision_target_model", sa.String(), nullable=True),
        sa.Column("decision_reasons", postgresql.JSONB(), nullable=False),  # reason codes
        sa.Column("counterfactual_usd", sa.Float(), nullable=True),
        sa.Column("decision_applied", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_ai_gateway_request_started", "ai_gateway_request", ["started_at"])
    op.create_index(
        "ix_ai_gateway_request_developer", "ai_gateway_request", ["developer_id", "started_at"]
    )
    op.create_index(
        "ix_ai_gateway_request_session", "ai_gateway_request", ["session_id", "started_at"]
    )
    op.create_index("ix_ai_gateway_request_fingerprint", "ai_gateway_request", ["fingerprint"])

    op.create_table(
        "ai_gateway_daily_usage",
        sa.Column("day", sa.Date(), nullable=False),  # UTC
        sa.Column("developer_id", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("request_class", sa.String(), nullable=False),
        sa.Column("requested_model", sa.String(), nullable=False),
        sa.Column("served_model", sa.String(), nullable=False),
        sa.Column("decision_target", sa.String(), nullable=False),  # '' = stay
        sa.Column("calls", sa.BigInteger(), nullable=False),
        sa.Column("errors", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_read_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_5m_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cache_write_1h_tokens", sa.BigInteger(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("unpriced_calls", sa.BigInteger(), nullable=False),
        sa.Column("with_routing_usd", sa.Float(), nullable=False),
        sa.Column("routed_calls", sa.BigInteger(), nullable=False),
        sa.Column("routed_tokens", sa.BigInteger(), nullable=False),
        sa.Column("routed_local_calls", sa.BigInteger(), nullable=False),
        sa.Column("routed_local_tokens", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.PrimaryKeyConstraint(
            "day",
            "developer_id",
            "client",
            "request_class",
            "requested_model",
            "served_model",
            "decision_target",
            name="pk_ai_gateway_daily_usage",
        ),
    )

    op.create_table(
        "ai_gateway_instance",
        sa.Column("gateway_id", sa.String(), primary_key=True),
        sa.Column("service_key_id", sa.String(), nullable=True),  # api_key.id breadcrumb
        sa.Column("software_version", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=True),
        sa.Column("key_set_version", sa.BigInteger(), nullable=True),
        sa.Column("started_at", _TZ, nullable=False),
        sa.Column("spool_backlog", sa.BigInteger(), nullable=False),
        sa.Column("upstream_reachable", sa.Boolean(), nullable=True),
        sa.Column("first_seen_at", _TZ, nullable=False),
        sa.Column("last_seen_at", _TZ, nullable=False),
    )

    op.create_table(
        "ai_gateway_config",
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("document", postgresql.JSONB(), nullable=False),  # the GatewayConfig
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("created_by", sa.String(), nullable=True),  # user_account.id breadcrumb
    )

    op.create_table(
        "ai_gateway_fingerprint",
        sa.Column("fingerprint", sa.String(), primary_key=True),  # 16 hex
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("first_seen_at", _TZ, nullable=False),
        sa.Column("last_seen_at", _TZ, nullable=False),
        sa.Column("request_count", sa.BigInteger(), nullable=False),
        sa.Column("client_versions", postgresql.JSONB(), nullable=False),
        sa.Column("shape", postgresql.JSONB(), nullable=True),
        sa.Column("shares_main_prefix", sa.Boolean(), nullable=True),
        sa.Column("reviewed_at", _TZ, nullable=True),
        sa.Column("reviewed_by", sa.String(), nullable=True),  # user_account.id breadcrumb
    )

    op.create_table(
        "ai_gateway_developer",
        sa.Column(
            "user_id",
            sa.String(),
            sa.ForeignKey("user_account.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("credential_profile", sa.String(), nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.Column("updated_by", sa.String(), nullable=True),  # user_account.id breadcrumb
    )

    state = op.create_table(
        "ai_gateway_state",
        sa.Column("id", sa.SmallInteger(), primary_key=True),  # always 1
        sa.Column("key_set_version", sa.BigInteger(), nullable=False),
    )
    op.bulk_insert(state, [{"id": 1, "key_set_version": 1}])


def downgrade() -> None:
    op.drop_table("ai_gateway_state")
    op.drop_table("ai_gateway_developer")
    op.drop_table("ai_gateway_fingerprint")
    op.drop_table("ai_gateway_config")
    op.drop_table("ai_gateway_instance")
    op.drop_table("ai_gateway_daily_usage")
    op.drop_index("ix_ai_gateway_request_fingerprint", table_name="ai_gateway_request")
    op.drop_index("ix_ai_gateway_request_session", table_name="ai_gateway_request")
    op.drop_index("ix_ai_gateway_request_developer", table_name="ai_gateway_request")
    op.drop_index("ix_ai_gateway_request_started", table_name="ai_gateway_request")
    op.drop_table("ai_gateway_request")
    op.drop_column("api_key", "kind")
