"""Update the gateway's packaged price list from Anthropic's published pricing page.

    uv run --package theygent-ai-gateway python apps/ai-gateway/scripts/update_pricing.py
    uv run --package theygent-ai-gateway python apps/ai-gateway/scripts/update_pricing.py --check

Without ``--check`` it rewrites ``pricing.json`` when a price changed (stamping the date and time
it read them) and leaves it untouched otherwise. With ``--check`` it changes nothing and exits 1
when the packaged prices differ from the published ones. ``--source`` reads a saved copy of the
page instead of fetching it. Any page it cannot read as expected exits 2, naming what it missed.

The scheduled workflow ``.github/workflows/ai-gateway-pricing.yml`` runs it weekly and opens a
pull request when prices change, so a change is reviewed before it reaches anyone's figures.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx
from theygent_ai_gateway.pricing_source import (
    PRICING_URL,
    PricingSourceError,
    price_list,
    same_prices,
)

PACKAGED = Path(__file__).resolve().parents[1] / "src" / "theygent_ai_gateway" / "pricing.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="report, change nothing")
    parser.add_argument("--source", type=Path, help="a saved copy of the pricing page (Markdown)")
    parser.add_argument("--output", type=Path, default=PACKAGED, help="the pricing.json to update")
    args = parser.parse_args(argv)

    try:
        if args.source is not None:
            markdown = args.source.read_text()
        else:
            response = httpx.get(PRICING_URL, timeout=30, follow_redirects=True)
            response.raise_for_status()
            markdown = response.text
        fresh = price_list(markdown)
    except (httpx.HTTPError, OSError, PricingSourceError) as exc:
        print(f"could not read the price list: {exc}", file=sys.stderr)
        return 2

    current = json.loads(args.output.read_text()) if args.output.exists() else {}
    if same_prices(current, fresh):
        print(f"prices unchanged since {current.get('updated_at', current.get('version'))}")
        return 0
    changed = sorted(
        model
        for model in set(fresh["models"]) | set(current.get("models", {}))
        if fresh["models"].get(model) != current.get("models", {}).get(model)
    )
    print(f"prices differ for: {', '.join(changed)}")
    if args.check:
        return 1
    args.output.write_text(json.dumps(fresh, indent=2) + "\n")
    print(f"wrote {args.output} (prices as of {fresh['updated_at']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
