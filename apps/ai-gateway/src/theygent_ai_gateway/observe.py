"""What the gateway learns from a response without changing it, and the per-exchange
``Observation`` it hands to the observe hook.

The response parser reads a copy of the bytes being relayed. It never raises and never holds the
stream back: a chunk it cannot make sense of marks it failed (with the failure's name) and it
stops parsing, while the relay carries on untouched. Its memory is bounded — an event that never
ends, or a non-streaming body past the cap, is a parse failure, not a growing buffer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from theygent_ir.gateway import Endpoint, Outcome, SeatAllowance, Timing, Usage

from theygent_ai_gateway.identity import Identity
from theygent_ai_gateway.signals import RequestSignals, token_or_none

if TYPE_CHECKING:
    from theygent_ai_gateway.conversations import Position

log = logging.getLogger(__name__)

#: An SSE event larger than this without a boundary is not something to keep buffering.
MAX_PENDING_EVENT = 1 << 20
#: Non-streaming bodies are parsed whole, up to this size.
MAX_JSON_BODY = 4 << 20
_RATE_LIMIT_PREFIX = "anthropic-ratelimit-unified-"
_TOOL_BLOCKS = frozenset({"tool_use", "server_tool_use"})


@dataclass(frozen=True)
class ResponseSummary:
    model: str | None = None
    usage: Usage = field(default_factory=Usage)
    stop_reason: str | None = None
    tool_use_count: int = 0
    error_type: str | None = None
    events: int = 0
    parse_error: str | None = None


def _count(value: Any) -> int:
    return value if isinstance(value, int) and value > 0 else 0


def to_usage(raw: Mapping[str, Any]) -> Usage:
    """The upstream's usage object as a ``Usage``, with cache writes split by TTL. Without the
    per-TTL breakdown, writes count as 5-minute (the default TTL)."""
    breakdown = raw.get("cache_creation")
    creation: Mapping[str, Any] = breakdown if isinstance(breakdown, dict) else {}
    total_write = _count(raw.get("cache_creation_input_tokens"))
    write_1h = _count(creation.get("ephemeral_1h_input_tokens"))
    write_5m = (
        _count(creation["ephemeral_5m_input_tokens"])
        if "ephemeral_5m_input_tokens" in creation
        else max(total_write - write_1h, 0)
    )
    return Usage(
        input_tokens=_count(raw.get("input_tokens")),
        output_tokens=_count(raw.get("output_tokens")),
        cache_read_tokens=_count(raw.get("cache_read_input_tokens")),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


class ResponseObserver:
    """Incremental reader for one response: a Messages SSE stream or a JSON body."""

    def __init__(self, content_type: str | None, content_encoding: str | None = None) -> None:
        self._sse = (content_type or "").startswith("text/event-stream")
        encoded = content_encoding not in (None, "", "identity")
        self._failed: str | None = "encoded_body" if encoded else None
        self._pending = b""
        self._json = bytearray()
        self._usage: dict[str, Any] = {}
        self._model: str | None = None
        self._stop_reason: str | None = None
        self._tool_uses = 0
        self._error_type: str | None = None
        self._events = 0

    def feed(self, chunk: bytes) -> None:
        if self._failed is not None:
            return
        try:
            if self._sse:
                self._feed_sse(chunk)
            elif len(self._json) + len(chunk) > MAX_JSON_BODY:
                self._failed = "body_too_large"
            else:
                self._json += chunk
        except Exception as exc:
            self._failed = f"parse:{type(exc).__name__}"

    def _feed_sse(self, chunk: bytes) -> None:
        # A CR/LF pair split across chunks normalises once both halves are here.
        self._pending = (self._pending + chunk).replace(b"\r\n", b"\n")
        while (end := self._pending.find(b"\n\n")) >= 0:
            raw, self._pending = self._pending[:end], self._pending[end + 2 :]
            self._event(raw)
        if len(self._pending) > MAX_PENDING_EVENT:
            self._failed = "event_too_large"
            self._pending = b""

    def _event(self, raw: bytes) -> None:
        name, data_lines = "message", []
        for line in raw.decode("utf-8").split("\n"):
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines and not raw.strip():
            return
        self._events += 1
        if not data_lines:
            return
        self._apply(name, json.loads("\n".join(data_lines)))

    def _apply(self, name: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        kind = data.get("type", name)
        if kind == "message_start":
            message = data.get("message") or {}
            self._model = message.get("model")
            self._usage.update(message.get("usage") or {})
        elif kind == "message_delta":
            self._usage.update(data.get("usage") or {})
            if reason := (data.get("delta") or {}).get("stop_reason"):
                self._stop_reason = reason
        elif kind == "content_block_start":
            if (data.get("content_block") or {}).get("type") in _TOOL_BLOCKS:
                self._tool_uses += 1
        elif kind == "error":
            self._error_type = (data.get("error") or {}).get("type")

    def _finish_json(self) -> None:
        data = json.loads(bytes(self._json)) if self._json else None
        if not isinstance(data, dict):
            return
        if data.get("type") == "error":
            self._error_type = (data.get("error") or {}).get("type")
            return
        self._model = data.get("model")
        self._usage.update(data.get("usage") or {})
        self._stop_reason = data.get("stop_reason")
        self._tool_uses = sum(
            1
            for block in data.get("content") or []
            if isinstance(block, dict) and block.get("type") in _TOOL_BLOCKS
        )

    def finish(self) -> ResponseSummary:
        if self._failed is None and not self._sse:
            try:
                self._finish_json()
            except Exception as exc:
                self._failed = f"parse:{type(exc).__name__}"
        try:
            usage = to_usage(self._usage)
        except Exception:
            usage = Usage()
        return ResponseSummary(
            model=token_or_none(self._model) if isinstance(self._model, str) else None,
            usage=usage,
            stop_reason=token_or_none(self._stop_reason)
            if isinstance(self._stop_reason, str)
            else None,
            tool_use_count=self._tool_uses,
            error_type=token_or_none(self._error_type)
            if isinstance(self._error_type, str)
            else None,
            events=self._events,
            parse_error=self._failed,
        )


def seat_allowance(headers: Mapping[str, str]) -> SeatAllowance | None:
    """A subscription seat's plan usage from the unified rate-limit headers; ``None`` when the
    response carries none (an API-key request)."""
    values = {
        name[len(_RATE_LIMIT_PREFIX) :]: value
        for name, value in headers.items()
        if name.lower().startswith(_RATE_LIMIT_PREFIX)
    }
    if not values:
        return None

    def fraction(key: str) -> float | None:
        try:
            number = float(values[key])
        except (KeyError, ValueError):
            return None
        return number if number >= 0 else None

    def moment(key: str) -> datetime | None:
        try:
            return datetime.fromtimestamp(int(values[key]), UTC)
        except (KeyError, ValueError, OverflowError, OSError):
            return None

    return SeatAllowance(
        five_hour_utilization=fraction("5h-utilization"),
        seven_day_utilization=fraction("7d-utilization"),
        five_hour_resets_at=moment("5h-reset"),
        seven_day_resets_at=moment("7d-reset"),
        status=token_or_none(values.get("status")),
        representative_claim=token_or_none(values.get("representative-claim")),
        overage_status=token_or_none(values.get("overage-status")),
    )


@dataclass(frozen=True)
class Observation:
    """One exchange as the gateway saw it, handed to the observe hook when it ends."""

    started_at: datetime
    method: str
    endpoint: Endpoint
    request: RequestSignals
    status: int | None
    outcome: Outcome
    timing: Timing
    response: ResponseSummary
    seat_allowance: SeatAllowance | None
    #: The developer, in team mode; ``None`` in personal mode or when the request was refused.
    identity: Identity | None = None
    #: Where a Messages request sat in its conversation (``None`` for other endpoints).
    position: Position | None = None
    #: Estimated USD at the price book's rates (API-equivalent on a subscription); ``None`` when
    #: the model has no known rates or nothing was used.
    cost_usd: float | None = None
    pricing_version: str | None = None


ObserveHook = Callable[[Observation], None]


def deliver(hook: ObserveHook | None, observation: Observation) -> None:
    """Hand an observation to the hook. A failing hook is logged by type and swallowed: what the
    gateway records must never break what it relays."""
    if hook is None:
        return
    try:
        hook(observation)
    except Exception as exc:
        log.error("the observe hook failed: %s", type(exc).__name__)
