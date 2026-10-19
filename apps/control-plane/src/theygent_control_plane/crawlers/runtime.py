"""The crawler backend both runtimes call — the provider seam for ``crawler`` nodes.

The walker and the durable steps hand it serializable inputs (provider, actor, connection id,
the resolved actor input, the node's limits) and get back plain data. It owns three things the
node config must not:

* **The token.** The connection is resolved through the same server-side resolver http tools use,
  inside the call; the token becomes a bearer header for that request only and never reaches the
  IR, a span, or the journal.
* **The spend ceiling.** Every run's ``maxTotalChargeUsd`` is the node's cap, held under the
  platform setting ``crawler.max_charge_usd`` (a live accessor), so no agent can raise its own.
* **The provider.** ``provider`` selects an adapter; only ``apify`` exists. An unknown provider is
  a loud error, never a silent pass-through.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from theygent_control_plane.crawlers.apify import (
    API_BASE,
    MAX_WAIT_S,
    TERMINAL_STATUSES,
    ApifyClient,
    ApifyError,
)

#: The platform ceiling used when no live setting is wired (tests, a degraded setup).
DEFAULT_MAX_CHARGE_USD = 1.0


class CrawlerError(RuntimeError):
    """A crawler call that could not run or did not succeed — an actionable message the node binds
    to its ``err`` handle (or feeds back to the model), never a run failure. ``transient`` marks a
    provider outage or rate limit, which a read (a poll, a dataset read) may simply retry."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class CrawlerLimitsIn:
    """The node's ``limits`` as plain data (the camelCase IR block, read by key)."""

    timeout_seconds: int
    max_charge_usd: float | None
    max_items: int | None
    item_limit: int
    fields: list[str]

    @classmethod
    def from_wire(cls, limits: Mapping[str, Any]) -> CrawlerLimitsIn:
        return cls(
            timeout_seconds=int(limits.get("timeoutSeconds", 120)),
            max_charge_usd=limits.get("maxChargeUsd"),
            max_items=limits.get("maxItems"),
            item_limit=int(limits.get("itemLimit", 10)),
            fields=list(limits.get("fields") or []),
        )


ConnectionLookup = Callable[[str], Awaitable[Any]]


class CrawlerBackend:
    """Runs crawler calls for one process. ``resolver`` is the connection resolver
    (connection id → ``ResolvedConnection`` with the decrypted secret, or ``None``);
    ``max_charge_usd`` is the platform ceiling — a number or a zero-arg live accessor.
    ``transport`` routes every provider request through a test transport."""

    def __init__(
        self,
        resolver: ConnectionLookup | None,
        *,
        max_charge_usd: float | Callable[[], Any] = DEFAULT_MAX_CHARGE_USD,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._resolver = resolver
        self._ceiling = max_charge_usd
        self._transport = transport

    # ── spend ──────────────────────────────────────────────────────────────────────

    def ceiling_usd(self) -> float:
        raw = self._ceiling() if callable(self._ceiling) else self._ceiling
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return DEFAULT_MAX_CHARGE_USD
        return value if value > 0 else DEFAULT_MAX_CHARGE_USD

    def charge_cap(self, node_cap: float | None) -> float:
        """The per-run charge cap: the node's own cap held under the platform ceiling (the
        ceiling alone when the node sets none)."""
        ceiling = self.ceiling_usd()
        return ceiling if node_cap is None else min(float(node_cap), ceiling)

    # ── the provider client ────────────────────────────────────────────────────────

    async def _client(self, provider: str, connection_id: str) -> ApifyClient:
        if provider != "apify":
            raise CrawlerError(f"unknown crawler provider {provider!r}")
        if self._resolver is None:
            raise CrawlerError("no connection resolver is configured on this runtime")
        try:
            conn = await self._resolver(connection_id)
        except Exception as exc:  # e.g. an undecryptable secret
            raise CrawlerError(f"connection {connection_id!r}: {exc}") from exc
        if conn is None:
            raise CrawlerError(f"connection {connection_id!r} not found or disabled")
        config = conn.config or {}
        auth_type = (config.get("auth") or {}).get("type")
        if auth_type not in (None, "bearer"):
            raise CrawlerError(
                f"connection {connection_id!r} uses {auth_type!r} auth; an Apify connection "
                "authenticates with a bearer token (its secret is the API token)"
            )
        if not conn.secret:
            raise CrawlerError(
                f"connection {connection_id!r} has no API token — set the token on the "
                "connection, then run again"
            )
        base = str(config.get("baseUrl") or API_BASE)
        return ApifyClient(conn.secret, base=base, transport=self._transport)

    @staticmethod
    def _fail(actor: str, exc: ApifyError) -> CrawlerError:
        if exc.kind == "run-timeout-exceeded":
            return CrawlerError(
                f"crawler {actor!r}: the run did not finish within the 300-second synchronous "
                "limit — lower the work per call, or run the agent durably so the crawl can "
                "take longer"
            )
        if exc.status in (401, 403):
            return CrawlerError(
                f"crawler {actor!r}: the provider refused the token ({exc}) — check the "
                "connection's API token"
            )
        return CrawlerError(
            f"crawler {actor!r} failed: {exc}", transient=exc.status >= 500 or exc.status == 429
        )

    # ── one synchronous run (the interactive runtime) ──────────────────────────────

    async def run_sync(
        self,
        *,
        provider: str,
        actor: str,
        connection: str,
        run_input: dict[str, Any],
        limits: CrawlerLimitsIn,
    ) -> dict[str, Any]:
        """Run the actor and return ``{items, chargeCapUsd}``. The provider's synchronous call
        names no run, so the run's cost is not reported here."""
        cap = self.charge_cap(limits.max_charge_usd)
        client = await self._client(provider, connection)
        try:
            items = await client.run_sync(
                actor,
                run_input,
                timeout_s=limits.timeout_seconds,
                max_items=limits.max_items,
                max_charge_usd=cap,
                item_limit=limits.item_limit,
                fields=limits.fields or None,
            )
        except ApifyError as exc:
            raise self._fail(actor, exc) from exc
        finally:
            await client.aclose()
        return {"items": items, "chargeCapUsd": cap}

    # ── start → poll → read (the durable runtime's steps) ──────────────────────────

    async def start(
        self,
        *,
        provider: str,
        actor: str,
        connection: str,
        run_input: dict[str, Any],
        limits: CrawlerLimitsIn,
    ) -> dict[str, Any]:
        """Start a run: ``{runId, datasetId, status, chargeCapUsd}``."""
        cap = self.charge_cap(limits.max_charge_usd)
        client = await self._client(provider, connection)
        try:
            run = await client.start(
                actor,
                run_input,
                timeout_s=limits.timeout_seconds,
                max_items=limits.max_items,
                max_charge_usd=cap,
            )
        except ApifyError as exc:
            raise self._fail(actor, exc) from exc
        finally:
            await client.aclose()
        return {
            "runId": str(run["id"]),
            "datasetId": run.get("defaultDatasetId"),
            "status": run.get("status"),
            "chargeCapUsd": cap,
        }

    async def poll(
        self, *, provider: str, actor: str, connection: str, run_id: str, wait_s: int = MAX_WAIT_S
    ) -> dict[str, Any]:
        """One poll of a run: ``{status, terminal, datasetId, usageTotalUsd}``. The provider holds
        the request until the run finishes or ``wait_s`` elapses."""
        client = await self._client(provider, connection)
        try:
            run = await client.get_run(run_id, wait_s=wait_s)
        except ApifyError as exc:
            raise self._fail(actor, exc) from exc
        finally:
            await client.aclose()
        status = run.get("status")
        usage = run.get("usageTotalUsd")
        return {
            "status": status,
            "terminal": status in TERMINAL_STATUSES,
            "datasetId": run.get("defaultDatasetId"),
            "usageTotalUsd": float(usage) if isinstance(usage, (int, float)) else None,
        }

    async def items(
        self,
        *,
        provider: str,
        actor: str,
        connection: str,
        dataset_id: str,
        limits: CrawlerLimitsIn,
    ) -> list[Any]:
        client = await self._client(provider, connection)
        try:
            return await client.dataset_items(
                dataset_id, limit=limits.item_limit, fields=limits.fields or None
            )
        except ApifyError as exc:
            raise self._fail(actor, exc) from exc
        finally:
            await client.aclose()
