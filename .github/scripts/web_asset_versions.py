#!/usr/bin/env python3
"""Give the marketing site's static files content-versioned URLs in the deploy tree.

Cloudflare caches /assets/* for a week and a Pages deploy does not purge that cache, so a file
whose contents change under the same URL keeps being served stale, by the edge and by browsers
alike. This rewrites every reference to a local static file to `<url>?v=<hash>`, where the hash
is the first 10 hex digits of the file's SHA-256. A changed file gets a new URL, and an unchanged
one keeps its URL and its cache.

Rewritten, in place, under <site-dir>:
  - url(...) in every .css file, stamped first: stamping changes a stylesheet's own bytes, and
    the pages must carry the hash of the stylesheet that is actually uploaded;
  - url(...) and href / src / content / srcset attribute values in every .html file, whether a
    site path (/assets/x.svg, ../x.css) or an absolute https://ertir.dev/ URL (og:image).

A reference is versioned only when it names an existing file with a static-asset suffix; pages,
anchors, external URLs, data: URIs and references that already carry a query are left alone.
The repository stays unstamped; the workflow runs this on the checkout it uploads.

Usage: web_asset_versions.py <site-dir>
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path
from urllib.parse import unquote

ORIGIN = "https://ertir.dev"
STATIC_SUFFIXES = {
    ".css", ".js", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".ico",
    ".woff2", ".woff", ".ttf", ".mp4", ".webm",
}  # fmt: skip
HASH_LEN = 10

CSS_URL_RE = re.compile(r"""(url\(\s*)(['"]?)([^'")\s]+)(\2\s*\))""", re.IGNORECASE)
ATTR_RE = re.compile(r"""(\s(?:href|src|content)\s*=\s*")([^"]*)(")""", re.IGNORECASE)
SRCSET_RE = re.compile(r"""(\ssrcset\s*=\s*")([^"]*)(")""", re.IGNORECASE)


class Versioner:
    def __init__(self, site: Path) -> None:
        self.site = site.resolve()
        self.hashes: dict[Path, str] = {}
        self.count = 0

    def target(self, source: Path, ref: str) -> Path | None:
        """The static file `ref` names from `source`, or None when it names no such file."""
        if not ref or "?" in ref or ref.startswith(("#", "//", "data:")):
            return None
        if ref.startswith(ORIGIN + "/"):
            path = self.site / unquote(ref[len(ORIGIN) + 1 :].split("#")[0])
        elif ref.startswith("/"):
            path = self.site / unquote(ref[1:].split("#")[0])
        elif re.match(r"^[a-z][a-z0-9+.-]*:", ref, re.IGNORECASE):
            return None
        else:
            path = source.parent / unquote(ref.split("#")[0])
        path = path.resolve()
        if path.suffix.lower() not in STATIC_SUFFIXES or not path.is_file():
            return None
        if not path.is_relative_to(self.site):
            return None
        return path

    def version(self, source: Path, ref: str) -> str:
        path = self.target(source, ref)
        if path is None:
            return ref
        if path not in self.hashes:
            self.hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()[:HASH_LEN]
        self.count += 1
        base, hash_mark, fragment = ref.partition("#")
        return f"{base}?v={self.hashes[path]}{hash_mark}{fragment}"

    def css_urls(self, source: Path, text: str) -> str:
        return CSS_URL_RE.sub(
            lambda m: m.group(1) + m.group(2) + self.version(source, m.group(3)) + m.group(4), text
        )

    def html(self, source: Path, text: str) -> str:
        text = self.css_urls(source, text)
        text = ATTR_RE.sub(
            lambda m: m.group(1) + self.version(source, m.group(2)) + m.group(3), text
        )

        def srcset(m: re.Match[str]) -> str:
            parts = []
            for candidate in m.group(2).split(","):
                bits = candidate.strip().split(maxsplit=1)
                if bits:
                    bits[0] = self.version(source, bits[0])
                parts.append(" ".join(bits))
            return m.group(1) + ", ".join(parts) + m.group(3)

        return SRCSET_RE.sub(srcset, text)


def stamp(site: Path) -> int:
    v = Versioner(site)
    files = 0
    for css in sorted(v.site.rglob("*.css")):
        text = css.read_text(encoding="utf-8")
        new = v.css_urls(css, text)
        if new != text:
            css.write_text(new, encoding="utf-8")
            files += 1
    for page in sorted(v.site.rglob("*.html")):
        text = page.read_text(encoding="utf-8")
        new = v.html(page, text)
        if new != text:
            page.write_text(new, encoding="utf-8")
            files += 1
    print(
        f"versioned {v.count} reference(s) to {len(v.hashes)} file(s) across {files} file(s)",
        file=sys.stderr,
    )
    return v.count


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    site = Path(sys.argv[1])
    if not (site / "index.html").is_file():
        print(f"{site}/index.html is missing: not a site directory", file=sys.stderr)
        return 1
    stamp(site)
    return 0


if __name__ == "__main__":
    sys.exit(main())
