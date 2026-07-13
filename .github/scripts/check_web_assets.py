#!/usr/bin/env python3
"""Fail if any local asset reference in the static marketing site does not resolve.

The site under apps/web/ has no build step, so the one thing that breaks silently is a
relative path to a missing file (a renamed logo, a mistyped stylesheet href). This walks
every .html and .css file, extracts local href/src/url() targets, and checks each resolves
to a file on disk. External URLs, mail/tel links, in-page anchors, and data: URIs are skipped.

Usage: check_web_assets.py <site-dir>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

# href="..."/src="..." (HTML) and url(...) (CSS). srcset is handled separately.
ATTR_RE = re.compile(r"""(?:href|src)\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
URL_RE = re.compile(r"""url\(\s*['"]?([^'")]+)['"]?\s*\)""", re.IGNORECASE)
SRCSET_RE = re.compile(r"""srcset\s*=\s*["']([^"']+)["']""", re.IGNORECASE)

SKIP_PREFIXES = ("http://", "https://", "//", "mailto:", "tel:", "data:", "#", "javascript:")


def is_local(ref: str) -> bool:
    ref = ref.strip()
    return bool(ref) and not ref.lower().startswith(SKIP_PREFIXES)


def resolve(site: Path, source_file: Path, ref: str) -> Path:
    # Strip query/fragment, decode %20 etc.
    path = unquote(urlparse(ref).path)
    if path.startswith("/"):
        return site / path.lstrip("/")
    return (source_file.parent / path).resolve()


def refs_in(text: str) -> list[str]:
    out = list(ATTR_RE.findall(text)) + list(URL_RE.findall(text))
    for group in SRCSET_RE.findall(text):
        # "a.png 1x, b.png 2x" -> ["a.png", "b.png"]
        out += [c.strip().split()[0] for c in group.split(",") if c.strip()]
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_web_assets.py <site-dir>", file=sys.stderr)
        return 2

    site = Path(sys.argv[1]).resolve()
    index = site / "index.html"
    if not index.is_file():
        print(f"FAIL: {index} is missing — the site has no entry point.", file=sys.stderr)
        return 1

    missing: list[str] = []
    checked = 0
    for f in sorted([*site.rglob("*.html"), *site.rglob("*.css")]):
        text = f.read_text(encoding="utf-8", errors="replace")
        for ref in refs_in(text):
            if not is_local(ref):
                continue
            checked += 1
            target = resolve(site, f, ref)
            if not target.exists():
                missing.append(f"{f.relative_to(site)} -> {ref}")

    if missing:
        print("FAIL: broken local asset references:", file=sys.stderr)
        for m in missing:
            print(f"  {m}", file=sys.stderr)
        return 1

    print(f"OK: {checked} local asset reference(s) across the site all resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
