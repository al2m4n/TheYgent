#!/usr/bin/env python3
"""Point the URLs the published docs versions name at the current hosts and /latest/ pages.

Versions up to v1.3.3 were built for the TheYgent hosts (docs.theygent.ai, theygent.ai), which
the project does not keep: every canonical link, sitemap, llms-full.txt and site link in them
names a host that ends up serving someone else. So, first, every version directory's URLs on
those hosts move to docs.ertir.dev and ertir.dev, path and query unchanged.

Versions v0.1.0 through v1.0.1 were built while mike appended the version being deployed to a
site_url that already ended in /latest/, so every URL they name for themselves — each page's
canonical link, the sitemap, llms-full.txt — is /latest/<version>/<page>, a path that only
redirects (root/_redirects). Search engines ignore a canonical that redirects and pick an old
version's copy as the canonical page instead of /latest/. Second, this rewrites that prefix to
/latest/ in the deploy tree before upload, so those versions declare the same canonicals as every
later one. The gh-pages branch stays as mike published it.

The versions come from the deploy tree's _redirects (copied from docs/user-docs/root/): each
`/latest/<version>/* /latest/:splat 301` rule names one. A rule whose version directory is
missing from the tree fails the deploy.

Usage: docs_repair_canonicals.py <deploy-tree>
"""

from __future__ import annotations

import gzip
import re
import sys
from pathlib import Path

ORIGIN = "https://docs.ertir.dev/latest/"
RULE = re.compile(r"^/latest/(v[^/\s]+)/\* /latest/:splat 301$", re.MULTILINE)
VERSION_DIR = re.compile(r"^v\d")
# A host ends at anything but a name character, so "theygent.ai." ending a sentence moves and
# "theygent.ai.example" does not.
_HOST_END = rb"(?![\w-]|\.\w)"
OLD_HOSTS = (
    (re.compile(rb"https?://docs\.theygent\.ai" + _HOST_END), b"https://docs.ertir.dev"),
    (re.compile(rb"https?://(?:www\.)?theygent\.ai" + _HOST_END), b"https://ertir.dev"),
)
# Text the site serves; images and fonts are never rewritten, so their bytes cannot shift.
TEXT_SUFFIXES = {".html", ".xml", ".txt", ".json", ".js", ".css", ".gz"}


def broken_versions(redirects: str) -> list[str]:
    return RULE.findall(redirects)


def self_url(version: str) -> re.Pattern[bytes]:
    """The version's root URL, with or without its trailing slash, never a longer version."""
    return re.compile(re.escape(f"{ORIGIN}{version}".encode()) + rb"(?:/|(?![\w-]|\.\d))")


def move_file(path: Path) -> int:
    raw = path.read_bytes()
    gz = path.suffix == ".gz"
    data = gzip.decompress(raw) if gz else raw
    total = 0
    for old, new in OLD_HOSTS:
        data, count = old.subn(new, data)
        total += count
    if total:
        path.write_bytes(gzip.compress(data, mtime=0) if gz else data)
    return total


def move_hosts(site: Path) -> dict[str, int]:
    """Rewrite every version directory's old-host URLs; return the URLs rewritten per version."""
    counts: dict[str, int] = {}
    for root in sorted(d for d in site.iterdir() if d.is_dir() and VERSION_DIR.match(d.name)):
        files = (p for p in root.rglob("*") if p.is_file() and p.suffix in TEXT_SUFFIXES)
        counts[root.name] = sum(move_file(p) for p in files)
    return counts


def repair_file(path: Path, old: re.Pattern[bytes]) -> int:
    raw = path.read_bytes()
    gz = path.suffix == ".gz"
    data, count = old.subn(ORIGIN.encode(), gzip.decompress(raw) if gz else raw)
    if count:
        path.write_bytes(gzip.compress(data, mtime=0) if gz else data)
    return count


def repair(site: Path) -> dict[str, int]:
    """Rewrite every version _redirects names; return the URLs rewritten per version."""
    redirects = site / "_redirects"
    versions = broken_versions(redirects.read_text(encoding="utf-8"))
    if not versions:
        raise ValueError(f"{redirects} has no /latest/<version>/* rules to repair")
    counts: dict[str, int] = {}
    for version in versions:
        root = site / version
        if not root.is_dir():
            raise FileNotFoundError(f"{redirects} names {version}, but {root} does not exist")
        old = self_url(version)
        counts[version] = sum(repair_file(p, old) for p in root.rglob("*") if p.is_file())
    return counts


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    site = Path(sys.argv[1])
    try:
        moved = move_hosts(site)
        counts = repair(site)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for version, count in moved.items():
        if count:
            print(f"{version}: {count} URLs now point at the Ertir hosts")
    for version, count in counts.items():
        print(f"{version}: {count} URLs now point at /latest/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
