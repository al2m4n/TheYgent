#!/usr/bin/env python3
"""Keep the marketing site's sitemap truthful at deploy time.

Two subcommands, both driven by git history (the workflow checks out with full depth):

  stamp <site-dir>
      Rewrite every <lastmod> in sitemap.xml to the date of the last commit that touched that
      URL's own page file. Crawlers trust <lastmod> only while it is consistently accurate, so a
      page that did not change keeps its old date even when a sibling page or the shared
      stylesheet did.

  changed <site-dir> <base-sha>
      Print, as a JSON array, the sitemap URLs whose page file changed between <base-sha> and
      HEAD — the list IndexNow asks for (submit changed URLs only). When the base commit is
      unknown (a first push, or history rewritten past it), every URL counts as changed.

Usage: web_sitemap.py stamp <site-dir> | web_sitemap.py changed <site-dir> <base-sha>
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ORIGIN = "https://theygent.ai"
URL_RE = re.compile(r"(<loc>([^<]+)</loc>\s*<lastmod>)[^<]*(</lastmod>)")


def page_for(site: Path, url: str) -> Path:
    path = url[len(ORIGIN) :].lstrip("/")
    return site / path / "index.html" if path == "" or path.endswith("/") else site / path


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()


def stamp(site: Path) -> int:
    sitemap = site / "sitemap.xml"

    def lastmod(m: re.Match[str]) -> str:
        date = git("log", "-1", "--format=%cs", "--", str(page_for(site, m.group(2))))
        if not date:
            raise SystemExit(f"no commit touches the page for {m.group(2)} — is it committed?")
        return f"{m.group(1)}{date}{m.group(3)}"

    text, count = URL_RE.subn(lastmod, sitemap.read_text(encoding="utf-8"))
    sitemap.write_text(text, encoding="utf-8")
    print(f"stamped lastmod on {count} URL(s)", file=sys.stderr)
    return 0


def changed(site: Path, base: str) -> int:
    urls = [m.group(2) for m in URL_RE.finditer((site / "sitemap.xml").read_text(encoding="utf-8"))]
    try:
        files = set(git("diff", "--name-only", base, "HEAD", "--", str(site)).splitlines())
    except subprocess.CalledProcessError:
        print(f"base {base!r} is not in history — treating every URL as changed", file=sys.stderr)
        print(json.dumps(urls))
        return 0
    root = Path(git("rev-parse", "--show-toplevel"))
    hits = [u for u in urls if page_for(site, u).resolve().relative_to(root).as_posix() in files]
    print(json.dumps(hits))
    return 0


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "stamp":
        return stamp(Path(sys.argv[2]))
    if len(sys.argv) == 4 and sys.argv[1] == "changed":
        return changed(Path(sys.argv[2]), sys.argv[3])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
