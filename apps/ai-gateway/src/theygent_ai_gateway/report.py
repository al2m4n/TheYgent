"""The observer report, built from the local request log.

It answers what the gateway was put on the path to answer: what the traffic cost, what it would
have cost had the policy's decisions been applied, how many API calls and tokens routing would
have moved (to a cheaper model, or off the API to a local one), and what is wrong with the setup.

Every figure comes from the records as written: each record carries its own cost (priced when it
was recorded, with the config's contracted rates) and its decision's counterfactual, so the report
never re-prices anything. "With routing" is a record's counterfactual where its decision was to
route, and its actual cost otherwise. On a subscription the dollar figures are API-equivalent
value; the seat's plan usage is reported from its utilization readings.

Claude Code files every token under the model it *requested*, so grouping by ``model`` matches
Claude Code's own ``modelUsage``; ``served-model`` shows where tokens actually went.

The log is read from disk without the gateway's cooperation: a line still being written (the last
one, without its newline) is not read yet, and a line that is not a record is counted and skipped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from pydantic import ValidationError
from theygent_ir.gateway import GatewayRequestRecord, GatewayTotals, SeatAllowance

from theygent_ai_gateway.costs import utilization_change

#: How a group key reads when the record has no value for it.
NONE_KEY = "(none)"
#: Claude Code sends about a dozen built-in tools, one of them deferred. Dozens of tools with none
#: deferred means every MCP tool schema went upfront: tool search is off.
TOOL_SEARCH_OFF_MIN_TOOLS = 30

DIMENSIONS: dict[str, Callable[[GatewayRequestRecord], str | None]] = {
    "class": lambda r: r.request_class,
    "client": lambda r: r.client,
    "session": lambda r: r.session_id,
    "model": lambda r: r.requested_model,
    "served-model": lambda r: r.served_model,
    "developer": lambda r: r.developer_id,
    "decision": lambda r: (
        f"route:{r.decision.target.model}"
        if r.decision.action == "route" and r.decision.target
        else "stay"
    ),
    "reason": lambda r: r.decision.reasons[0],
}
DEFAULT_DIMENSIONS = ("class", "client", "model")


def totals_json(totals: GatewayTotals) -> dict[str, object]:
    return {
        **totals.model_dump(),
        "cache_write_tokens": totals.cache_write_tokens,
        "saving_usd": totals.saving_usd,
    }


@dataclass(frozen=True)
class SetupWarning:
    code: str
    message: str
    calls: int


@dataclass
class SeatUse:
    """A developer's seat: the latest utilization reading, and how much of each window was used
    within the report (readings across a window reset are not compared)."""

    latest: SeatAllowance
    latest_at: datetime
    five_hour_used: float = 0.0
    seven_day_used: float = 0.0


@dataclass
class Report:
    since: datetime | None
    until: datetime | None
    first: datetime | None = None
    last: datetime | None = None
    total: GatewayTotals = field(default_factory=GatewayTotals)
    groups: dict[str, dict[str, GatewayTotals]] = field(default_factory=dict)
    seats: dict[str, SeatUse] = field(default_factory=dict)
    warnings: list[SetupWarning] = field(default_factory=list)
    pricing_versions: set[str] = field(default_factory=set)
    #: Some records were served by a model other than the one requested.
    served_differs: bool = False
    unreadable_lines: int = 0

    def to_json(self) -> dict[str, object]:
        def when(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "since": when(self.since),
            "until": when(self.until),
            "first": when(self.first),
            "last": when(self.last),
            "total": totals_json(self.total),
            "groups": {
                dimension: {key: totals_json(totals) for key, totals in rows.items()}
                for dimension, rows in self.groups.items()
            },
            "seats": {
                developer: {
                    "latest": seat.latest.model_dump(mode="json"),
                    "latest_at": when(seat.latest_at),
                    "five_hour_used": seat.five_hour_used,
                    "seven_day_used": seat.seven_day_used,
                }
                for developer, seat in self.seats.items()
            },
            "warnings": [asdict(w) for w in self.warnings],
            "pricing_versions": sorted(self.pricing_versions),
            "served_differs": self.served_differs,
            "unreadable_lines": self.unreadable_lines,
        }


class LogReader:
    """Every record in the log, oldest segment first. A line still being written is left for
    later; a line that is not a record is counted in ``unreadable`` and skipped."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.unreadable = 0

    def __iter__(self) -> Iterator[GatewayRequestRecord]:
        for path in sorted(self.directory.glob("requests-*.jsonl")):
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                continue  # removed by the gateway's retention since the listing
            for line in data.split(b"\n")[:-1]:
                if not line.strip():
                    continue
                try:
                    yield GatewayRequestRecord.model_validate_json(line)
                except ValidationError:
                    self.unreadable += 1


def build_report(
    records: Iterable[GatewayRequestRecord],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    dimensions: Iterable[str] = DEFAULT_DIMENSIONS,
) -> Report:
    chosen = list(dimensions)
    unknown = [d for d in chosen if d not in DIMENSIONS]
    if unknown:
        raise ValueError(f"unknown dimension(s): {', '.join(unknown)}")
    report = Report(since=since, until=until, groups={d: {} for d in chosen})
    tool_search_off = hints_off = 0
    unrecognized: set[str] = set()
    unrecognized_calls = 0
    unpriced_models: set[str] = set()
    readings: dict[str, list[tuple[datetime, SeatAllowance]]] = {}

    for r in records:
        if since is not None and r.started_at < since:
            continue
        if until is not None and r.started_at >= until:
            continue
        report.first = r.started_at if report.first is None else min(report.first, r.started_at)
        report.last = r.started_at if report.last is None else max(report.last, r.started_at)
        contribution = GatewayTotals.of(r)
        report.total = report.total + contribution
        for dimension in chosen:
            key = DIMENSIONS[dimension](r) or NONE_KEY
            rows = report.groups[dimension]
            rows[key] = rows.get(key, GatewayTotals()) + contribution
        if r.pricing_version:
            report.pricing_versions.add(r.pricing_version)
        if r.served_model and r.requested_model and r.served_model != r.requested_model:
            report.served_differs = True
        if contribution.unpriced_calls:
            unpriced_models.add(r.served_model or r.requested_model or NONE_KEY)
        if r.endpoint == "messages":
            shape = r.shape
            if (
                shape is not None
                and shape.deferred_tool_count == 0
                and shape.tool_count >= TOOL_SEARCH_OFF_MIN_TOOLS
            ):
                tool_search_off += 1
            # Only Claude Code's user-agent carries a version, so this is Claude Code traffic.
            if r.request_class == "unlabeled" and r.client_version is not None:
                hints_off += 1
        if r.request_class == "unrecognized" and r.request_class_raw:
            unrecognized.add(r.request_class_raw)
            unrecognized_calls += 1
        if r.seat_allowance is not None:
            readings.setdefault(r.developer_id or NONE_KEY, []).append(
                (r.started_at, r.seat_allowance)
            )

    for developer, seen in readings.items():
        seen.sort(key=lambda reading: reading[0])
        seat = SeatUse(latest=seen[-1][1], latest_at=seen[-1][0])
        for (_, before), (_, after) in pairwise(seen):
            change = utilization_change(before, after)
            seat.five_hour_used += change.five_hour or 0.0
            seat.seven_day_used += change.seven_day or 0.0
        report.seats[developer] = seat

    if tool_search_off:
        report.warnings.append(
            SetupWarning(
                "tool_search_off",
                "Claude Code sent every tool schema upfront: tool search is off, which a custom "
                "base URL does by default and which multiplies input tokens. Set "
                "ENABLE_TOOL_SEARCH=true (the desktop app: toolSearchEnabled).",
                tool_search_off,
            )
        )
    if hints_off:
        report.warnings.append(
            SetupWarning(
                "hint_headers_off",
                "Claude Code is not sending routing hints, so requests cannot be told apart "
                "(main, subagent, compaction…) and none is a routing candidate. Set "
                "CLAUDE_CODE_GATEWAY_HINT_HEADERS=1.",
                hints_off,
            )
        )
    if unrecognized:
        report.warnings.append(
            SetupWarning(
                "unrecognized_request_class",
                f"Claude Code sent request classes this gateway does not know "
                f"({', '.join(sorted(unrecognized))}); they pass through unrouted. Update the "
                "gateway.",
                unrecognized_calls,
            )
        )
    if report.total.unpriced_calls:
        report.warnings.append(
            SetupWarning(
                "unpriced_models",
                f"No price is known for {', '.join(sorted(unpriced_models))}; those calls are "
                "left out of the dollar figures. Add them to the config's pricing_overrides.",
                report.total.unpriced_calls,
            )
        )
    return report


def report_from_log(
    directory: Path,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    dimensions: Iterable[str] = DEFAULT_DIMENSIONS,
) -> Report:
    reader = LogReader(directory)
    report = build_report(reader, since=since, until=until, dimensions=dimensions)
    report.unreadable_lines = reader.unreadable
    if reader.unreadable:
        report.warnings.append(
            SetupWarning(
                "unreadable_lines",
                "Some lines in the request log are not records (a write cut short by a crash); "
                "they were skipped.",
                reader.unreadable,
            )
        )
    return report


_DURATION = re.compile(r"^(\d+)([mhdw])$")
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_when(value: str, *, now: datetime | None = None) -> datetime:
    """``30m``, ``24h``, ``7d``, ``2w`` (that long ago), or an ISO date or datetime (UTC unless it
    names a zone)."""
    match = _DURATION.match(value.strip())
    if match:
        moment = now if now is not None else datetime.now(UTC)
        return moment - timedelta(**{_UNITS[match[2]]: int(match[1])})
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        raise ValueError(
            f"{value!r} is neither a duration (30m, 24h, 7d, 2w) nor an ISO date"
        ) from None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ── text rendering ───────────────────────────────────────────────────────────


def _usd(value: float) -> str:
    return f"${value:,.4f}"


def _int(value: int) -> str:
    return f"{value:,}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(headers)]

    def line(cells: list[str]) -> str:
        first = cells[0].ljust(widths[0])
        rest = [cell.rjust(width) for cell, width in zip(cells[1:], widths[1:], strict=True)]
        return "  ".join([first, *rest]).rstrip()

    return [line(headers), *(line(row) for row in rows)]


def render_text(report: Report, *, top: int = 20) -> str:
    total = report.total
    out: list[str] = ["TheYgent AI gateway — observer report"]
    if not total.calls:
        out.append("No records in this window.")
        out.extend(f"! {w.message}" for w in report.warnings)
        return "\n".join(out) + "\n"
    first = report.first.strftime("%Y-%m-%d %H:%M") if report.first else "?"
    last = report.last.strftime("%Y-%m-%d %H:%M") if report.last else "?"
    out.append(f"Window: {first} → {last} UTC · {_int(total.calls)} calls")
    if report.pricing_versions:
        out.append(f"Prices: {', '.join(sorted(report.pricing_versions))}")
    saving_share = total.saving_usd / total.cost_usd if total.cost_usd else 0.0
    unpriced = f" ({total.unpriced_calls} calls unpriced)" if total.unpriced_calls else ""
    out += [
        "",
        f"Spend         {_usd(total.cost_usd)}{unpriced}",
        f"With routing  {_usd(total.with_routing_usd)}",
        f"Saving        {_usd(total.saving_usd)} ({saving_share:.1%})",
        f"Calls         {_int(total.calls)} ({_int(total.errors)} errors) · would route "
        f"{_int(total.routed_calls)} · API calls avoided {_int(total.routed_local_calls)}",
        f"Tokens        input {_int(total.input_tokens)} · output {_int(total.output_tokens)} · "
        f"cache read {_int(total.cache_read_tokens)} · cache write "
        f"{_int(total.cache_write_tokens)} (5m {_int(total.cache_write_5m_tokens)}, "
        f"1h {_int(total.cache_write_1h_tokens)})",
        f"Tokens moved  {_int(total.routed_tokens)} to another model, "
        f"{_int(total.routed_local_tokens)} of them off the API",
    ]
    if report.seats:
        out += ["", "Seat allowance"]
        out += [
            "  " + line
            for line in _table(
                ["developer", "5-hour", "7-day", "used here (5h)", "used here (7d)"],
                [
                    [
                        developer,
                        _pct(seat.latest.five_hour_utilization),
                        _pct(seat.latest.seven_day_utilization),
                        f"+{_pct(seat.five_hour_used)}",
                        f"+{_pct(seat.seven_day_used)}",
                    ]
                    for developer, seat in sorted(report.seats.items())
                ],
            )
        ]
    for dimension, rows in report.groups.items():
        ranked = sorted(rows.items(), key=lambda item: (-item[1].cost_usd, -item[1].calls))
        out += ["", f"By {dimension}"]
        out += [
            "  " + line
            for line in _table(
                [
                    dimension,
                    "calls",
                    "input",
                    "output",
                    "cache read",
                    "cache write",
                    "spend",
                    "with routing",
                    "saving",
                ],
                [
                    [
                        key,
                        _int(t.calls),
                        _int(t.input_tokens),
                        _int(t.output_tokens),
                        _int(t.cache_read_tokens),
                        _int(t.cache_write_tokens),
                        _usd(t.cost_usd),
                        _usd(t.with_routing_usd),
                        _usd(t.saving_usd),
                    ]
                    for key, t in ranked[:top]
                ],
            )
        ]
        if len(ranked) > top:
            out.append(f"  … {len(ranked) - top} more")
        if dimension == "model" and report.served_differs:
            out.append(
                "  Claude Code files tokens under the model it requested, as this table does; "
                "see served-model for where they went."
            )
    if report.warnings:
        out += ["", "Setup warnings"]
        out += [f"  ! {w.message} ({_int(w.calls)} calls)" for w in report.warnings]
    return "\n".join(out) + "\n"


def render_json(report: Report) -> str:
    return json.dumps(report.to_json(), indent=2) + "\n"
