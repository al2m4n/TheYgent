"""What a request cost, and what it would have cost on another model.

Prices come from ``pricing.json`` (list prices, versioned and dated) with the gateway config's
``pricing_overrides`` on top for contracted rates; every figure carries the price version it was
computed with. An unknown model has no cost — ``None``, never a guess.

Two costs matter for routing:

* ``cost`` — the request as it ran: input, output, cache reads, and cache writes at their TTL's
  rate (one-hour writes cost more than five-minute ones);
* ``cost_cold`` — the same request on a model whose cache holds nothing for this conversation:
  every token it read from cache must be written instead. That is what moving a warm
  conversation to another model costs on its first request there.

Counterfactuals assume the other model would have used the same number of tokens. Output length
differs between models, so a counterfactual is an estimate; shadow runs calibrate it later.

On a subscription the dollar figure is an API-equivalent value, not a bill: a seat is charged in
plan allowance. ``utilization_change`` reads that from two consecutive seat readings. Allowance is
shared with everything else the seat does (including claude.ai chat), so attributing it to
requests is a report-time aggregate, not a per-request fact.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from importlib import resources
from typing import Literal

# ``base_model`` is shared with the control plane, which checks targets with it on save.
from theygent_ir.gateway import ModelRates, SeatAllowance, Usage, base_model

PER_MILLION = 1_000_000
Ttl = Literal["5m", "1h"]


@dataclass(frozen=True)
class Rates:
    """USD per million tokens."""

    input: float
    output: float
    cache_write_5m: float
    cache_write_1h: float
    cache_read: float

    @classmethod
    def from_wire(cls, rates: ModelRates) -> Rates:
        return cls(
            rates.input, rates.output, rates.cache_write_5m, rates.cache_write_1h, rates.cache_read
        )


class PriceBook:
    def __init__(self, version: str, rates: Mapping[str, Rates], *, source: str = "") -> None:
        self.version = version
        self.source = source
        self._rates = dict(rates)

    @classmethod
    def packaged(cls) -> PriceBook:
        data = json.loads(
            resources.files("theygent_ai_gateway").joinpath("pricing.json").read_text()
        )
        rates = {
            model: Rates(
                entry["input"],
                entry["output"],
                entry["cache_write_5m"],
                entry["cache_write_1h"],
                entry["cache_read"],
            )
            for model, entry in data["models"].items()
        }
        return cls(data["version"], rates, source=data["source"])

    def with_overrides(self, overrides: Mapping[str, ModelRates]) -> PriceBook:
        """Contracted rates on top of list prices. The version says overrides are in effect."""
        if not overrides:
            return self
        merged = {**self._rates, **{m: Rates.from_wire(r) for m, r in overrides.items()}}
        return PriceBook(f"{self.version}+overrides", merged, source=self.source)

    def rates(self, model: str | None) -> Rates | None:
        if not model:
            return None
        return self._rates.get(base_model(model))

    def models(self) -> list[str]:
        return sorted(self._rates)

    def cost(self, model: str | None, usage: Usage) -> float | None:
        """USD for ``usage`` on ``model``; ``None`` for a model without known rates."""
        rates = self.rates(model)
        if rates is None:
            return None
        return (
            usage.input_tokens * rates.input
            + usage.output_tokens * rates.output
            + usage.cache_read_tokens * rates.cache_read
            + usage.cache_write_5m_tokens * rates.cache_write_5m
            + usage.cache_write_1h_tokens * rates.cache_write_1h
        ) / PER_MILLION

    def cost_cold(self, model: str | None, usage: Usage, ttl: Ttl) -> float | None:
        """USD for ``usage`` on ``model`` with nothing of it cached there yet: the tokens read
        from cache are written at ``ttl``'s rate instead."""
        moved = usage.cache_read_tokens
        cold = Usage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=0,
            cache_write_5m_tokens=usage.cache_write_5m_tokens + (moved if ttl == "5m" else 0),
            cache_write_1h_tokens=usage.cache_write_1h_tokens + (moved if ttl == "1h" else 0),
        )
        return self.cost(model, cold)


def ttl_for(seconds: int) -> Ttl:
    """The marker TTL a cache lifetime corresponds to."""
    return "1h" if seconds > 300 else "5m"


@dataclass(frozen=True)
class UtilizationChange:
    """How much of a seat's plan windows were used between two readings. ``None`` for a window
    that reset in between (or that a reading lacks): no change can be read across a reset."""

    five_hour: float | None
    seven_day: float | None


def utilization_change(before: SeatAllowance, after: SeatAllowance) -> UtilizationChange:
    def window(
        old: float | None, new: float | None, old_reset: object, new_reset: object
    ) -> float | None:
        if old is None or new is None or old_reset is None or old_reset != new_reset:
            return None
        return max(new - old, 0.0)

    return UtilizationChange(
        five_hour=window(
            before.five_hour_utilization,
            after.five_hour_utilization,
            before.five_hour_resets_at,
            after.five_hour_resets_at,
        ),
        seven_day=window(
            before.seven_day_utilization,
            after.seven_day_utilization,
            before.seven_day_resets_at,
            after.seven_day_resets_at,
        ),
    )
