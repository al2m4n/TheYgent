"""MkDocs hook: write ``llms-full.txt`` — the whole user documentation as one Markdown file.

Answer engines and coding agents read a site far more reliably from one plain-text file than
by crawling every page, so each build emits every page's source Markdown, in navigation order,
under its canonical URL. The file lands at the root of each built version
(``/latest/llms-full.txt``), and the domain-root ``llms.txt`` links to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# src_uri -> (title, canonical URL, markdown); filled per build, written in nav order.
_pages: dict[str, tuple[str, str, str]] = {}
_order: list[str] = []


def on_nav(nav: Any, config: Any, files: Any) -> Any:
    _order[:] = [page.file.src_uri for page in nav.pages]
    _pages.clear()
    return nav


def on_page_markdown(markdown: str, page: Any, config: Any, files: Any) -> str:
    title = page.title or page.file.src_uri
    _pages[page.file.src_uri] = (title, page.canonical_url or "", markdown)
    return markdown


def on_post_build(config: Any) -> None:
    parts = [
        f"# {config.site_name} — full text\n\n"
        f"> {' '.join(config.site_description.split())}\n\n"
        f"Every page of the user documentation, in navigation order. "
        f"Browsable at {config.site_url}\n"
    ]
    for uri in _order:
        if uri not in _pages:
            continue
        title, url, markdown = _pages[uri]
        body = markdown.strip()
        if not body.startswith("# "):
            body = f"# {title}\n\n{body}"
        parts.append(f"---\n\nSource: {url}\n\n{body}\n")
    Path(config.site_dir, "llms-full.txt").write_text("\n".join(parts), encoding="utf-8")
