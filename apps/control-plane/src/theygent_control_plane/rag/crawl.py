"""Docs-site crawling behind one function — crawlee for the walk, trafilatura for the text.

Two crawler shapes, one seam: the static crawler (plain HTTP + BeautifulSoup link discovery)
is the default — cheap, no browser; sources whose sites are JS-rendered opt into the
Playwright crawler per source (``render_js``). Browser binaries are NEVER bundled: the
Playwright path fails with an actionable install hint until the user runs
``playwright install chromium`` once.

Scope discipline: the crawl stays same-origin AND under the root URL's path prefix (pointing
at ``/docs/`` must not wander into the marketing site), respects robots.txt, and is bounded
by ``max_pages`` — an unbounded crawl is never the default. Storage is in-memory per crawl
(nothing lands on disk); extraction (boilerplate removal → markdown) happens per page so the
caller can chunk/embed incrementally and report honest progress.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_MAX_PAGES = 200


@dataclass(frozen=True)
class CrawlConfig:
    root_url: str
    max_pages: int = DEFAULT_MAX_PAGES
    render_js: bool = False


@dataclass(frozen=True)
class CrawledPage:
    url: str
    title: str | None
    markdown: str


class BrowserNotInstalled(RuntimeError):
    """The source asked for JS rendering but no Playwright browser is installed."""


def _path_prefix(root_url: str) -> str:
    """The URL prefix the crawl is confined to. A root that names a page
    (``…/guide/intro.html`` — a dotted last segment) scopes to its directory (``…/guide/``);
    a directory root typed WITHOUT the trailing slash (``…/docs`` — the common way users type
    one) scopes to itself (``…/docs/``), never silently to its parent — pointing at ``/docs``
    must not crawl the whole origin."""
    parsed = urlparse(root_url)
    path = parsed.path or "/"
    if not path.endswith("/"):
        last = path.rsplit("/", 1)[-1]
        path = path.rsplit("/", 1)[0] + "/" if "." in last else path + "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def _extract(content: bytes | str, url: str) -> CrawledPage | None:
    """Boilerplate-stripped markdown + title for one fetched page; ``None`` when the page has
    no extractable main content (nav/index shells) — skipped, not an error."""
    import trafilatura

    markdown = trafilatura.extract(
        content, url=url, output_format="markdown", include_links=False, include_tables=True
    )
    if not markdown or not markdown.strip():
        return None
    title: str | None = None
    try:
        html = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
        meta = trafilatura.extract_metadata(html, default_url=url)
        title = meta.title if meta is not None else None
    except Exception:  # metadata is a nicety; extraction already succeeded
        title = None
    return CrawledPage(url=url, title=title, markdown=markdown.strip())


async def crawl_site(
    config: CrawlConfig,
    on_page: Callable[[CrawledPage], Awaitable[None]],
    *,
    on_visit: Callable[[str], Awaitable[None]] | None = None,
) -> None:
    """Crawl ``config.root_url`` and call ``on_page`` for every content page, in visit order.
    ``on_visit`` (if given) fires for every fetched URL — the live progress counter. Raises
    :class:`BrowserNotInstalled` with the install hint on the JS path when the browser binary
    is missing; other crawl-level failures propagate to the ingest service, which records them
    on the source."""
    # Imported here, not at module top: crawlee spins up its service locator on import-heavy
    # paths, and only the crawl ingest path needs it.
    from crawlee import ConcurrencySettings, Glob
    from crawlee.storage_clients import MemoryStorageClient

    prefix = _path_prefix(config.root_url)
    # The slashless spelling of the prefix directory is also in scope — pages commonly link
    # ``…/docs`` while the canonical prefix is ``…/docs/``.
    include = [Glob(f"{prefix}**"), Glob(prefix.rstrip("/"))]
    common: dict = {
        "max_requests_per_crawl": config.max_pages,
        "respect_robots_txt_file": True,
        "storage_client": MemoryStorageClient(),  # per-crawl, in-memory, nothing on disk
        # Polite by default (docs sites are someone else's server); both knobs set because the
        # library's desired default exceeds a low max.
        "concurrency_settings": ConcurrencySettings(desired_concurrency=2, max_concurrency=4),
        "configure_logging": False,  # never hijack the app's logging config
    }

    if config.render_js:
        from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

        crawler = PlaywrightCrawler(headless=True, browser_type="chromium", **common)

        @crawler.router.default_handler
        async def handle_js(ctx: PlaywrightCrawlingContext) -> None:  # pragma: no cover - thin
            if on_visit is not None:
                await on_visit(ctx.request.loaded_url or ctx.request.url)
            html = await ctx.page.content()
            page = _extract(html, ctx.request.loaded_url or ctx.request.url)
            if page is not None:
                await on_page(page)
            await ctx.enqueue_links(strategy="same-origin", include=include)

    else:
        from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext

        crawler = BeautifulSoupCrawler(**common)

        @crawler.router.default_handler
        async def handle_static(ctx: BeautifulSoupCrawlingContext) -> None:
            if on_visit is not None:
                await on_visit(ctx.request.loaded_url or ctx.request.url)
            raw = await ctx.http_response.read()
            page = _extract(raw, ctx.request.loaded_url or ctx.request.url)
            if page is not None:
                await on_page(page)
            await ctx.enqueue_links(strategy="same-origin", include=include)

    try:
        await crawler.run([config.root_url])
    except Exception as exc:
        message = str(exc)
        if config.render_js and "Executable doesn't exist" in message:
            raise BrowserNotInstalled(
                "JS rendering needs a Playwright browser. Install once with: "
                "uv run --package theygent-control-plane playwright install chromium"
            ) from exc
        raise
