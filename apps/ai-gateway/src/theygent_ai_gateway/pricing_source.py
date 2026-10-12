"""Reading Anthropic's published price list into the gateway's ``pricing.json``.

The source is the pricing page of Anthropic's documentation, which the docs site also serves as
Markdown (``PRICING_URL``). Two of its tables matter to Claude Code's traffic:

* **Model pricing** — per model: base input, 5-minute and 1-hour cache writes, cache hits, output.
  A model priced by prompt length (its rows say "for prompts up to / over N tokens") gets its
  higher prices as a ``long_context`` tier above that threshold;
* **Fast mode pricing** — input and output for the models with a fast mode (a request with
  ``speed: "fast"``). The page says caching multipliers apply on top, so a fast tier's cache rates
  keep the model's own ratios to its input price.

Anything the parser does not recognise fails loudly (``PricingSourceError``): a price list that
changed shape must be looked at, never half-read. The rest of the page — batch, data residency,
cloud platforms — does not apply to requests a gateway relays to the first-party API.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

PRICING_URL = "https://platform.claude.com/docs/en/about-claude/pricing.md"
SOURCE_PAGE = "https://platform.claude.com/docs/en/about-claude/pricing"
RATE_FIELDS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
#: The model table's columns, in the page's order (after the model name).
_MODEL_COLUMNS = RATE_FIELDS
#: Fewer models than this means the table was not read whole.
MIN_MODELS = 5

_PRICE = re.compile(r"^\$([0-9]+(?:\.[0-9]+)?)\s*/\s*MTok")
_NAME = re.compile(r"^Claude\s+([A-Za-z]+)\s+([0-9]+(?:\.[0-9]+)?)$")
_TIER = re.compile(r"for prompts (up to|over) ([0-9][0-9,]*) tokens")


class PricingSourceError(ValueError):
    """The price list could not be read as expected."""


def model_id(name: str) -> str:
    """``Claude Opus 5.5`` → ``claude-opus-5-5``."""
    match = _NAME.match(name.strip())
    if match is None:
        raise PricingSourceError(f"not a model name: {name!r}")
    family, version = match.groups()
    return f"claude-{family.lower()}-{version.replace('.', '-')}"


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _price(cell: str) -> float:
    text = re.sub(r"<sup>.*?</sup>", "", cell).strip()
    match = _PRICE.match(text)
    if match is None:
        raise PricingSourceError(f"not a price: {cell!r}")
    return float(match.group(1))


def _table(markdown: str, heading: str) -> list[list[str]]:
    """The rows of the first table under ``heading`` (header and divider dropped)."""
    lines = markdown.splitlines()
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        raise PricingSourceError(f"no {heading!r} section") from None
    rows: list[list[str]] = []
    in_table = False
    for line in lines[start + 1 :]:
        if line.startswith("|"):
            in_table = True
            rows.append(_cells(line))
        elif in_table:
            break
        elif line.startswith("#"):
            break
    if len(rows) < 3:
        raise PricingSourceError(f"no table under {heading!r}")
    return rows[2:]


def _split_name(cell: str) -> tuple[str, str]:
    """A model cell's name and its parenthesised note (links removed)."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", cell).strip()
    match = re.match(r"^(.*?)\s*\((.*)\)\s*$", text)
    return (match.group(1), match.group(2)) if match else (text, "")


def _derived(rates: dict[str, float], new_input: float, new_output: float) -> dict[str, float]:
    """A tier priced at other input and output rates, with the model's own cache ratios."""
    base = rates["input"]
    return {
        "input": new_input,
        "cache_write_5m": round(new_input * rates["cache_write_5m"] / base, 6),
        "cache_write_1h": round(new_input * rates["cache_write_1h"] / base, 6),
        "cache_read": round(new_input * rates["cache_read"] / base, 6),
        "output": new_output,
    }


def parse(markdown: str) -> dict[str, dict[str, Any]]:
    """Every model's rates, with its ``long_context`` and ``fast`` tiers where it has them."""
    models: dict[str, dict[str, Any]] = {}
    for row in _table(markdown, "## Model pricing"):
        if len(row) != 1 + len(_MODEL_COLUMNS):
            raise PricingSourceError(f"a model row has {len(row)} cells: {row[0]!r}")
        name, note = _split_name(row[0])
        identifier = model_id(name)
        rates = {field: _price(cell) for field, cell in zip(_MODEL_COLUMNS, row[1:], strict=True)}
        tier = _TIER.search(note)
        if tier is None:
            if identifier in models:
                raise PricingSourceError(f"{identifier} is listed twice")
            models[identifier] = rates
        elif tier.group(1) == "up to":
            models.setdefault(identifier, {}).update(rates)
        else:
            entry = models.setdefault(identifier, {})
            entry["long_context"] = {
                "over_prompt_tokens": int(tier.group(2).replace(",", "")),
                **rates,
            }
    for row in _table(markdown, "### Fast mode pricing"):
        if len(row) != 3:
            raise PricingSourceError(f"a fast mode row has {len(row)} cells: {row[0]!r}")
        fast_input, fast_output = _price(row[1]), _price(row[2])
        for name in row[0].split("/"):
            identifier = model_id(name)
            if identifier not in models:
                raise PricingSourceError(f"fast mode for an unlisted model: {identifier}")
            models[identifier]["fast"] = _derived(models[identifier], fast_input, fast_output)
    if len(models) < MIN_MODELS:
        raise PricingSourceError(f"only {len(models)} models read")
    for identifier, entry in models.items():
        missing = [f for f in RATE_FIELDS if f not in entry]
        if missing:
            raise PricingSourceError(f"{identifier} has no {', '.join(missing)} price")
    return models


def price_list(markdown: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The ``pricing.json`` document for the page's prices, stamped with when they were read."""
    when = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    return {
        "version": when.date().isoformat(),
        "updated_at": when.isoformat().replace("+00:00", "Z"),
        "source_url": SOURCE_PAGE,
        "source": (
            "Anthropic's list prices for the Claude API, in USD per million tokens, as published "
            "on the source page when updated_at says. A long_context tier applies to a request "
            "whose prompt (input, cache reads and cache writes) is over its threshold; a fast "
            'tier to a request sent with speed: "fast". Contracted rates belong in the gateway '
            "config's pricing_overrides."
        ),
        "models": parse(markdown),
    }


def same_prices(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Whether two price lists charge the same, whenever each was read."""
    return a.get("models") == b.get("models")
