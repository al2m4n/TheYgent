"""A price list frozen for tests that pin dollar figures.

The packaged ``pricing.json`` follows Anthropic's published prices and changes when they do; a test
that pins what the plan's arithmetic comes to reads this copy instead, so a price update changes
the gateway's figures without breaking the arithmetic's tests.
"""

from __future__ import annotations

import json
from pathlib import Path

from theygent_ai_gateway.costs import PriceBook

FROZEN_PRICES = PriceBook.from_document(
    json.loads((Path(__file__).parent / "fixtures" / "pricing-2026-10-11.json").read_text())
)
