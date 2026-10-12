"""Reading the published price list: what the updater takes from the page, what makes it refuse,
and what the update script does with it.

The page (``fixtures/pricing-page.md``) is a stand-in written in the published page's format (its
two tables, footnote markers, links in model names, the prompt-length rows), not a copy of it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.pricing_source import (
    PricingSourceError,
    model_id,
    parse,
    price_list,
    same_prices,
)

PAGE = (Path(__file__).parent / "fixtures" / "pricing-page.md").read_text()


def test_every_model_row_is_read() -> None:
    models = parse(PAGE)
    assert set(models) == {
        "claude-opus-5-5",
        "claude-opus-5",
        "claude-opus-4",
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-haiku-5-5",
    }
    assert models["claude-sonnet-5-5"] == {
        "input": 2.0,
        "cache_write_5m": 2.5,
        "cache_write_1h": 4.0,
        "cache_read": 0.1,
        "output": 10.0,
    }
    # Footnote markers and links in names are not part of a price or a model id.
    assert models["claude-sonnet-5"]["input"] == 2.0
    assert models["claude-opus-4"]["output"] == 75.0


def test_a_model_priced_by_prompt_length_gets_a_long_context_tier() -> None:
    haiku = parse(PAGE)["claude-haiku-5-5"]
    assert (haiku["input"], haiku["output"]) == (0.1, 0.5)
    assert haiku["long_context"] == {
        "over_prompt_tokens": 100_000,
        "input": 0.5,
        "cache_write_5m": 0.625,
        "cache_write_1h": 1.0,
        "cache_read": 0.05,
        "output": 2.5,
    }


def test_fast_mode_keeps_each_models_cache_ratios() -> None:
    models = parse(PAGE)
    assert models["claude-opus-5-5"]["fast"] == {
        "input": 8.0,
        "cache_write_5m": 10.0,
        "cache_write_1h": 16.0,
        "cache_read": 0.4,
        "output": 40.0,
    }
    # One row can name several models.
    assert models["claude-opus-5"]["fast"]["input"] == models["claude-opus-4"]["fast"]["input"]
    assert models["claude-opus-4"]["fast"]["cache_read"] == pytest.approx(1.0)  # 0.1x of $10
    assert "fast" not in models["claude-sonnet-5-5"]


@pytest.mark.parametrize(
    ("name", "identifier"),
    [("Claude Opus 5.5", "claude-opus-5-5"), ("Claude Haiku 4.5", "claude-haiku-4-5")],
)
def test_model_ids(name: str, identifier: str) -> None:
    assert model_id(name) == identifier


@pytest.mark.parametrize(
    ("change", "complaint"),
    [
        (("## Model pricing", "## Prices"), "no '## Model pricing' section"),
        (("### Fast mode pricing", "### Fast"), "no '### Fast mode pricing' section"),
        (("| $4 / MTok | $5 / MTok |", "| four dollars | $5 / MTok |"), "not a price"),
        (("| Claude Opus 5 / Claude Opus 4 |", "| Claude Opus 9 |"), "unlisted model"),
        (("| Claude Opus 5 | $5", "| Opus Five | $5"), "not a model name"),
        (("| Claude Sonnet 5 | $2", "| Claude Sonnet 5.5 | $2"), "listed twice"),
    ],
)
def test_a_page_that_changed_shape_is_refused(change: tuple[str, str], complaint: str) -> None:
    old, new = change
    assert old in PAGE
    with pytest.raises(PricingSourceError, match=complaint):
        parse(PAGE.replace(old, new, 1))


def test_a_table_read_short_is_refused() -> None:
    lines = [line for line in PAGE.splitlines() if "Opus 4" not in line and "Sonnet" not in line]
    page = "\n".join(lines).replace(" / Claude Opus 4", "")
    with pytest.raises(PricingSourceError, match="models read"):
        parse(page)


def test_a_price_list_says_when_it_was_read_and_loads() -> None:
    now = datetime(2026, 10, 11, 13, 56, 42, 123, tzinfo=UTC)
    document = price_list(PAGE, now=now)
    assert (document["version"], document["updated_at"]) == ("2026-10-11", "2026-10-11T13:56:42Z")
    assert document["source_url"].startswith("https://platform.claude.com/")
    book = PriceBook.from_document(document)
    assert book.version == "2026-10-11" and book.updated_at == "2026-10-11T13:56:42Z"
    assert book.has_fast_mode("claude-opus-5-5")
    later = price_list(PAGE, now=datetime(2026, 10, 18, tzinfo=UTC))
    assert same_prices(document, later) and later["updated_at"] != document["updated_at"]


# ── the update script ────────────────────────────────────────────────────────

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update_pricing.py"


def run_script(*args: str) -> int:
    spec = importlib.util.spec_from_file_location("update_pricing", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["update_pricing"] = module
    spec.loader.exec_module(module)
    return module.main(list(args))


def test_the_script_writes_changed_prices_and_leaves_unchanged_ones(tmp_path: Path) -> None:
    page, output = tmp_path / "pricing.md", tmp_path / "pricing.json"
    page.write_text(PAGE)
    assert run_script("--check", "--source", str(page), "--output", str(output)) == 1
    assert not output.exists()  # --check changes nothing
    assert run_script("--source", str(page), "--output", str(output)) == 0
    written = json.loads(output.read_text())
    assert written["models"] == parse(PAGE)
    assert run_script("--check", "--source", str(page), "--output", str(output)) == 0
    # Unchanged prices keep the file as it was, timestamp included.
    before = output.read_text()
    assert run_script("--source", str(page), "--output", str(output)) == 0
    assert output.read_text() == before


def test_the_script_refuses_a_page_it_cannot_read(tmp_path: Path) -> None:
    page, output = tmp_path / "pricing.md", tmp_path / "pricing.json"
    page.write_text(PAGE.replace("## Model pricing", "## Something else"))
    assert run_script("--source", str(page), "--output", str(output)) == 2
    assert not output.exists()
    assert run_script("--source", str(tmp_path / "missing.md"), "--output", str(output)) == 2


def test_the_packaged_list_reads_like_the_page_says() -> None:
    # The file the gateway ships was written by this parser: every model has its five rates.
    book = PriceBook.packaged()
    assert book.updated_at is not None and book.version == book.updated_at[:10]
    assert book.rates("claude-opus-5-5") is not None and book.has_fast_mode("claude-opus-5-5")
