#!/usr/bin/env python3
"""Fail if the static marketing site's pages drift out of shape for search and answer engines.

The site under apps/web/ is hand-written HTML with no build step, so nothing generates the
sitemap or the structured data — they are kept in step by hand, and this check is what keeps
them honest:

* every indexable page is listed in sitemap.xml, and every sitemap URL is a real page;
* every indexable page has a <title>, a meta description, exactly one <h1>, and a canonical
  URL equal to its own address;
* every JSON-LD block parses;
* a page's FAQPage structured data matches its visible FAQ question for question and word for
  word (Google's policy forbids structured data that is not visible on the page);
* 404.html is noindex and stays out of the sitemap;
* every theygent.ai URL in llms.txt is a real page.

Usage: check_web_pages.py <site-dir>
"""

from __future__ import annotations

import html
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

ORIGIN = "https://theygent.ai"
NOT_FOUND = "404.html"
# Typographic quotes in visible copy versus straight quotes in JSON-LD are the same words.
QUOTES = str.maketrans({chr(0x2018): "'", chr(0x2019): "'", chr(0x201C): '"', chr(0x201D): '"'})


def normalize(fragment: str) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment)).translate(QUOTES)
    return re.sub(r"\s+", " ", text).strip()


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: str | None = None
        self.description: str | None = None
        self.canonical: str | None = None
        self.robots: str = ""
        self.h1_count = 0
        self.ld_blocks: list[str] = []
        self._in_title = False
        self._in_ld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
            self.title = ""
        elif tag == "meta" and a.get("name") == "description":
            self.description = a.get("content", "")
        elif tag == "meta" and a.get("name") == "robots":
            self.robots = a.get("content", "")
        elif tag == "link" and a.get("rel") == "canonical":
            self.canonical = a.get("href", "")
        elif tag == "h1":
            self.h1_count += 1
        elif tag == "script" and a.get("type") == "application/ld+json":
            self._in_ld = True
            self.ld_blocks.append("")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag == "script":
            self._in_ld = False

    def handle_data(self, data: str) -> None:
        if self._in_title and self.title is not None:
            self.title += data
        if self._in_ld:
            self.ld_blocks[-1] += data


def url_for(site: Path, page: Path) -> str:
    rel = page.relative_to(site).as_posix()
    if rel == "index.html":
        return ORIGIN + "/"
    if rel.endswith("/index.html"):
        return f"{ORIGIN}/{rel[: -len('index.html')]}"
    return f"{ORIGIN}/{rel}"


def page_for(site: Path, url: str) -> Path:
    path = url[len(ORIGIN) :].split("#", 1)[0].lstrip("/")
    if path == "" or path.endswith("/"):
        return site / path / "index.html"
    return site / path


def visible_faq(text: str) -> list[tuple[str, str]]:
    items = []
    for block in re.findall(r'<details class="faq-item">(.*?)</details>', text, re.S):
        q = re.search(r"<h3>(.*?)</h3>", block, re.S)
        a = re.search(r"</summary>\s*<p>(.*?)</p>", block, re.S)
        if q and a:
            items.append((normalize(q.group(1)), normalize(a.group(1))))
    return items


def ld_faq(blocks: list[dict]) -> list[tuple[str, str]] | None:
    for block in blocks:
        for node in block.get("@graph", [block]):
            if node.get("@type") == "FAQPage":
                return [
                    (normalize(q["name"]), normalize(q["acceptedAnswer"]["text"]))
                    for q in node.get("mainEntity", [])
                ]
    return None


def check_page(site: Path, page: Path, errors: list[str]) -> None:
    name = page.relative_to(site).as_posix()
    text = page.read_text(encoding="utf-8")
    p = PageParser()
    p.feed(text)

    blocks: list[dict] = []
    for raw in p.ld_blocks:
        try:
            blocks.append(json.loads(raw))
        except json.JSONDecodeError as exc:
            errors.append(f"{name}: JSON-LD does not parse ({exc})")

    if name == NOT_FOUND:
        if "noindex" not in p.robots:
            errors.append(f"{name}: must carry <meta name=robots content=noindex>")
        return

    if not (p.title or "").strip():
        errors.append(f"{name}: missing <title>")
    if not (p.description or "").strip():
        errors.append(f"{name}: missing meta description")
    if p.h1_count != 1:
        errors.append(f"{name}: has {p.h1_count} <h1> elements, expected exactly 1")
    if p.canonical != url_for(site, page):
        errors.append(f"{name}: canonical is {p.canonical!r}, expected {url_for(site, page)!r}")

    shown, marked = visible_faq(text), ld_faq(blocks)
    if shown and marked is None:
        errors.append(f"{name}: has a visible FAQ but no FAQPage structured data")
    elif marked is not None and marked != shown:
        if len(marked) != len(shown):
            errors.append(
                f"{name}: FAQPage has {len(marked)} questions, the visible FAQ has {len(shown)}"
            )
        for i, (m, s) in enumerate(zip(marked, shown, strict=False), 1):
            if m[0] != s[0]:
                errors.append(
                    f"{name}: FAQ #{i} question differs:\n    json-ld: {m[0]}\n    visible: {s[0]}"
                )
            elif m[1] != s[1]:
                errors.append(
                    f"{name}: FAQ #{i} answer differs ({s[0]}):"
                    f"\n    json-ld: {m[1]}\n    visible: {s[1]}"
                )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_web_pages.py <site-dir>", file=sys.stderr)
        return 2
    site = Path(sys.argv[1]).resolve()
    errors: list[str] = []

    pages = sorted(site.rglob("*.html"))
    for page in pages:
        check_page(site, page, errors)

    listed = re.findall(r"<loc>([^<]+)</loc>", (site / "sitemap.xml").read_text(encoding="utf-8"))
    indexable = {url_for(site, p) for p in pages if p.relative_to(site).as_posix() != NOT_FOUND}
    for url in sorted(indexable - set(listed)):
        errors.append(f"sitemap.xml: page {url} is not listed")
    for url in listed:
        if url not in indexable:
            errors.append(f"sitemap.xml: {url} is not a page on the site")

    llms = (site / "llms.txt").read_text(encoding="utf-8")
    for url in re.findall(r"\((https://theygent\.ai/[^)\s]*)\)", llms):
        if not page_for(site, url).is_file():
            errors.append(f"llms.txt: {url} is not a page on the site")

    if errors:
        print("FAIL: the site's pages are out of shape:", file=sys.stderr)
        for e in errors:
            print(f"  {e}", file=sys.stderr)
        return 1
    print(f"OK: {len(pages)} page(s), the sitemap, structured data, and llms.txt links all agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
