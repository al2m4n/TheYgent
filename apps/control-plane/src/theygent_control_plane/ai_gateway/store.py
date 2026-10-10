"""Persistence for the AI gateway's records (migration ``0020_ai_gateway``).

``ingest`` takes one ``GatewayEventBatch`` in the caller's transaction:

* every record is inserted keyed by its id, and a record already stored is skipped — a batch
  re-sent after a lost acknowledgement is a no-op, and two deliveries of one record racing each
  other count once (the second waits on the first's key, then finds it);
* the records that were new are added to the per-day aggregates, so totals never double-count;
* new auxiliary requests update the fingerprint catalog's stats (first and last seen, count,
  client versions, latest shape). Review status is not stored here: it is a rule in the config.

``key_set`` is what gateways authenticate developers against: every live gateway key of an
enabled account, as a digest, with its developer's credential profile. Its version moves (by
trigger, migration ``0021``) in the same transaction as any change to that set.

Aggregate and catalog rows are upserted in key order, so concurrent batches touching the same
rows take their locks in the same order. Totals are ``GatewayTotals.of`` summed: the same
definition the gateway's own report uses.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import case, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from theygent_ir.gateway import (
    CredentialProfile,
    FingerprintRule,
    GatewayConfig,
    GatewayEventAck,
    GatewayEventBatch,
    GatewayHeartbeat,
    GatewayHeartbeatAck,
    GatewayKey,
    GatewayKeySet,
    GatewayMode,
    GatewayRequestRecord,
    GatewayTotals,
    ModelRates,
    PolicyConfig,
)

from theygent_control_plane.models import (
    AiGatewayConfigRow,
    AiGatewayDailyUsageRow,
    AiGatewayDeveloperRow,
    AiGatewayFingerprintRow,
    AiGatewayInstanceRow,
    AiGatewayRequestRow,
    AiGatewayStateRow,
    ApiKeyRow,
    UserAccountRow,
)

#: A developer without a profile row brings their own credential.
DEFAULT_PROFILE: CredentialProfile = "passthrough"
#: An instance whose last heartbeat is older than this is shown offline. Gateways beat every 30 s
#: by default, so this allows three missed beats and some clock skew.
INSTANCE_STALE_AFTER = timedelta(seconds=120)
#: Serializes config writes, so each save takes the next version without racing another.
_CONFIG_LOCK = 0x7A6731  # arbitrary, fixed


class ConfigDraft(BaseModel):
    """What an admin writes; the version, time and author are the server's."""

    model_config = ConfigDict(extra="forbid")

    mode: GatewayMode
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    fingerprints: list[FingerprintRule] = Field(default_factory=list)
    pricing_overrides: dict[str, ModelRates] = Field(default_factory=dict)


class ConfigVersion(BaseModel):
    version: int
    mode: GatewayMode
    created_at: datetime
    created_by: str | None


class Instance(BaseModel):
    """A gateway instance as its latest heartbeat described it."""

    gateway_id: str
    software_version: str
    mode: GatewayMode
    config_version: int | None
    #: Whether it runs the latest config.
    config_current: bool
    key_set_version: int | None
    started_at: datetime
    spool_backlog: int
    upstream_reachable: bool | None
    first_seen_at: datetime
    last_seen_at: datetime
    online: bool
    service_key_id: str | None


class Developer(BaseModel):
    """An account as the AI gateway sees it: how it is billed, and how many live keys it has."""

    user_id: str
    username: str
    display_name: str
    disabled: bool
    credential_profile: CredentialProfile
    live_gateway_keys: int
    updated_at: datetime | None = None
    updated_by: str | None = None


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

    async def key_set_version(self, session: AsyncSession) -> int:
        row = await session.get(AiGatewayStateRow, 1)
        return row.key_set_version if row is not None else 1

    async def key_set(self, session: AsyncSession, *, generated_at: datetime) -> GatewayKeySet:
        # The version is read before the keys: a change committing in between then shows up
        # under the older version, and the gateway's next sync sees the newer one and fetches
        # again. Read the other way round, a gateway could keep stale keys under a new version.
        version = await self.key_set_version(session)
        result = await session.execute(
            select(ApiKeyRow.id, ApiKeyRow.token_hash, ApiKeyRow.user_id, AiGatewayDeveloperRow)
            .join(UserAccountRow, UserAccountRow.id == ApiKeyRow.user_id)
            .outerjoin(AiGatewayDeveloperRow, AiGatewayDeveloperRow.user_id == ApiKeyRow.user_id)
            .where(
                ApiKeyRow.kind == "gateway",
                ApiKeyRow.revoked_at.is_(None),
                UserAccountRow.disabled.is_(False),
            )
            .order_by(ApiKeyRow.id)
        )
        keys = [
            GatewayKey(
                key_id=key_id,
                token_sha256=token_hash,
                developer_id=user_id,
                credential_profile=(
                    profile.credential_profile if profile is not None else DEFAULT_PROFILE
                ),
            )
            for key_id, token_hash, user_id, profile in result.all()
        ]
        return GatewayKeySet(version=version, generated_at=generated_at, keys=keys)

    # -- config: immutable versions --

    async def latest_config(self, session: AsyncSession) -> GatewayConfig | None:
        row = (
            await session.execute(
                select(AiGatewayConfigRow).order_by(AiGatewayConfigRow.version.desc()).limit(1)
            )
        ).scalar_one_or_none()
        return GatewayConfig.model_validate(row.document) if row is not None else None

    async def latest_config_version(self, session: AsyncSession) -> int | None:
        return (await session.execute(select(func.max(AiGatewayConfigRow.version)))).scalar()

    async def config(self, session: AsyncSession, version: int) -> GatewayConfig | None:
        row = await session.get(AiGatewayConfigRow, version)
        return GatewayConfig.model_validate(row.document) if row is not None else None

    async def config_versions(self, session: AsyncSession) -> list[ConfigVersion]:
        result = await session.execute(
            select(AiGatewayConfigRow).order_by(AiGatewayConfigRow.version.desc())
        )
        return [
            ConfigVersion.model_validate(
                {
                    "version": row.version,
                    "mode": row.mode,
                    "created_at": row.created_at,
                    "created_by": row.created_by,
                }
            )
            for row in result.scalars()
        ]

    async def save_config(
        self, session: AsyncSession, draft: ConfigDraft, *, by: str | None, now: datetime
    ) -> GatewayConfig:
        """Store ``draft`` as the next version. Raises ``ValidationError`` for a config the
        gateway would refuse (a duplicate fingerprint rule, an engine name as a local target)."""
        await session.execute(select(func.pg_advisory_xact_lock(_CONFIG_LOCK)))
        latest = await self.latest_config_version(session)
        config = GatewayConfig.model_validate(
            {
                **draft.model_dump(mode="json"),
                "version": (latest or 0) + 1,
                "updated_at": now,
                "updated_by": by,
            }
        )
        session.add(
            AiGatewayConfigRow(
                version=config.version,
                mode=config.mode,
                document=config.model_dump(mode="json"),
                created_at=now,
                created_by=by,
            )
        )
        await session.flush()
        return config

    # -- instances --

    async def record_heartbeat(
        self,
        session: AsyncSession,
        beat: GatewayHeartbeat,
        *,
        service_key_id: str | None,
        now: datetime,
    ) -> GatewayHeartbeatAck:
        values = {
            "gateway_id": beat.gateway_id,
            "service_key_id": service_key_id,
            "software_version": beat.software_version,
            "mode": beat.mode,
            "config_version": beat.config_version,
            "key_set_version": beat.key_set_version,
            "started_at": beat.started_at,
            "spool_backlog": beat.spool_backlog,
            "upstream_reachable": beat.upstream_reachable,
            "first_seen_at": now,
            "last_seen_at": now,
        }
        statement = insert(AiGatewayInstanceRow).values(**values)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["gateway_id"],
                set_={
                    k: statement.excluded[k]
                    for k in values
                    if k not in ("gateway_id", "first_seen_at")
                },
            )
        )
        return GatewayHeartbeatAck(
            config_version=await self.latest_config_version(session),
            key_set_version=await self.key_set_version(session),
        )

    async def instances(self, session: AsyncSession, *, now: datetime) -> list[Instance]:
        latest = await self.latest_config_version(session)
        result = await session.execute(
            select(AiGatewayInstanceRow).order_by(AiGatewayInstanceRow.gateway_id)
        )
        return [
            Instance.model_validate(
                {
                    "gateway_id": row.gateway_id,
                    "software_version": row.software_version,
                    "mode": row.mode,
                    "config_version": row.config_version,
                    "config_current": row.config_version == latest,
                    "key_set_version": row.key_set_version,
                    "started_at": row.started_at,
                    "spool_backlog": row.spool_backlog,
                    "upstream_reachable": row.upstream_reachable,
                    "first_seen_at": row.first_seen_at,
                    "last_seen_at": row.last_seen_at,
                    "online": now - row.last_seen_at <= INSTANCE_STALE_AFTER,
                    "service_key_id": row.service_key_id,
                }
            )
            for row in result.scalars()
        ]

    # -- developers --

    async def developers(self, session: AsyncSession) -> list[Developer]:
        live = (
            select(ApiKeyRow.user_id, func.count().label("n"))
            .where(ApiKeyRow.kind == "gateway", ApiKeyRow.revoked_at.is_(None))
            .group_by(ApiKeyRow.user_id)
            .subquery()
        )
        result = await session.execute(
            select(UserAccountRow, AiGatewayDeveloperRow, live.c.n)
            .outerjoin(AiGatewayDeveloperRow, AiGatewayDeveloperRow.user_id == UserAccountRow.id)
            .outerjoin(live, live.c.user_id == UserAccountRow.id)
            .order_by(UserAccountRow.created_at, UserAccountRow.id)
        )
        return [
            Developer(
                user_id=user.id,
                username=user.username,
                display_name=user.display_name,
                disabled=user.disabled,
                credential_profile=(
                    profile.credential_profile if profile is not None else DEFAULT_PROFILE
                ),
                live_gateway_keys=keys or 0,
                updated_at=profile.updated_at if profile is not None else None,
                updated_by=profile.updated_by if profile is not None else None,
            )
            for user, profile, keys in result.all()
        ]

    async def set_credential_profile(
        self,
        session: AsyncSession,
        user_id: str,
        profile: CredentialProfile,
        *,
        by: str | None,
        now: datetime,
    ) -> bool:
        """Set how ``user_id``'s gateway traffic is billed. ``False`` for an unknown account."""
        if await session.get(UserAccountRow, user_id) is None:
            return False
        statement = insert(AiGatewayDeveloperRow).values(
            user_id=user_id, credential_profile=profile, updated_at=now, updated_by=by
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["user_id"],
                set_={
                    "credential_profile": statement.excluded.credential_profile,
                    "updated_at": statement.excluded.updated_at,
                    "updated_by": statement.excluded.updated_by,
                },
            )
        )
        return True

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
                    "first_seen_at": kind[0].started_at,
                    "last_seen_at": latest.started_at,
                    "request_count": len(kind),
                    "client_versions": sorted({r.client_version for r in kind if r.client_version}),
                    "shape": shape.model_dump(mode="json") if shape else None,
                    "shares_main_prefix": latest.shares_main_prefix,
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
