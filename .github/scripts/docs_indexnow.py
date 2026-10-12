#!/usr/bin/env python3
"""Print, as a JSON array, the docs.theygent.ai/latest/ URLs a release changed.

IndexNow asks for changed URLs only. A docs page is changed when its source Markdown under
docs/user-docs/content/ differs between the previous release and this one. A change to the
site's configuration or build hooks (mkdocs.yml, hooks/) can touch every page, so it counts as
every URL changed. Only URLs present in the built /latest/ sitemap are printed, so a deleted
or renamed page is never submitted.

Usage: docs_indexnow.py <built-site-dir> <base-ref> <head-ref>
       (the built-site dir is the deploy tree; its latest/sitemap.xml lists the live pages)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ORIGIN = "https://docs.theygent.ai/latest/"
CONTENT = "docs/user-docs/content/"
SITE_WIDE = ("docs/user-docs/mkdocs.yml", "docs/user-docs/hooks/")


def page_url(path: str) -> str | None:
    if not (path.startswith(CONTENT) and path.endswith(".md")):
        return None
    rel = path[len(CONTENT) : -len(".md")]
    if rel == "index":
        return ORIGIN
    if rel.endswith("/index"):
        rel = rel[: -len("/index")]
    return f"{ORIGIN}{rel}/"


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__, file=sys.stderr)
        return 2
    site, base, head = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
    sitemap = (site / "latest" / "sitemap.xml").read_text(encoding="utf-8")
    live = re.findall(r"<loc>([^<]+)</loc>", sitemap)
    try:
        changed = subprocess.run(
            ["git", "diff", "--name-only", base, head, "--", "docs/user-docs"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
    except subprocess.CalledProcessError:
        print(f"base {base!r} is not in history — treating every page as changed", file=sys.stderr)
        print(json.dumps(live))
        return 0
    if any(f == s or f.startswith(s) for f in changed for s in SITE_WIDE):
        print(json.dumps(live))
        return 0
    wanted = {u for u in (page_url(f) for f in changed) if u}
    print(json.dumps([u for u in live if u in wanted]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
