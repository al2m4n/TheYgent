"""The cost model: list prices, contracted overrides, cold-cache counterfactuals, seat allowance.

The figures that matter are pinned against the numbers the routing plan rests on: the Phase 0
pair (the same Explore subagent work on Opus 5.5 and Sonnet 5.5) and the plan's worked example of
a mid-conversation switch costing more than it saves. They are priced on a frozen copy of the
price list (``_prices``), so a published price change moves the gateway's figures, not these; the
packaged list itself is checked for shape and source.
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
from _prices import FROZEN_PRICES
from theygent_ai_gateway.app import create_app
from theygent_ai_gateway.costs import PriceBook, ttl_for, utilization_change
from theygent_ai_gateway.observe import Observation, ResponseObserver
from theygent_ai_gateway.settings import GatewaySettings
from theygent_ir.gateway import ModelRates, SeatAllowance, Usage

PRICES = FROZEN_PRICES
PACKAGED = PriceBook.packaged()


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
    assert sonnet == pytest.approx(0.0788, abs=5e-5)
    assert 1 - sonnet / opus == pytest.approx(0.51, abs=0.01)


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
    assert switch == pytest.approx(0.5004, abs=1e-4)
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


def test_a_dated_snapshot_is_priced_like_its_model() -> None:
    # The API names the model that served a request by its snapshot: Claude Code asks for
    # claude-haiku-4-5, and the response says claude-haiku-4-5-20251001.
    usage = Usage(input_tokens=10_000, output_tokens=500)
    cost = PRICES.cost("claude-haiku-4-5-20251001", usage)
    assert cost is not None and cost == PRICES.cost("claude-haiku-4-5", usage)
    assert PRICES.cost("claude-haiku-4-5-2025", usage) is None  # not a date: an unknown model


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
    assert book.version == f"{PRICES.version}+overrides"
    assert book.cost("claude-opus-5-5", Usage(output_tokens=1_000_000)) == pytest.approx(18.0)
    assert book.cost("claude-sonnet-5-5", Usage(output_tokens=1_000_000)) == pytest.approx(10.0)
    assert PRICES.cost("claude-opus-5-5", Usage(output_tokens=1_000_000)) == pytest.approx(20.0)


def test_an_override_can_price_a_model_the_list_lacks() -> None:
    rates = ModelRates(input=1, output=2, cache_write_5m=1.25, cache_write_1h=2, cache_read=0.1)
    book = PRICES.with_overrides({"claude-gateway-alias": rates})
    assert book.cost("claude-gateway-alias", Usage(output_tokens=1_000_000)) == pytest.approx(2.0)


def test_no_overrides_keeps_the_list_version() -> None:
    assert PRICES.with_overrides({}).version == PRICES.version


# ── the price file ───────────────────────────────────────────────────────────


def _packaged() -> dict:
    return json.loads(resources.files("theygent_ai_gateway").joinpath("pricing.json").read_text())


def test_the_price_file_is_dated_sourced_and_complete() -> None:
    data = _packaged()
    assert data["version"] == PACKAGED.version == data["updated_at"][:10]
    assert datetime.fromisoformat(data["updated_at"]).tzinfo is not None  # date and time, UTC
    assert data["source_url"].startswith("https://platform.claude.com/")
    assert "pricing_overrides" in data["source"]
    assert {"claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-4-5"} <= set(data["models"])
    for model, entry in data["models"].items():
        tiers = [entry, *(entry[k] for k in ("long_context", "fast") if k in entry)]
        for tier in tiers:
            rates = [tier[k] for k in ("input", "output", "cache_write_5m", "cache_write_1h")]
            assert min(rates) > 0 and tier["cache_read"] >= 0, model


def test_cache_writes_follow_the_standard_multipliers_and_reads_cost_less() -> None:
    for model, entry in _packaged()["models"].items():
        for tier in (entry, *(entry[k] for k in ("long_context", "fast") if k in entry)):
            assert tier["cache_write_5m"] == pytest.approx(1.25 * tier["input"]), model
            assert tier["cache_write_1h"] == pytest.approx(2.0 * tier["input"]), model
            assert tier["cache_read"] <= 0.1 * tier["input"] + 1e-9, model


def test_the_current_models_cache_reads_are_the_published_rates() -> None:
    for model, read in (
        ("claude-opus-5-5", 0.2),
        ("claude-sonnet-5-5", 0.1),
        ("claude-fable-5-1", 0.25),
        ("claude-haiku-4-5", 0.1),
    ):
        rates = PRICES.rates(model)
        assert rates is not None and rates.cache_read == read


# ── published modifiers ──────────────────────────────────────────────────────


def test_a_long_prompt_pays_the_long_context_rates() -> None:
    # Haiku 5.5 is priced by prompt length, cache reads and writes included in the prompt.
    short = Usage(input_tokens=60_000, cache_read_tokens=40_000, output_tokens=1_000)
    long = Usage(input_tokens=60_000, cache_read_tokens=40_001, output_tokens=1_000)
    assert PRICES.cost("claude-haiku-5-5", short) == pytest.approx(
        (60_000 * 0.1 + 40_000 * 0.01 + 1_000 * 0.5) / 1e6
    )
    assert PRICES.cost("claude-haiku-5-5", long) == pytest.approx(
        (60_000 * 0.5 + 40_001 * 0.05 + 1_000 * 2.5) / 1e6
    )
    # A model without the tier costs the same at any length.
    assert PRICES.cost("claude-sonnet-5-5", long) == pytest.approx(
        (60_000 * 2 + 40_001 * 0.1 + 1_000 * 10) / 1e6
    )


def test_fast_mode_pays_the_fast_rates_with_the_models_cache_ratios() -> None:
    usage = Usage(input_tokens=1_000, output_tokens=1_000, cache_read_tokens=10_000)
    assert PRICES.cost("claude-opus-5-5", usage, fast=True) == pytest.approx(
        (1_000 * 8 + 1_000 * 40 + 10_000 * 0.4) / 1e6
    )
    assert PRICES.has_fast_mode("claude-opus-5-5") and not PRICES.has_fast_mode("claude-sonnet-5-5")
    # A model with no fast mode never charges fast rates.
    assert PRICES.cost("claude-sonnet-5-5", usage, fast=True) == PRICES.cost(
        "claude-sonnet-5-5", usage
    )


def test_contracted_rates_keep_each_tiers_published_ratio() -> None:
    half = ModelRates(input=2, output=10, cache_write_5m=2.5, cache_write_1h=4, cache_read=0.1)
    book = PRICES.with_overrides({"claude-opus-5-5": half})
    usage = Usage(output_tokens=1_000_000)
    assert book.cost("claude-opus-5-5", usage) == pytest.approx(10.0)
    assert book.cost("claude-opus-5-5", usage, fast=True) == pytest.approx(20.0)  # still 2x


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


def test_a_fast_mode_exchange_is_priced_at_fast_rates(tmp_path: Path) -> None:
    seen: list[Observation] = []
    exchange = load("main-bugfix-1")
    body = json.dumps({**exchange.body, "speed": "fast"}, separators=(",", ":")).encode()
    with fake_anthropic() as upstream:
        settings = GatewaySettings(
            upstream_url=upstream.base_url, gateway_id="gw-test", state_dir=tmp_path
        )
        with serve(create_app(settings, observe=seen.append)) as url:
            httpx.post(
                f"{url}{exchange.target}",
                content=body,
                headers=[*exchange.request_headers, ("x-fake-fixture", exchange.name)],
                timeout=10,
            )
            deadline = time.monotonic() + 10
            while not seen and time.monotonic() < deadline:
                time.sleep(0.01)
    [observation] = seen
    model, usage = usage_of(exchange.name)
    standard = PACKAGED.cost(model, usage)
    assert observation.request.fast
    assert observation.cost_usd == pytest.approx(PACKAGED.cost(model, usage, fast=True))
    assert standard is not None and observation.cost_usd is not None
    assert observation.cost_usd > standard


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
    assert observation.cost_usd == pytest.approx(PACKAGED.cost(model, usage))
    assert observation.pricing_version == PACKAGED.version
