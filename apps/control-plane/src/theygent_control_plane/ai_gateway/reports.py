"""Reports over the AI gateway's records: totals, groupings, sessions and their timelines.

Totals are aggregated in SQL by ``TOTALS``, the column-by-column translation of
``GatewayTotals.of`` (the one definition the gateway's own report also sums). The test suite seeds
records and checks the SQL against the Python definition, so the two cannot drift silently.

Who sees what is decided by the caller (the API layer) as a ``Scope``: every record, or one
developer's. The functions here only apply it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import Select, and_, case, func, literal, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import QueryableAttribute
from sqlalchemy.sql.elements import ColumnElement
from theygent_ir.gateway import TOOL_SEARCH_OFF_MIN_TOOLS, GatewayTotals, SetupWarningCode

from theygent_control_plane.models import AiGatewayRequestRow as R

#: How a group key reads when the record has no value for it (as in the gateway's own report).
NONE_KEY = "(none)"

_TOKENS = (R.input_tokens + R.output_tokens + R.cache_read_tokens) + (
    R.cache_write_5m_tokens + R.cache_write_1h_tokens
)
_ROUTED = R.decision_action == "route"
_LOCAL = and_(_ROUTED, R.decision_target_kind == "local")
_FAILED = or_(R.outcome != "complete", R.status >= 400)
_COST = func.coalesce(R.cost_usd, 0.0)

#: ``GatewayTotals.of`` summed, field by field.
TOTALS: dict[str, ColumnElement[Any]] = {
    "calls": func.count(),
    "errors": func.count().filter(_FAILED),
    "input_tokens": func.coalesce(func.sum(R.input_tokens), 0),
    "output_tokens": func.coalesce(func.sum(R.output_tokens), 0),
    "cache_read_tokens": func.coalesce(func.sum(R.cache_read_tokens), 0),
    "cache_write_5m_tokens": func.coalesce(func.sum(R.cache_write_5m_tokens), 0),
    "cache_write_1h_tokens": func.coalesce(func.sum(R.cache_write_1h_tokens), 0),
    "cost_usd": func.coalesce(func.sum(_COST), 0.0),
    "unpriced_calls": func.count().filter(and_(R.cost_usd.is_(None), _TOKENS > 0)),
    "with_routing_usd": func.coalesce(
        func.sum(
            case(
                (and_(_ROUTED, R.counterfactual_usd.is_not(None)), R.counterfactual_usd),
                else_=_COST,
            )
        ),
        0.0,
    ),
    "routed_calls": func.count().filter(_ROUTED),
    "routed_tokens": func.coalesce(func.sum(case((_ROUTED, _TOKENS), else_=0)), 0),
    "routed_local_calls": func.count().filter(_LOCAL),
    "routed_local_tokens": func.coalesce(func.sum(case((_LOCAL, _TOKENS), else_=0)), 0),
}

#: Report dimensions, named as in the gateway's own report, plus ``day`` (UTC).
DIMENSIONS: dict[str, ColumnElement[Any] | QueryableAttribute[Any]] = {
    "class": R.request_class,
    "client": R.client,
    "session": R.session_id,
    "model": R.requested_model,
    "served-model": R.served_model,
    "developer": R.developer_id,
    "decision": case((_ROUTED, literal("route:") + R.decision_target_model), else_=literal("stay")),
    "reason": R.decision_reasons.op("->>")(0),
    "day": func.to_char(func.timezone("UTC", R.started_at), "YYYY-MM-DD"),
}
#: Dimensions that single out a person or their work: admin-only across the organization.
IDENTIFYING = frozenset({"developer", "session"})


@dataclass(frozen=True)
class Scope:
    """Which records a reader may see: everyone's (``developer_id`` ``None``), or one
    developer's."""

    developer_id: str | None = None


def totals_json(totals: GatewayTotals) -> dict[str, Any]:
    return {
        **totals.model_dump(),
        "cache_write_tokens": totals.cache_write_tokens,
        "saving_usd": totals.saving_usd,
    }


def _totals(row: Any) -> GatewayTotals:
    return GatewayTotals.model_validate({name: row._mapping[name] for name in TOTALS})


def _where(stmt: Select[Any], scope: Scope, since: datetime, until: datetime) -> Select[Any]:
    stmt = stmt.where(R.started_at >= since, R.started_at < until)
    if scope.developer_id is not None:
        stmt = stmt.where(R.developer_id == scope.developer_id)
    return stmt


class SetupWarning(BaseModel):
    code: SetupWarningCode
    calls: int
    #: What the warning names: the unrecognized request classes, or the unpriced models.
    values: list[str] = []


class SeatUse(BaseModel):
    """A developer's subscription seat: the latest plan-window reading, and how much of each window
    was used within the report (never read across a window reset)."""

    developer_id: str | None
    latest_at: datetime
    five_hour_utilization: float | None
    seven_day_utilization: float | None
    five_hour_resets_at: str | None
    seven_day_resets_at: str | None
    five_hour_used: float
    seven_day_used: float


class Report(BaseModel):
    since: datetime
    until: datetime
    developer_id: str | None
    first: datetime | None
    last: datetime | None
    total: dict[str, Any]
    #: Per dimension, rows ordered by spend, most first.
    groups: dict[str, list[dict[str, Any]]]
    pricing_versions: list[str]
    #: Some records were served by a model other than the one requested (Claude Code files
    #: tokens under the requested one).
    served_differs: bool
    warnings: list[SetupWarning]
    #: Per developer; empty when the reader may not see individuals.
    seats: list[SeatUse]


async def build_report(
    session: AsyncSession,
    *,
    scope: Scope,
    since: datetime,
    until: datetime,
    dimensions: Sequence[str],
    include_seats: bool = True,
) -> Report:
    total_row = (
        await session.execute(
            _where(
                select(
                    *(expr.label(name) for name, expr in TOTALS.items()),
                    func.min(R.started_at).label("first"),
                    func.max(R.started_at).label("last"),
                    func.bool_or(
                        and_(
                            R.served_model.is_not(None),
                            R.requested_model.is_not(None),
                            R.served_model != R.requested_model,
                        )
                    ).label("served_differs"),
                ),
                scope,
                since,
                until,
            )
        )
    ).one()
    groups: dict[str, list[dict[str, Any]]] = {}
    for dimension in dimensions:
        key = func.coalesce(DIMENSIONS[dimension], NONE_KEY).label("key")
        rows = await session.execute(
            _where(
                select(key, *(expr.label(name) for name, expr in TOTALS.items())),
                scope,
                since,
                until,
            )
            .group_by(key)
            .order_by(TOTALS["cost_usd"].desc(), TOTALS["calls"].desc(), key)
        )
        groups[dimension] = [{"key": row.key, **totals_json(_totals(row))} for row in rows]
    versions = await session.execute(
        _where(select(R.pricing_version).distinct(), scope, since, until).where(
            R.pricing_version.is_not(None)
        )
    )
    return Report(
        since=since,
        until=until,
        developer_id=scope.developer_id,
        first=total_row.first,
        last=total_row.last,
        total=totals_json(_totals(total_row)),
        groups=groups,
        pricing_versions=sorted(v for (v,) in versions),
        served_differs=bool(total_row.served_differs),
        warnings=await _warnings(session, scope, since, until),
        seats=await _seats(session, scope, since, until) if include_seats else [],
    )


async def _warnings(
    session: AsyncSession, scope: Scope, since: datetime, until: datetime
) -> list[SetupWarning]:
    """The same setup problems the gateway's own report detects, by the same rules."""
    tool_count = func.coalesce(R.shape["tool_count"].as_integer(), 0)
    deferred = func.coalesce(R.shape["deferred_tool_count"].as_integer(), 0)
    messages = R.endpoint == "messages"
    row = (
        await session.execute(
            _where(
                select(
                    func.count()
                    .filter(messages, deferred == 0, tool_count >= TOOL_SEARCH_OFF_MIN_TOOLS)
                    .label("tool_search_off"),
                    func.count()
                    .filter(messages, R.request_class == "unlabeled", R.client_version.is_not(None))
                    .label("hint_headers_off"),
                    func.count()
                    .filter(R.request_class == "unrecognized", R.request_class_raw.is_not(None))
                    .label("unrecognized"),
                    func.array_agg(R.request_class_raw.distinct())
                    .filter(R.request_class == "unrecognized", R.request_class_raw.is_not(None))
                    .label("unrecognized_values"),
                    func.count().filter(R.cost_usd.is_(None), _TOKENS > 0).label("unpriced"),
                    func.array_agg(
                        func.coalesce(R.served_model, R.requested_model, NONE_KEY).distinct()
                    )
                    .filter(R.cost_usd.is_(None), _TOKENS > 0)
                    .label("unpriced_values"),
                ),
                scope,
                since,
                until,
            )
        )
    ).one()
    found: list[SetupWarning] = []
    if row.tool_search_off:
        found.append(SetupWarning(code="tool_search_off", calls=row.tool_search_off))
    if row.hint_headers_off:
        found.append(SetupWarning(code="hint_headers_off", calls=row.hint_headers_off))
    if row.unrecognized:
        found.append(
            SetupWarning(
                code="unrecognized_request_class",
                calls=row.unrecognized,
                values=sorted(row.unrecognized_values or []),
            )
        )
    if row.unpriced:
        found.append(
            SetupWarning(
                code="unpriced_models", calls=row.unpriced, values=sorted(row.unpriced_values or [])
            )
        )
    return found


#: Seat readings in order per developer: the latest one, and each window's increases between
#: consecutive readings that share a reset time (a reset makes them incomparable).
_SEATS = text(
    """
    WITH readings AS (
        SELECT developer_id, started_at, id,
               (seat_allowance->>'five_hour_utilization')::float AS u5,
               (seat_allowance->>'seven_day_utilization')::float AS u7,
               seat_allowance->>'five_hour_resets_at' AS r5,
               seat_allowance->>'seven_day_resets_at' AS r7
        FROM ai_gateway_request
        WHERE seat_allowance IS NOT NULL
          AND started_at >= :since AND started_at < :until
          AND (CAST(:developer AS text) IS NULL OR developer_id = :developer)
    ), paired AS (
        SELECT *,
               LAG(u5) OVER w AS p5, LAG(r5) OVER w AS pr5,
               LAG(u7) OVER w AS p7, LAG(r7) OVER w AS pr7,
               ROW_NUMBER() OVER (
                   PARTITION BY developer_id ORDER BY started_at DESC, id DESC
               ) AS newest
        FROM readings
        WINDOW w AS (PARTITION BY developer_id ORDER BY started_at, id)
    )
    SELECT developer_id,
           MAX(started_at) FILTER (WHERE newest = 1) AS latest_at,
           MAX(u5) FILTER (WHERE newest = 1) AS u5,
           MAX(u7) FILTER (WHERE newest = 1) AS u7,
           MAX(r5) FILTER (WHERE newest = 1) AS r5,
           MAX(r7) FILTER (WHERE newest = 1) AS r7,
           COALESCE(SUM(GREATEST(u5 - p5, 0)) FILTER (
               WHERE u5 IS NOT NULL AND p5 IS NOT NULL AND pr5 IS NOT NULL AND r5 = pr5
           ), 0) AS used5,
           COALESCE(SUM(GREATEST(u7 - p7, 0)) FILTER (
               WHERE u7 IS NOT NULL AND p7 IS NOT NULL AND pr7 IS NOT NULL AND r7 = pr7
           ), 0) AS used7
    FROM paired
    GROUP BY developer_id
    ORDER BY developer_id NULLS LAST
    """
)


async def _seats(
    session: AsyncSession, scope: Scope, since: datetime, until: datetime
) -> list[SeatUse]:
    rows = await session.execute(
        _SEATS, {"since": since, "until": until, "developer": scope.developer_id}
    )
    return [
        SeatUse(
            developer_id=row.developer_id,
            latest_at=row.latest_at,
            five_hour_utilization=row.u5,
            seven_day_utilization=row.u7,
            five_hour_resets_at=row.r5,
            seven_day_resets_at=row.r7,
            five_hour_used=row.used5,
            seven_day_used=row.used7,
        )
        for row in rows
    ]


class SessionSummary(BaseModel):
    session_id: str
    developer_id: str | None
    client: str | None
    first_at: datetime
    last_at: datetime
    totals: dict[str, Any]


async def list_sessions(
    session: AsyncSession,
    *,
    scope: Scope,
    since: datetime,
    until: datetime,
    limit: int,
    before: datetime | None,
) -> list[SessionSummary]:
    """Sessions with activity in the range, the most recently active first. ``before`` pages:
    sessions last active before that instant."""
    last_at = func.max(R.started_at)
    stmt = (
        _where(
            select(
                R.session_id,
                R.developer_id,
                func.max(R.client).label("client"),
                func.min(R.started_at).label("first_at"),
                last_at.label("last_at"),
                *(expr.label(name) for name, expr in TOTALS.items()),
            ),
            scope,
            since,
            until,
        )
        .where(R.session_id.is_not(None))
        .group_by(R.session_id, R.developer_id)
        .order_by(last_at.desc(), R.session_id.desc())
        .limit(limit)
    )
    if before is not None:
        stmt = stmt.having(last_at < before)
    rows = await session.execute(stmt)
    return [
        SessionSummary(
            session_id=row.session_id,
            developer_id=row.developer_id,
            client=row.client,
            first_at=row.first_at,
            last_at=row.last_at,
            totals=totals_json(_totals(row)),
        )
        for row in rows
    ]


class TimelineEntry(BaseModel):
    """One exchange in a session, with what the policy would have done."""

    id: str
    started_at: datetime
    request_class: str
    agent_id: str | None
    agent_type: str | None
    prompt_id: str | None
    endpoint: str
    status: int | None
    outcome: str
    error_type: str | None
    requested_model: str | None
    served_model: str | None
    stop_reason: str | None
    compaction: str | None
    shares_main_prefix: bool | None
    usage: dict[str, int]
    cost_usd: float | None
    timing: dict[str, Any]
    decision: dict[str, Any]


class SessionDetail(BaseModel):
    session_id: str
    developer_id: str | None
    totals: dict[str, Any]
    requests: list[TimelineEntry]
    #: More requests follow; ask again with ``after`` set to the last one's ``id``.
    truncated: bool


async def session_detail(
    session: AsyncSession,
    session_id: str,
    *,
    scope: Scope,
    limit: int,
    after: str | None,
) -> SessionDetail | None:
    """A session's requests in the order they started (ties by id), from just past the request
    ``after`` names. ``None`` when the scope holds no request of that session."""
    owner_filter = R.session_id == session_id
    if scope.developer_id is not None:
        owner_filter = and_(owner_filter, R.developer_id == scope.developer_id)
    summary = (
        await session.execute(
            select(
                func.max(R.developer_id).label("developer_id"),
                *(expr.label(name) for name, expr in TOTALS.items()),
            ).where(owner_filter)
        )
    ).one()
    if not summary.calls:
        return None
    stmt = select(R).where(owner_filter).order_by(R.started_at, R.id).limit(limit + 1)
    if after is not None:
        cursor = select(R.started_at).where(R.id == after).scalar_subquery()
        stmt = stmt.where(or_(R.started_at > cursor, and_(R.started_at == cursor, R.id > after)))
    rows = list((await session.execute(stmt)).scalars())
    return SessionDetail(
        session_id=session_id,
        developer_id=summary.developer_id,
        totals=totals_json(_totals(summary)),
        requests=[_entry(row) for row in rows[:limit]],
        truncated=len(rows) > limit,
    )


def _entry(row: R) -> TimelineEntry:
    target = (
        {"kind": row.decision_target_kind, "model": row.decision_target_model}
        if row.decision_target_kind is not None
        else None
    )
    return TimelineEntry(
        id=row.id,
        started_at=row.started_at,
        request_class=row.request_class,
        agent_id=row.agent_id,
        agent_type=row.agent_type,
        prompt_id=row.prompt_id,
        endpoint=row.endpoint,
        status=row.status,
        outcome=row.outcome,
        error_type=row.error_type,
        requested_model=row.requested_model,
        served_model=row.served_model,
        stop_reason=row.stop_reason,
        compaction=row.compaction,
        shares_main_prefix=row.shares_main_prefix,
        usage={
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "cache_read_tokens": row.cache_read_tokens,
            "cache_write_5m_tokens": row.cache_write_5m_tokens,
            "cache_write_1h_tokens": row.cache_write_1h_tokens,
        },
        cost_usd=row.cost_usd,
        timing=row.timing,
        decision={
            "action": row.decision_action,
            "target": target,
            "reasons": row.decision_reasons,
            "counterfactual_usd": row.counterfactual_usd,
            "applied": row.decision_applied,
        },
    )
