"""Reports over the AI gateway's records: totals, groupings, sessions and their timelines.

Every figure is a sum over *units*: one per request row for the days still kept in detail, and
the per-day aggregates for the days whose request rows were archived (before ``archived_before``
on ``ai_gateway_state``). A request row's unit is ``CONTRIB`` — ``GatewayTotals.of`` translated
column by column, the one definition the gateway's own report also sums — and an aggregate row
already holds those sums, keyed by every dimension a report groups by. So a report reads the same
before and after its days are archived; the test suite checks both against the Python definition.
What only request rows hold — setup warnings, seat readings, the prices used, a session's
timeline — covers the days kept in detail. Archived days count whole: a window starting inside one
includes all of it.

Who sees what is decided by the caller (the API layer) as a ``Scope``: every record, or one
developer's. The functions here only apply it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import (
    BigInteger,
    Float,
    Select,
    and_,
    case,
    func,
    literal,
    or_,
    select,
    text,
    union_all,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute
from sqlalchemy.sql.elements import ColumnElement
from theygent_ir.gateway import TOOL_SEARCH_OFF_MIN_TOOLS, GatewayTotals, SetupWarningCode

from theygent_control_plane.models import AiGatewayDailyUsageRow as D
from theygent_control_plane.models import AiGatewayRequestRow as R
from theygent_control_plane.models import AiGatewayStateRow

#: How a group key reads when the record has no value for it (as in the gateway's own report).
NONE_KEY = "(none)"

_TOKENS = (R.input_tokens + R.output_tokens + R.cache_read_tokens) + (
    R.cache_write_5m_tokens + R.cache_write_1h_tokens
)
_ROUTED = R.decision_action == "route"
_APPLIED = and_(_ROUTED, R.decision_applied.is_(True))
_LOCAL = and_(_ROUTED, R.decision_target_kind == "local")
_FAILED = or_(R.outcome != "complete", R.status >= 400)
_COST = func.coalesce(R.cost_usd, 0.0)
_SHADOWED = R.shadow.is_not(None)
_ANSWERED = R.shadow["outcome"].astext == "answered"
_COMPARED = R.shadow["agreed"].astext.in_(("true", "false"))
_AGREED = R.shadow["agreed"].astext == "true"
_SHADOW_COST = func.coalesce(R.shadow["cost_usd"].astext.cast(Float), 0.0)
_SHADOW_OUTPUT = func.coalesce(R.shadow["usage"]["output_tokens"].astext.cast(BigInteger), 0)

#: One request row's contribution, field by field: ``GatewayTotals.of`` in SQL.
CONTRIB: dict[str, ColumnElement[Any] | InstrumentedAttribute[Any]] = {
    "calls": literal(1),
    "errors": case((_FAILED, 1), else_=0),
    "input_tokens": R.input_tokens,
    "output_tokens": R.output_tokens,
    "cache_read_tokens": R.cache_read_tokens,
    "cache_write_5m_tokens": R.cache_write_5m_tokens,
    "cache_write_1h_tokens": R.cache_write_1h_tokens,
    "cost_usd": _COST,
    "unpriced_calls": case((and_(R.cost_usd.is_(None), _TOKENS > 0), 1), else_=0),
    "baseline_usd": func.coalesce(R.baseline_usd, _COST),
    "with_routing_usd": case(
        (and_(_ROUTED, R.counterfactual_usd.is_not(None)), R.counterfactual_usd), else_=_COST
    ),
    "routed_calls": case((_ROUTED, 1), else_=0),
    "applied_calls": case((_APPLIED, 1), else_=0),
    "routed_tokens": case((_ROUTED, _TOKENS), else_=0),
    "routed_local_calls": case((_LOCAL, 1), else_=0),
    "routed_local_tokens": case((_LOCAL, _TOKENS), else_=0),
    "shadow_calls": case((_SHADOWED, 1), else_=0),
    "shadow_answered_calls": case((_ANSWERED, 1), else_=0),
    "shadow_cost_usd": case((_SHADOWED, _SHADOW_COST), else_=0.0),
    "shadow_output_tokens": case((_ANSWERED, _SHADOW_OUTPUT), else_=0),
    "shadowed_output_tokens": case((_ANSWERED, R.output_tokens), else_=0),
    "shadow_compared_calls": case((_COMPARED, 1), else_=0),
    "shadow_agreed_calls": case((_AGREED, 1), else_=0),
}
_FLOATS = frozenset({"cost_usd", "baseline_usd", "with_routing_usd", "shadow_cost_usd"})
#: The unit columns every report dimension reads, ``''`` for a missing value (as the aggregates
#: store it).
_KEYS = (
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


async def archived_before(session: AsyncSession) -> date | None:
    """The first UTC day whose request rows are all still kept; ``None``: nothing archived."""
    return (
        await session.execute(
            select(AiGatewayStateRow.archived_before).where(AiGatewayStateRow.id == 1)
        )
    ).scalar_one_or_none()


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(), UTC)


def _units(
    scope: Scope,
    since: datetime | None,
    until: datetime | None,
    archived: date | None,
    *,
    session_id: str | None = None,
) -> Any:
    """The units of a report: request rows kept in detail, and aggregates for archived days."""
    kept = select(
        func.to_char(func.timezone("UTC", R.started_at), "YYYY-MM-DD").label("day"),
        func.coalesce(R.developer_id, "").label("developer_id"),
        func.coalesce(R.client, "").label("client"),
        R.request_class.label("request_class"),
        func.coalesce(R.requested_model, "").label("requested_model"),
        func.coalesce(R.served_model, "").label("served_model"),
        case((_ROUTED, func.coalesce(R.decision_target_model, "")), else_="").label(
            "decision_target"
        ),
        func.coalesce(R.session_id, "").label("session_id"),
        func.coalesce(R.decision_reasons.op("->>")(0), "").label("reason"),
        R.started_at.label("first_at"),
        R.started_at.label("last_at"),
        *(expr.label(name) for name, expr in CONTRIB.items()),
    )
    if since is not None:
        kept = kept.where(R.started_at >= since)
    if until is not None:
        kept = kept.where(R.started_at < until)
    if archived is not None:
        kept = kept.where(R.started_at >= _midnight(archived))
    if scope.developer_id is not None:
        kept = kept.where(R.developer_id == scope.developer_id)
    if session_id is not None:
        kept = kept.where(R.session_id == session_id)
    if archived is None or (since is not None and since >= _midnight(archived)):
        return kept.subquery("units")
    summarized = select(
        func.to_char(D.day, "YYYY-MM-DD").label("day"),
        *(D.__table__.c[name] for name in _KEYS[1:]),
        D.first_at,
        D.last_at,
        *(D.__table__.c[name] for name in CONTRIB),
    ).where(D.day < archived)
    if since is not None:
        summarized = summarized.where(D.day >= since.astimezone(UTC).date())
    if until is not None:
        summarized = summarized.where(
            D.day <= (until.astimezone(UTC) - timedelta(microseconds=1)).date()
        )
    if scope.developer_id is not None:
        summarized = summarized.where(D.developer_id == scope.developer_id)
    if session_id is not None:
        summarized = summarized.where(D.session_id == session_id)
    return union_all(kept, summarized).subquery("units")


def _sums(units: Any) -> dict[str, ColumnElement[Any]]:
    """``GatewayTotals`` over units, field by field."""
    return {
        name: func.coalesce(func.sum(units.c[name]), 0.0 if name in _FLOATS else 0)
        for name in CONTRIB
    }


def _dimensions(units: Any) -> dict[str, ColumnElement[Any]]:
    """Report dimensions over units, named as in the gateway's own report, plus ``day`` (UTC)."""
    c = units.c
    return {
        "class": c.request_class,
        "client": func.nullif(c.client, ""),
        "session": func.nullif(c.session_id, ""),
        "model": func.nullif(c.requested_model, ""),
        "served-model": func.nullif(c.served_model, ""),
        "developer": func.nullif(c.developer_id, ""),
        "decision": case(
            (c.decision_target == "", literal("stay")),
            else_=literal("route:") + c.decision_target,
        ),
        "reason": func.nullif(c.reason, ""),
        "day": c.day,
    }


#: Report dimensions, named as in the gateway's own report, plus ``day`` (UTC).
DIMENSIONS: tuple[str, ...] = (
    "class",
    "client",
    "session",
    "model",
    "served-model",
    "developer",
    "decision",
    "reason",
    "day",
)
#: ``GatewayTotals`` field by field, as the report sums them.
TOTALS = CONTRIB
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
    #: Days before this one were archived: the figures sum their daily aggregates, and warnings,
    #: seats and prices cover only the days after it. ``None``: every day in detail.
    summarized_before: date | None = None
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
    archived = await archived_before(session)
    units = _units(scope, since, until, archived)
    sums = _sums(units)
    c = units.c
    total_row = (
        await session.execute(
            select(
                *(expr.label(name) for name, expr in sums.items()),
                func.min(c.first_at).label("first"),
                func.max(c.last_at).label("last"),
                func.bool_or(
                    and_(
                        c.served_model != "",
                        c.requested_model != "",
                        c.served_model != c.requested_model,
                    )
                ).label("served_differs"),
            )
        )
    ).one()
    dims = _dimensions(units)
    groups: dict[str, list[dict[str, Any]]] = {}
    for dimension in dimensions:
        key = func.coalesce(dims[dimension], NONE_KEY).label("key")
        rows = await session.execute(
            select(key, *(expr.label(name) for name, expr in sums.items()))
            .group_by(key)
            .order_by(sums["cost_usd"].desc(), sums["calls"].desc(), key)
        )
        groups[dimension] = [{"key": row.key, **totals_json(_totals(row))} for row in rows]
    # Prices, warnings and seat readings live on request rows: the days kept in detail.
    summarized = archived is not None and since < _midnight(archived)
    kept_since = max(since, _midnight(archived)) if archived is not None else since
    versions = await session.execute(
        _where(select(R.pricing_version).distinct(), scope, kept_since, until).where(
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
        warnings=await _warnings(session, scope, kept_since, until),
        seats=await _seats(session, scope, kept_since, until) if include_seats else [],
        summarized_before=archived if summarized else None,
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
    units = _units(scope, since, until, await archived_before(session))
    sums = _sums(units)
    c = units.c
    last_at = func.max(c.last_at)
    stmt = (
        select(
            c.session_id,
            func.nullif(c.developer_id, "").label("developer_id"),
            func.max(func.nullif(c.client, "")).label("client"),
            func.min(c.first_at).label("first_at"),
            last_at.label("last_at"),
            *(expr.label(name) for name, expr in sums.items()),
        )
        .where(c.session_id != "")
        .group_by(c.session_id, c.developer_id)
        .order_by(last_at.desc(), c.session_id.desc())
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
    baseline_usd: float | None
    timing: dict[str, Any]
    decision: dict[str, Any]
    #: The shadow run's outcome, usage and cost (``ShadowRun``), where one ran.
    shadow: dict[str, Any] | None = None


class SessionDetail(BaseModel):
    session_id: str
    developer_id: str | None
    totals: dict[str, Any]
    requests: list[TimelineEntry]
    #: More requests follow; ask again with ``after`` set to the last one's ``id``.
    truncated: bool
    #: Requests of this session on archived days: in its totals, not in its timeline.
    archived_requests: int = 0


async def session_detail(
    session: AsyncSession,
    session_id: str,
    *,
    scope: Scope,
    limit: int,
    after: str | None,
) -> SessionDetail | None:
    """A session's requests in the order they started (ties by id), from just past the request
    ``after`` names. ``None`` when the scope holds no request of that session. Its totals include
    requests on archived days, which its timeline no longer lists."""
    archived = await archived_before(session)
    units = _units(scope, None, None, archived, session_id=session_id)
    sums = _sums(units)
    summary = (
        await session.execute(
            select(
                func.max(func.nullif(units.c.developer_id, "")).label("developer_id"),
                *(expr.label(name) for name, expr in sums.items()),
            )
        )
    ).one()
    if not summary.calls:
        return None
    owner_filter = R.session_id == session_id
    if scope.developer_id is not None:
        owner_filter = and_(owner_filter, R.developer_id == scope.developer_id)
    if archived is not None:
        owner_filter = and_(owner_filter, R.started_at >= _midnight(archived))
    kept = (await session.execute(select(func.count()).where(owner_filter))).scalar_one()
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
        archived_requests=int(summary.calls) - int(kept),
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
        baseline_usd=row.baseline_usd,
        shadow=row.shadow,
        timing=row.timing,
        decision={
            "action": row.decision_action,
            "target": target,
            "reasons": row.decision_reasons,
            "counterfactual_usd": row.counterfactual_usd,
            "applied": row.decision_applied,
        },
    )
