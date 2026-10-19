"""Crawlers — hosted crawler/scraper actors run as ``crawler`` nodes, behind a provider seam.

* ``apify``   — the Apify API adapter (Store browsing, synchronous and start/poll/read runs).
* ``schema``  — pure: an actor input schema → exposed parameters + fixed input; coercion of a
                model's values into the actor's shapes; the result budget a model reads.
* ``runtime`` — the injected backend both runtimes call (connection auth at call time, the
                platform spend ceiling, provider dispatch).
* ``catalog`` — the proxied, cached public store and schema → node-config conversion the
                interface's Crawlers browser reads.
"""

from theygent_control_plane.crawlers.catalog import CrawlerCatalog, CrawlerCatalogError
from theygent_control_plane.crawlers.runtime import CrawlerBackend, CrawlerError, CrawlerLimitsIn

__all__ = [
    "CrawlerBackend",
    "CrawlerCatalog",
    "CrawlerCatalogError",
    "CrawlerError",
    "CrawlerLimitsIn",
]
