"""Persistence for the AI gateway's records (migration ``0020_ai_gateway``).

``ingest`` takes one ``GatewayEventBatch`` in the caller's transaction:

* every record is inserted keyed by its id, and a record already stored is skipped — a batch
  re-sent after a lost acknowledgement is a no-op, and two deliveries of one record racing each
  other count once (the second waits on the first's key, then finds it);
* the records that were new are added to the per-day aggregates, so totals never double-count;
* new auxiliary requests update the fingerprint catalog's stats (first and last seen, count,
  client versions, latest shape); a kind seen for the first time is ``unreviewed``.

Aggregate and catalog rows are upserted in key order, so concurrent batches touching the same
rows take their locks in the same order. Totals are ``GatewayTotals.of`` summed: the same
definition the gateway's own report uses.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import case, func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from theygent_ir.gateway import (
    GatewayEventAck,
    GatewayEventBatch,
    GatewayRequestRecord,
    GatewayTotals,
)

from theygent_control_plane.models import (
    AiGatewayDailyUsageRow,
    AiGatewayFingerprintRow,
    AiGatewayRequestRow,
)

#: A day's aggregates are keyed by these, with ``''`` for a missing value.
DailyKey = tuple[date, str, str, str, str, str, str]
_TOTAL_FIELDS = tuple(GatewayTotals.model_fields)


def _request_row(
    record: GatewayRequestRecord, *, service_key_id: str | None, received_at: datetime
) -> dict[str, object]:
    usage = record.usage
    decision = record.decision
    return {
        "id": record.id,
        "gateway_id": record.gateway_id,
        "service_key_id": service_key_id,
        "received_at": received_at,
        "started_at": record.started_at,
        "config_version": record.config_version,
        "endpoint": record.endpoint,
        "status": record.status,
        "outcome": record.outcome,
        "error_type": record.error_type,
        "developer_id": record.developer_id,
        "credential_kind": record.credential_kind,
        "credential_profile": record.credential_profile,
        "client": record.client,
        "client_version": record.client_version,
        "session_id": record.session_id,
        "agent_id": record.agent_id,
        "parent_agent_id": record.parent_agent_id,
        "agent_type": record.agent_type,
        "prompt_id": record.prompt_id,
        "request_class": record.request_class,
        "request_class_raw": record.request_class_raw,
        "compaction": record.compaction,
        "context_compacted": record.context_compacted,
        "fingerprint": record.fingerprint,
        "shares_main_prefix": record.shares_main_prefix,
        "shape": record.shape.model_dump(mode="json") if record.shape else None,
        "requested_model": record.requested_model,
        "served_model": record.served_model,
        "stop_reason": record.stop_reason,
        "tool_use_count": record.tool_use_count,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cache_write_5m_tokens": usage.cache_write_5m_tokens,
        "cache_write_1h_tokens": usage.cache_write_1h_tokens,
        "timing": record.timing.model_dump(mode="json"),
        "seat_allowance": (
            record.seat_allowance.model_dump(mode="json") if record.seat_allowance else None
        ),
        "cost_usd": record.cost_usd,
        "pricing_version": record.pricing_version,
        "decision_action": decision.action,
        "decision_target_kind": decision.target.kind if decision.target else None,
        "decision_target_model": decision.target.model if decision.target else None,
        "decision_reasons": list(decision.reasons),
        "counterfactual_usd": decision.counterfactual_usd,
        "decision_applied": decision.applied,
    }


def daily_key(record: GatewayRequestRecord) -> DailyKey:
    decision = record.decision
    target = decision.target.model if decision.action == "route" and decision.target else ""
    return (
        record.started_at.astimezone(UTC).date(),
        record.developer_id or "",
        record.client or "",
        record.request_class,
        record.requested_model or "",
        record.served_model or "",
        target,
    )


class AiGatewayStore:
    """Never commits: the caller owns the transaction."""

    async def ingest(
        self,
        session: AsyncSession,
        batch: GatewayEventBatch,
        *,
        service_key_id: str | None,
        received_at: datetime,
    ) -> GatewayEventAck:
        unique: dict[str, GatewayRequestRecord] = {}
        for record in batch.records:
            unique.setdefault(record.id, record)
        inserted = await session.execute(
            insert(AiGatewayRequestRow)
            .values(
                [
                    _request_row(r, service_key_id=service_key_id, received_at=received_at)
                    for r in unique.values()
                ]
            )
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(AiGatewayRequestRow.id)
        )
        fresh_ids = set(inserted.scalars())
        fresh = [record for record in unique.values() if record.id in fresh_ids]
        if fresh:
            await self._add_to_daily(session, fresh, received_at)
            await self._add_to_catalog(session, fresh)
        return GatewayEventAck(accepted=len(fresh), duplicates=len(batch.records) - len(fresh))

    async def _add_to_daily(
        self, session: AsyncSession, records: list[GatewayRequestRecord], now: datetime
    ) -> None:
        totals: dict[DailyKey, GatewayTotals] = {}
        for record in records:
            key = daily_key(record)
            totals[key] = totals.get(key, GatewayTotals()) + GatewayTotals.of(record)
        names = (
            "day",
            "developer_id",
            "client",
            "request_class",
            "requested_model",
            "served_model",
            "decision_target",
        )
        statement = insert(AiGatewayDailyUsageRow).values(
            [
                {
                    **dict(zip(names, key, strict=True)),
                    **totals[key].model_dump(),
                    "updated_at": now,
                }
                for key in sorted(totals)
            ]
        )
        table = AiGatewayDailyUsageRow.__table__
        await session.execute(
            statement.on_conflict_do_update(
                constraint="pk_ai_gateway_daily_usage",
                set_={
                    **{f: table.c[f] + statement.excluded[f] for f in _TOTAL_FIELDS},
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    async def _add_to_catalog(
        self, session: AsyncSession, records: list[GatewayRequestRecord]
    ) -> None:
        seen: dict[str, list[GatewayRequestRecord]] = {}
        for record in records:
            if record.request_class == "auxiliary" and record.fingerprint is not None:
                seen.setdefault(record.fingerprint, []).append(record)
        if not seen:
            return
        rows = []
        for fingerprint in sorted(seen):
            kind = sorted(seen[fingerprint], key=lambda r: r.started_at)
            latest = kind[-1]
            # The newest shape actually observed; a sighting without one keeps what was known.
            shape = next((r.shape for r in reversed(kind) if r.shape is not None), None)
            rows.append(
                {
                    "fingerprint": fingerprint,
                    "status": "unreviewed",
                    "label": None,
                    "first_seen_at": kind[0].started_at,
                    "last_seen_at": latest.started_at,
                    "request_count": len(kind),
                    "client_versions": sorted({r.client_version for r in kind if r.client_version}),
                    "shape": shape.model_dump(mode="json") if shape else None,
                    "shares_main_prefix": latest.shares_main_prefix,
                    "reviewed_at": None,
                    "reviewed_by": None,
                }
            )
        statement = insert(AiGatewayFingerprintRow).values(rows)
        table = AiGatewayFingerprintRow.__table__
        excluded = statement.excluded
        newer = excluded.last_seen_at >= table.c.last_seen_at
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["fingerprint"],
                set_={
                    "first_seen_at": func.least(table.c.first_seen_at, excluded.first_seen_at),
                    "last_seen_at": func.greatest(table.c.last_seen_at, excluded.last_seen_at),
                    "request_count": table.c.request_count + excluded.request_count,
                    "client_versions": _MERGED_CLIENT_VERSIONS,
                    "shape": case(
                        (newer, func.coalesce(excluded.shape, table.c.shape)), else_=table.c.shape
                    ),
                    "shares_main_prefix": case(
                        (newer, excluded.shares_main_prefix), else_=table.c.shares_main_prefix
                    ),
                },
            )
        )


#: Both lists' client versions, distinct and sorted.
_MERGED_CLIENT_VERSIONS = text(
    "(SELECT coalesce(jsonb_agg(DISTINCT v ORDER BY v), '[]'::jsonb) "
    "FROM jsonb_array_elements_text("
    "ai_gateway_fingerprint.client_versions || excluded.client_versions) AS v)"
)
