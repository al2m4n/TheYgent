"""What a request cost, and what it would have cost on another model.

Prices come from ``pricing.json`` (list prices, versioned and stamped with when they were read;
``scripts/update_pricing.py`` refreshes it) with the gateway config's ``pricing_overrides`` on top
for contracted rates; every figure carries the price version it was computed with. An unknown
model has no cost — ``None``, never a guess.

Two published modifiers apply per request: a model priced by prompt length charges its
``long_context`` rates when the prompt (input, cache reads and cache writes) is over the
threshold, and a request sent with ``speed: "fast"`` pays the model's ``fast`` rates. A contracted
override keeps each tier's published ratio to the base price.

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
from typing import Any, Literal

# ``base_model`` is shared with the control plane, which checks targets with it on save.
from theygent_ir.gateway import LocalRates, ModelRates, SeatAllowance, Usage, base_model

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

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> Rates:
        return cls(
            entry["input"],
            entry["output"],
            entry["cache_write_5m"],
            entry["cache_write_1h"],
            entry["cache_read"],
        )

    def scaled(self, base: Rates, onto: Rates) -> Rates:
        """This tier's ratio to ``base``, applied to ``onto``: a tier of contracted rates."""

        def ratio(field: str) -> float:
            ours, theirs = getattr(self, field), getattr(base, field)
            return getattr(onto, field) * (ours / theirs) if theirs else ours

        return Rates(
            ratio("input"),
            ratio("output"),
            ratio("cache_write_5m"),
            ratio("cache_write_1h"),
            ratio("cache_read"),
        )


@dataclass(frozen=True)
class ModelPrices:
    """A model's rates, with the tiers it is published with."""

    base: Rates
    #: Rates for a prompt over ``long_context[0]`` tokens.
    long_context: tuple[int, Rates] | None = None
    #: Rates for a request sent with ``speed: "fast"``.
    fast: Rates | None = None

    def contracted(self, base: Rates) -> ModelPrices:
        long = self.long_context
        return ModelPrices(
            base,
            (long[0], long[1].scaled(self.base, base)) if long else None,
            self.fast.scaled(self.base, base) if self.fast else None,
        )


class PriceBook:
    def __init__(
        self,
        version: str,
        rates: Mapping[str, Rates | ModelPrices],
        *,
        source: str = "",
        updated_at: str | None = None,
    ) -> None:
        self.version = version
        self.source = source
        self.updated_at = updated_at
        self._prices = {
            model: entry if isinstance(entry, ModelPrices) else ModelPrices(entry)
            for model, entry in rates.items()
        }

    @classmethod
    def packaged(cls) -> PriceBook:
        return cls.from_document(
            json.loads(resources.files("theygent_ai_gateway").joinpath("pricing.json").read_text())
        )

    @classmethod
    def from_document(cls, data: Mapping[str, Any]) -> PriceBook:
        """A price list in ``pricing.json``'s shape."""
        prices: dict[str, ModelPrices] = {}
        for model, entry in data["models"].items():
            long = entry.get("long_context")
            fast = entry.get("fast")
            prices[model] = ModelPrices(
                Rates.from_entry(entry),
                (long["over_prompt_tokens"], Rates.from_entry(long)) if long else None,
                Rates.from_entry(fast) if fast else None,
            )
        return cls(
            data["version"], prices, source=data["source"], updated_at=data.get("updated_at")
        )

    def with_overrides(self, overrides: Mapping[str, ModelRates]) -> PriceBook:
        """Contracted rates on top of list prices. The version says overrides are in effect."""
        if not overrides:
            return self
        merged = dict(self._prices)
        for model, wire in overrides.items():
            rates = Rates.from_wire(wire)
            listed = merged.get(base_model(model))
            merged[model] = listed.contracted(rates) if listed else ModelPrices(rates)
        return PriceBook(
            f"{self.version}+overrides", merged, source=self.source, updated_at=self.updated_at
        )

    def rates(self, model: str | None) -> Rates | None:
        """A model's base rates."""
        prices = self._prices.get(base_model(model)) if model else None
        return prices.base if prices else None

    def models(self) -> list[str]:
        return sorted(self._prices)

    def has_fast_mode(self, model: str | None) -> bool:
        prices = self._prices.get(base_model(model)) if model else None
        return prices is not None and prices.fast is not None

    def cost(self, model: str | None, usage: Usage, *, fast: bool = False) -> float | None:
        """USD for ``usage`` on ``model``; ``None`` for a model without known rates."""
        prices = self._prices.get(base_model(model)) if model else None
        if prices is None:
            return None
        rates = prices.base
        prompt = usage.input_tokens + usage.cache_read_tokens
        prompt += usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
        if prices.long_context is not None and prompt > prices.long_context[0]:
            rates = prices.long_context[1]
        if fast and prices.fast is not None:
            rates = prices.fast
        return (
            usage.input_tokens * rates.input
            + usage.output_tokens * rates.output
            + usage.cache_read_tokens * rates.cache_read
            + usage.cache_write_5m_tokens * rates.cache_write_5m
            + usage.cache_write_1h_tokens * rates.cache_write_1h
        ) / PER_MILLION

    def cost_cold(
        self, model: str | None, usage: Usage, ttl: Ttl, *, fast: bool = False
    ) -> float | None:
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
        return self.cost(model, cold, fast=fast)


def local_cost(rates: LocalRates | None, usage: Usage) -> float:
    """What a local model's answer cost to run, at the rates an admin set for it; ``0.0`` for a
    model without rates. A local engine reads every prompt token, so cached or not they count as
    input."""
    if rates is None:
        return 0.0
    prompt = usage.input_tokens + usage.cache_read_tokens
    prompt += usage.cache_write_5m_tokens + usage.cache_write_1h_tokens
    return (prompt * rates.input + usage.output_tokens * rates.output) / PER_MILLION


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
