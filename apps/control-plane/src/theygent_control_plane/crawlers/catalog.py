"""Browsing crawlers: the provider's public store, proxied and cached, and an actor's input
schema converted into a ``crawler`` node config.

The interface never calls the provider: it asks the control plane, which fetches the public
Store (no token), caches each answer for a few minutes, and normalizes it into what a browse card
shows — category, author, pricing model and headline price, users in the last 30 days, and the
30-day run success rate (the reliability signal: an actor that times out on most runs reads as
such before anyone installs it). An actor's detail adds its README summary and its input fields
as a form renders them, plus the suggested node config from :func:`schema.convert`.

No side effects beyond the cache: this module never writes the database. Installing is the
interface dropping the returned config onto the canvas.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from theygent_control_plane.crawlers.apify import API_BASE, ApifyClient, ApifyError
from theygent_control_plane.crawlers.schema import convert, form_fields

#: The crawler providers the catalog can browse — the same closed vocabulary as the IR's.
PROVIDERS = ("apify",)

#: The limits a freshly installed node starts with: a two-minute run, a quarter-dollar charge cap,
#: five items read back, and a budget a small local model's context can hold.
_DEFAULT_LIMITS: dict[str, Any] = {
    "timeoutSeconds": 120,
    "maxChargeUsd": 0.25,
    "maxItems": 10,
    "itemLimit": 5,
    "resultChars": 6000,
    "fields": [],
}


class CrawlerCatalogError(RuntimeError):
    """The store could not be reached or answered with an unusable payload (→ 502), or an actor
    has no readable input schema (→ 404)."""

    def __init__(self, message: str, *, status: int = 502, code: str = "crawler_catalog_error"):
        super().__init__(message)
        self.status = status
        self.code = code


def _success_rate(stats: dict[str, Any]) -> tuple[float | None, int]:
    runs = stats.get("publicActorRunStats30Days") or {}
    total = runs.get("TOTAL") or 0
    if not isinstance(total, int) or total <= 0:
        return None, 0
    return round(float(runs.get("SUCCEEDED") or 0) / total, 4), total


def headline_price(pricing: dict[str, Any] | None) -> dict[str, Any]:
    """The pricing model plus the one price a card shows. Pay-per-event actors price each event;
    the headline is the primary event (the one billed per result), at the free plan's tier when
    the price is tiered. Older models price a unit (a result, a month)."""
    pricing = pricing or {}
    model = pricing.get("pricingModel") or "UNKNOWN"
    out: dict[str, Any] = {"model": model, "priceUsd": None, "unit": None}
    if model == "PAY_PER_EVENT":
        events = (pricing.get("pricingPerEvent") or {}).get("actorChargeEvents") or {}
        chosen = next((e for e in events.values() if e.get("isPrimaryEvent")), None)
        if chosen is None:
            chosen = next((e for e in events.values() if not e.get("isOneTimeEvent")), None)
        if chosen:
            price = chosen.get("eventPriceUsd")
            tiers = chosen.get("eventTieredPricingUsd") or {}
            if price is None and tiers:
                first = tiers.get("FREE") or next(iter(tiers.values()))
                price = (first or {}).get("tieredEventPriceUsd")
            out["priceUsd"] = price
            out["unit"] = chosen.get("eventTitle")
    elif pricing.get("pricePerUnitUsd") is not None:
        out["priceUsd"] = pricing.get("pricePerUnitUsd")
        out["unit"] = pricing.get("unitName") or (
            "month" if model == "FLAT_PRICE_PER_MONTH" else None
        )
    return out


def current_pricing(pricing_infos: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The pricing in force now from an actor's pricing history: the latest entry that has
    started (a scheduled change is listed before it applies)."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    started = [
        p for p in pricing_infos or [] if isinstance(p, dict) and str(p.get("startedAt", "")) <= now
    ]
    if not started:
        return None
    return max(started, key=lambda p: str(p.get("startedAt", "")))


def card(provider: str, item: dict[str, Any]) -> dict[str, Any]:
    """One normalized browse card from a store item."""
    stats = item.get("stats") or {}
    rate, runs = _success_rate(stats)
    username, name = item.get("username") or "", item.get("name") or ""
    return {
        "provider": provider,
        "actor": f"{username}/{name}",
        "title": item.get("title") or name,
        "description": item.get("description") or "",
        "author": item.get("userFullName") or username,
        "categories": list(item.get("categories") or []),
        "pictureUrl": item.get("pictureUrl"),
        "url": item.get("url"),
        "pricing": headline_price(item.get("currentPricingInfo")),
        "users30d": stats.get("totalUsers30Days"),
        "runs30d": runs,
        "successRate30d": rate,
        "rating": stats.get("actorReviewRating") or item.get("actorReviewRating"),
        "reviews": stats.get("actorReviewCount") or item.get("actorReviewCount"),
    }


def _tool_name(actor: str) -> str:
    """A function-name-safe node id suggestion from an actor id (``apify/rag-web-browser`` →
    ``rag_web_browser``)."""
    tail = actor.replace("~", "/").split("/")[-1]
    cleaned = "".join(c if c.isalnum() else "_" for c in tail).strip("_").lower()
    return cleaned or "crawler"


def node_config(
    provider: str,
    actor: str,
    schema: dict[str, Any],
    *,
    description: str | None,
    expose: list[str] | None = None,
    scalar: list[str] | None = None,
    fixed: dict[str, Any] | None = None,
    connection: str | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A complete ``crawler`` node config (camelCase, as the IR stores it) from an actor's input
    schema, plus what the builder should know: the fields nobody can fill and the suggested node
    id. ``connection`` may be empty — the graph won't validate until one is picked."""
    conv = convert(schema, expose=expose, scalar=scalar, fixed=fixed)
    config: dict[str, Any] = {
        "provider": provider,
        "actor": actor,
        "connection": connection or "",
        "input": conv.input,
        "expose": conv.expose,
        "scalar": conv.scalar,
        "parameterSchema": conv.parameter_schema,
        "description": description,
        "limits": {**_DEFAULT_LIMITS, **(limits or {})},
    }
    return {
        "config": config,
        "unfillable": conv.unfillable,
        "totalFields": conv.total_fields,
        "suggestedId": _tool_name(actor),
    }


class CrawlerCatalog:
    """Read-only, TTL-cached access to the providers' public stores. The http transport and clock
    are injectable so the fast suite runs against a fixture store with a hand-advanced clock."""

    def __init__(
        self,
        *,
        base: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_ttl_s: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base = base
        self._transport = transport
        self._ttl = cache_ttl_s
        self._clock = clock
        self._cache: dict[tuple[Any, ...], tuple[float, Any]] = {}

    def _client(self) -> ApifyClient:
        return ApifyClient(base=self._base, transport=self._transport)

    async def _cached(
        self, key: tuple[Any, ...], fetch: Callable[[ApifyClient], Any], *, label: str = ""
    ) -> Any:
        now = self._clock()
        hit = self._cache.get(key)
        if hit is not None and now - hit[0] < self._ttl:
            return hit[1]
        async with self._client() as client:
            try:
                value = await fetch(client)
            except ApifyError as exc:
                if exc.status == 404:
                    raise CrawlerCatalogError(
                        f"the store has no actor {label!r}", status=404, code="crawler_not_found"
                    ) from exc
                raise CrawlerCatalogError(f"the crawler store is unavailable: {exc}") from exc
        self._cache[key] = (now, value)
        return value

    @staticmethod
    def _provider(provider: str) -> str:
        if provider not in PROVIDERS:
            raise CrawlerCatalogError(
                f"unknown crawler provider {provider!r}; known: {', '.join(PROVIDERS)}",
                status=400,
                code="crawler_provider_unknown",
            )
        return provider

    async def search(
        self,
        provider: str = "apify",
        *,
        search: str = "",
        category: str | None = None,
        sort_by: str = "popularity",
        limit: int = 24,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page of store cards: ``{entries, total, offset, limit}``."""
        provider = self._provider(provider)
        key = (provider, "search", search, category, sort_by, limit, offset)
        page = await self._cached(
            key,
            lambda c: c.store(
                search, sort_by=sort_by, category=category, limit=limit, offset=offset
            ),
        )
        entries = [card(provider, item) for item in page["items"] if isinstance(item, dict)]
        return {
            "entries": entries,
            "total": page.get("total"),
            "offset": page.get("offset", offset),
            "limit": page.get("limit", limit),
        }

    async def schema(self, provider: str, actor: str) -> dict[str, Any]:
        provider = self._provider(provider)
        schema = await self._cached(
            (provider, "schema", actor), lambda c: c.input_schema(actor), label=actor
        )
        if not schema:
            raise CrawlerCatalogError(
                f"actor {actor!r} publishes no input schema",
                status=404,
                code="crawler_schema_unavailable",
            )
        return schema

    async def detail(self, provider: str, actor: str) -> dict[str, Any]:
        """The actor's card, README summary, default run options, input fields (form-ready), and
        the suggested node config."""
        provider = self._provider(provider)
        info = await self._cached((provider, "actor", actor), lambda c: c.actor(actor), label=actor)
        schema = await self.schema(provider, actor)
        item = {
            **info,
            "currentPricingInfo": info.get("currentPricingInfo")
            or current_pricing(info.get("pricingInfos")),
            "url": info.get("url") or f"https://apify.com/{actor.replace('~', '/')}",
        }
        description = info.get("description") or info.get("title")
        return {
            "entry": card(provider, item),
            "readmeSummary": info.get("readmeSummary"),
            "defaultRunOptions": info.get("defaultRunOptions") or {},
            "fields": form_fields(schema),
            "suggested": node_config(provider, actor, schema, description=description),
        }

    async def convert(
        self,
        provider: str,
        actor: str,
        *,
        expose: list[str] | None,
        scalar: list[str] | None,
        fixed: dict[str, Any] | None,
        description: str | None,
        connection: str | None,
        limits: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Re-derive a node config for the builder's choice of exposed fields — the inspector
        calls this when the ticked fields change, so the stored ``parameterSchema`` always comes
        from the actor's real schema."""
        schema = await self.schema(provider, actor)
        return node_config(
            provider,
            actor,
            schema,
            description=description,
            expose=expose,
            scalar=scalar,
            fixed=fixed,
            connection=connection,
            limits=limits,
        )
