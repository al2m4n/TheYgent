"""The cost model: list prices, contracted overrides, cold-cache counterfactuals, seat allowance.

The figures that matter are pinned against the numbers the routing plan rests on: the Phase 0
pair (the same Explore subagent work on Opus 5.5 and Sonnet 5.5) and the plan's worked example of
a mid-conversation switch costing more than it saves.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import httpx
import pytest
from _fake_anthropic import fake_anthropic, serve
from _fixtures import load
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.costs import PriceBook, Rates, ttl_for, utilization_change
from theygent_ai_gateway.observe import Observation, ResponseObserver
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ir.gateway import ModelRates, SeatAllowance, Usage

PRICES = PriceBook.packaged()


def usage_of(name: str) -> tuple[str | None, Usage]:
    exchange = load(name)
    observer = ResponseObserver(exchange.response_header("content-type"))
    observer.feed(exchange.response_bytes())
    summary = observer.finish()
    return summary.model, summary.usage


# ── the numbers the plan rests on ────────────────────────────────────────────


def test_the_phase_0_subagent_pair() -> None:
    def total(prefix: str) -> float:
        costs = [PRICES.cost(*usage_of(f"{prefix}-{n}")) for n in (1, 2, 3)]
        assert all(c is not None for c in costs)
        return sum(c for c in costs if c is not None)

    opus, sonnet = total("subagent-explore"), total("subagent-explore-sonnet")
    assert opus == pytest.approx(0.1621, abs=5e-5)
    assert sonnet == pytest.approx(0.0837, abs=5e-5)
    assert 1 - sonnet / opus == pytest.approx(0.48, abs=0.01)


def test_the_plans_worked_example_a_warm_switch_costs_more() -> None:
    # A 120K-token conversation runs an 8-request tool loop, each request adding 3K input and
    # 600 output tokens (five-minute cache, as in the plan's arithmetic).
    def turn(prefix: int) -> Usage:
        return Usage(output_tokens=600, cache_read_tokens=prefix, cache_write_5m_tokens=3000)

    turns = [turn(120_000 + 3_000 * i) for i in range(8)]
    stay = sum(PRICES.cost("claude-opus-5-5", u) or 0 for u in turns)
    switch_first = PRICES.cost_cold("claude-sonnet-5-5", turns[0], "5m") or 0
    switch = switch_first + sum(PRICES.cost("claude-sonnet-5-5", u) or 0 for u in turns[1:])
    assert stay == pytest.approx(0.4248, abs=1e-4)
    assert switch == pytest.approx(0.5928, abs=1e-4)
    assert switch > stay


# ── pricing a request ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (Usage(input_tokens=1_000_000), 4.0),
        (Usage(output_tokens=1_000_000), 20.0),
        (Usage(cache_read_tokens=1_000_000), 0.2),
        (Usage(cache_write_5m_tokens=1_000_000), 5.0),
        (Usage(cache_write_1h_tokens=1_000_000), 8.0),
        (Usage(), 0.0),
    ],
)
def test_each_token_kind_has_its_own_rate(usage: Usage, expected: float) -> None:
    assert PRICES.cost("claude-opus-5-5", usage) == pytest.approx(expected)


def test_a_real_main_turn_is_priced_at_the_one_hour_write_rate() -> None:
    model, usage = usage_of("main-bugfix-1")
    assert usage.cache_write_1h_tokens == 18618
    expected = (
        usage.input_tokens * 4
        + usage.output_tokens * 20
        + usage.cache_read_tokens * 0.2
        + 18618 * 8
    ) / 1_000_000
    assert PRICES.cost(model, usage) == pytest.approx(expected)


def test_the_extended_context_variant_is_priced_like_the_base_model() -> None:
    usage = Usage(input_tokens=10_000, output_tokens=500)
    assert PRICES.cost("claude-opus-5-5[1m]", usage) == PRICES.cost("claude-opus-5-5", usage)


@pytest.mark.parametrize("model", ["claude-imaginary-9", "", None])
def test_an_unknown_model_has_no_cost(model: str | None) -> None:
    assert PRICES.cost(model, Usage(input_tokens=1)) is None


def test_a_cold_cache_reprices_reads_as_writes() -> None:
    usage = Usage(
        input_tokens=2, output_tokens=100, cache_read_tokens=10_000, cache_write_1h_tokens=500
    )
    cold = PRICES.cost_cold("claude-sonnet-5-5", usage, "1h")
    assert cold == pytest.approx((2 * 2 + 100 * 10 + 10_500 * 4) / 1_000_000)
    warm = PRICES.cost("claude-sonnet-5-5", usage)
    assert cold is not None and warm is not None and cold > warm


@pytest.mark.parametrize(("seconds", "ttl"), [(300, "5m"), (3600, "1h"), (0, "5m")])
def test_ttl_for(seconds: int, ttl: str) -> None:
    assert ttl_for(seconds) == ttl


# ── contracted rates ─────────────────────────────────────────────────────────


def test_overrides_replace_list_prices_and_say_so() -> None:
    contracted = ModelRates(
        input=3.6, output=18.0, cache_write_5m=4.5, cache_write_1h=7.2, cache_read=0.18
    )
    book = PRICES.with_overrides({"claude-opus-5-5": contracted})
    assert book.version == "2026-09-25+overrides"
    assert book.cost("claude-opus-5-5", Usage(output_tokens=1_000_000)) == pytest.approx(18.0)
    assert book.cost("claude-sonnet-5-5", Usage(output_tokens=1_000_000)) == pytest.approx(10.0)
    assert PRICES.cost("claude-opus-5-5", Usage(output_tokens=1_000_000)) == pytest.approx(20.0)


def test_an_override_can_price_a_model_the_list_lacks() -> None:
    rates = ModelRates(input=1, output=2, cache_write_5m=1.25, cache_write_1h=2, cache_read=0.1)
    book = PRICES.with_overrides({"claude-gateway-alias": rates})
    assert book.cost("claude-gateway-alias", Usage(output_tokens=1_000_000)) == pytest.approx(2.0)


def test_no_overrides_keeps_the_list_version() -> None:
    assert PRICES.with_overrides({}).version == "2026-09-25"


# ── the price file ───────────────────────────────────────────────────────────


def test_the_price_file_is_dated_sourced_and_complete() -> None:
    data = json.loads(resources.files("theygent_ai_gateway").joinpath("pricing.json").read_text())
    assert data["version"] == PRICES.version
    assert "Re-check" in data["source"]
    for model, entry in data["models"].items():
        rates = Rates(
            *(
                entry[k]
                for k in ("input", "output", "cache_write_5m", "cache_write_1h", "cache_read")
            )
        )
        assert min(rates.__dict__.values()) >= 0, model
        assert entry["cache_read_rate"] in ("published", "derived"), model


def test_derived_rates_follow_the_standard_multipliers() -> None:
    data = json.loads(resources.files("theygent_ai_gateway").joinpath("pricing.json").read_text())
    for model, entry in data["models"].items():
        assert entry["cache_write_5m"] == pytest.approx(1.25 * entry["input"]), model
        assert entry["cache_write_1h"] == pytest.approx(2.0 * entry["input"]), model
        if entry["cache_read_rate"] == "derived":
            assert entry["cache_read"] == pytest.approx(0.1 * entry["input"]), model


def test_the_current_models_cache_reads_are_the_published_rates() -> None:
    for model, read in (
        ("claude-opus-5-5", 0.2),
        ("claude-sonnet-5-5", 0.2),
        ("claude-fable-5-1", 0.25),
    ):
        rates = PRICES.rates(model)
        assert rates is not None and rates.cache_read == read


# ── seat allowance ───────────────────────────────────────────────────────────

RESET_5H = datetime(2026, 10, 4, 12, tzinfo=UTC)
RESET_7D = datetime(2026, 10, 10, 18, tzinfo=UTC)


def seat(
    five: float | None, seven: float | None, reset_5h: datetime | None = RESET_5H
) -> SeatAllowance:
    return SeatAllowance(
        five_hour_utilization=five,
        seven_day_utilization=seven,
        five_hour_resets_at=reset_5h,
        seven_day_resets_at=RESET_7D,
    )


def test_utilization_change_within_a_window() -> None:
    change = utilization_change(seat(0.38, 0.04), seat(0.41, 0.06))
    assert change.five_hour == pytest.approx(0.03)
    assert change.seven_day == pytest.approx(0.02)


def test_no_change_is_read_across_a_window_reset() -> None:
    later = datetime(2026, 10, 4, 17, tzinfo=UTC)
    change = utilization_change(seat(0.95, 0.30), seat(0.02, 0.31, reset_5h=later))
    assert change.five_hour is None
    assert change.seven_day == pytest.approx(0.01)


def test_missing_readings_give_no_change() -> None:
    change = utilization_change(seat(None, 0.1), seat(0.2, None))
    assert (change.five_hour, change.seven_day) == (None, None)


def test_a_reading_that_went_down_counts_as_no_use() -> None:
    assert utilization_change(seat(0.5, 0.1), seat(0.4, 0.1)).five_hour == 0.0


# ── through the gateway ──────────────────────────────────────────────────────


def test_observations_carry_their_cost(tmp_path: Path) -> None:
    seen: list[Observation] = []
    exchange = load("subagent-explore-sonnet-1")
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
        )
        with serve(create_app(settings, observe=seen.append)) as url:
            httpx.request(
                exchange.method,
                f"{url}{exchange.target}",
                content=exchange.request_bytes(),
                headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
                timeout=10,
            )
            deadline = time.monotonic() + 10
            while not seen and time.monotonic() < deadline:
                time.sleep(0.01)
    [observation] = seen
    model, usage = usage_of(exchange.name)
    # Priced on the model that served it (Sonnet), not the one Claude Code asked for (Opus).
    assert model == "claude-sonnet-5-5"
    assert observation.cost_usd == pytest.approx(PRICES.cost(model, usage))
    assert observation.pricing_version == "2026-09-25"
