"""The Apify provider adapter — the parts of the Apify API a crawler node needs.

Browsing (the Store, an actor's details, its input schema) is public and needs no token. Running
an actor needs the user's API token, sent as ``Authorization: Bearer``.

Two ways to run, one per runtime:

* ``run_sync`` — ``POST /acts/{actor}/run-sync-get-dataset-items``. One request; Apify holds it
  for at most 300 s and answers ``201`` with the dataset items, or ``408 run-timeout-exceeded``
  while the run carries on. The response names no run, so its cost is not reported.
* ``start`` + ``get_run`` + ``dataset_items`` — ``POST /acts/{actor}/runs``, then
  ``GET /actor-runs/{id}`` (``waitForFinish`` holds each poll up to 60 s) until the run is
  terminal, then ``GET /datasets/{id}/items``. No time limit, each a short request — the shape a
  durable runtime journals as steps — and the run object carries ``usageTotalUsd``.

Both cap spend with ``maxTotalChargeUsd`` (what pay-per-event actors bill against) and
``maxItems`` (pay-per-result actors). Errors arrive as ``{"error": {"type", "message"}}`` and
surface as :class:`ApifyError` carrying the status and the API's error type.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx

API_BASE = "https://api.apify.com/v2"

#: How long Apify holds a synchronous run request before answering ``408``.
SYNC_LIMIT_S = 300

#: The longest ``waitForFinish`` one run poll may ask for.
MAX_WAIT_S = 60

#: Run statuses after which a run's dataset is final.
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"})


class ApifyError(RuntimeError):
    """A non-2xx answer from the Apify API (or an unusable body). ``kind`` is the API's error
    ``type`` (``invalid-input``, ``token-not-provided``, ``run-timeout-exceeded``, …)."""

    def __init__(self, status: int, kind: str, message: str) -> None:
        super().__init__(f"{status} {kind}: {message}")
        self.status = status
        self.kind = kind
        self.message = message


def actor_path(actor: str) -> str:
    """``apify/rag-web-browser`` → ``apify~rag-web-browser``, the form the API's paths use."""
    return actor.replace("/", "~")


class ApifyClient:
    """An async client over one API base. ``token`` is optional — the Store and public actors'
    schemas need none. ``transport`` lets tests route requests to an in-process fake."""

    def __init__(
        self,
        token: str | None = None,
        *,
        base: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.base = base.rstrip("/")
        self._http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_s),
            transport=transport,
            # The request carries the bearer token: never replay it at a host a redirect names.
            follow_redirects=False,
        )

    async def __aenter__(self) -> ApifyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── transport ───────────────────────────────────────────────────────────────────

    async def _get(
        self, path: str, params: Mapping[str, Any] | None = None, *, read_s: float | None = None
    ) -> Any:
        # The API rate-limits per resource and answers 429 with a short back-off; reads are safe to
        # repeat, so retry a few times before giving up.
        timeout = httpx.Timeout(30.0, read=read_s) if read_s else None
        resp: httpx.Response | None = None
        for attempt in range(4):
            resp = await self._send("GET", path, params=params, limits=timeout)
            if resp.status_code != 429:
                break
            await asyncio.sleep(0.5 * 2**attempt)
        assert resp is not None
        return self._unwrap(resp)

    async def _post(self, path: str, params: Mapping[str, Any], body: Any, *, read_s: float) -> Any:
        resp = await self._send(
            "POST", path, params=params, json_body=body, limits=httpx.Timeout(30.0, read=read_s)
        )
        return self._unwrap(resp)

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        limits: httpx.Timeout | None = None,
    ) -> httpx.Response:
        kwargs: dict[str, Any] = {"params": dict(params or {})}
        if json_body is not None:
            kwargs["json"] = json_body
        if limits is not None:
            kwargs["timeout"] = limits
        try:
            return await self._http.request(method, f"{self.base}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise ApifyError(504, "timeout", f"no answer from {self.base}: {exc!r}") from exc
        except httpx.HTTPError as exc:
            reason = str(exc) or type(exc).__name__
            raise ApifyError(502, "unreachable", f"{self.base} unreachable: {reason}") from exc

    @staticmethod
    def _unwrap(resp: httpx.Response) -> Any:
        if resp.status_code >= 400:
            kind, message = "error", resp.text[:300]
            try:
                err = resp.json().get("error") or {}
                kind = str(err.get("type") or kind)
                message = str(err.get("message") or message)
            except (ValueError, AttributeError):
                pass
            raise ApifyError(resp.status_code, kind, message)
        try:
            body = resp.json()
        except ValueError as exc:
            raise ApifyError(resp.status_code, "invalid-response", "the body is not JSON") from exc
        return body.get("data", body) if isinstance(body, dict) else body

    # ── browsing (public) ───────────────────────────────────────────────────────────

    async def store(
        self,
        search: str = "",
        *,
        sort_by: str = "popularity",
        category: str | None = None,
        limit: int = 24,
        offset: int = 0,
    ) -> dict[str, Any]:
        """One page of the public Store: ``{total, count, offset, limit, items}``."""
        params: dict[str, Any] = {"sortBy": sort_by, "limit": limit, "offset": offset}
        if search:
            params["search"] = search
        if category:
            params["category"] = category
        page = await self._get("/store", params)
        if not isinstance(page, dict) or not isinstance(page.get("items"), list):
            raise ApifyError(502, "invalid-response", "the Store returned no item list")
        return page

    async def actor(self, actor: str) -> dict[str, Any]:
        """An actor's public details (title, description, README summary, default run options,
        pricing). Public actors answer without a token."""
        data = await self._get(f"/acts/{actor_path(actor)}")
        if not isinstance(data, dict):
            raise ApifyError(502, "invalid-response", f"no details for {actor!r}")
        return data

    async def input_schema(self, actor: str) -> dict[str, Any] | None:
        """The input schema of the actor's default build. The build carries it both as a JSON
        string (``inputSchema``) and parsed (``actorDefinition.input``); either may be absent."""
        build = await self._get(f"/acts/{actor_path(actor)}/builds/default")
        if not isinstance(build, dict):
            return None
        schema: Any = (build.get("actorDefinition") or {}).get("input") or build.get("inputSchema")
        if isinstance(schema, str):
            try:
                schema = json.loads(schema)
            except ValueError:
                return None
        return schema if isinstance(schema, dict) else None

    # ── running (needs a token) ─────────────────────────────────────────────────────

    @staticmethod
    def _run_params(
        timeout_s: int, max_items: int | None, max_charge_usd: float | None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"timeout": timeout_s}
        if max_items is not None:
            params["maxItems"] = max_items
        if max_charge_usd is not None:
            params["maxTotalChargeUsd"] = max_charge_usd
        return params

    async def run_sync(
        self,
        actor: str,
        run_input: dict[str, Any],
        *,
        timeout_s: int,
        max_items: int | None,
        max_charge_usd: float | None,
        item_limit: int,
        fields: list[str] | None = None,
    ) -> list[Any]:
        """Run the actor and wait for its dataset items in one request. ``timeout_s`` is capped
        at the synchronous limit, so the run itself stops when the request does — no run is left
        spending after the caller has given up."""
        timeout_s = max(1, min(timeout_s, SYNC_LIMIT_S))
        params = self._run_params(timeout_s, max_items, max_charge_usd)
        params.update({"limit": item_limit, "clean": "true"})
        if fields:
            params["fields"] = ",".join(fields)
        items = await self._post(
            f"/acts/{actor_path(actor)}/run-sync-get-dataset-items",
            params,
            run_input,
            read_s=timeout_s + 30,
        )
        if not isinstance(items, list):
            raise ApifyError(502, "invalid-response", "the run returned no item list")
        return items[:item_limit]

    async def start(
        self,
        actor: str,
        run_input: dict[str, Any],
        *,
        timeout_s: int,
        max_items: int | None,
        max_charge_usd: float | None,
    ) -> dict[str, Any]:
        """Start a run and return the run object (``id``, ``status``, ``defaultDatasetId``)."""
        run = await self._post(
            f"/acts/{actor_path(actor)}/runs",
            self._run_params(timeout_s, max_items, max_charge_usd),
            run_input,
            read_s=60,
        )
        if not isinstance(run, dict) or not run.get("id"):
            raise ApifyError(502, "invalid-response", "the run start returned no run id")
        return run

    async def get_run(self, run_id: str, *, wait_s: int = MAX_WAIT_S) -> dict[str, Any]:
        """The run object, after Apify has held the request until the run finishes or ``wait_s``
        (at most 60) elapses — so polling a long run costs one request a minute."""
        wait_s = max(0, min(wait_s, MAX_WAIT_S))
        run = await self._get(
            f"/actor-runs/{run_id}", {"waitForFinish": wait_s}, read_s=wait_s + 30
        )
        if not isinstance(run, dict):
            raise ApifyError(502, "invalid-response", f"no run {run_id!r}")
        return run

    async def dataset_items(
        self, dataset_id: str, *, limit: int, fields: list[str] | None = None
    ) -> list[Any]:
        params: dict[str, Any] = {"limit": limit, "clean": "true"}
        if fields:
            params["fields"] = ",".join(fields)
        items = await self._get(f"/datasets/{dataset_id}/items", params)
        if not isinstance(items, list):
            raise ApifyError(502, "invalid-response", f"dataset {dataset_id!r} returned no items")
        return items[:limit]
