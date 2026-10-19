"""A local stand-in for the Apify API, for testing the crawler node with no Apify account.

A real threaded HTTP server on an ephemeral port, speaking the documented response shapes:

* the run API a crawler node uses — ``POST /v2/acts/{actor}/run-sync-get-dataset-items`` (201 +
  items), ``POST /v2/acts/{actor}/runs`` (201 + run), ``GET /v2/actor-runs/{id}`` and
  ``GET /v2/datasets/{id}/items`` — which needs ``Authorization: Bearer``;
* the public browse API the Crawlers browser proxies — ``GET /v2/store``,
  ``GET /v2/acts/{actor}`` and ``GET /v2/acts/{actor}/builds/default`` — which needs none.

Run input is checked the way Apify checks it before a run starts: a missing required field or a
``requestListSources`` field that isn't a list of ``{"url": ...}`` objects is ``400 invalid-input``.
Datasets are canned per actor, and their facts are invented, so a model's answer that states one
came from the tool result. Every run request is recorded (actor, mode, input, query, auth).

    with FakeApify() as fake:      # fake.base → http://127.0.0.1:<port>/v2
        ...
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

#: Trimmed input schemas in the actors' real shape (field names, types, editors, sections).
SCHEMAS: dict[str, dict[str, Any]] = {
    "apify~rag-web-browser": {
        "title": "Web browser",
        "type": "object",
        "schemaVersion": 1,
        "properties": {
            "query": {
                "title": "Search term or URL",
                "type": "string",
                "editor": "textfield",
                "description": "Search keywords, or the <b>URL</b> of one page to read.",
                "prefill": "local-first agents",
            },
            "maxResults": {
                "title": "Maximum results",
                "type": "integer",
                "description": "How many result pages to read.",
                "default": 3,
                "minimum": 1,
                "maximum": 100,
            },
            "outputFormats": {
                "title": "Output formats",
                "type": "array",
                "editor": "select",
                "description": "Formats to extract each page into.",
                "default": ["markdown"],
                "items": {"type": "string", "enum": ["text", "markdown", "html"]},
            },
            "requestTimeoutSecs": {
                "title": "Request timeout",
                "type": "integer",
                "editor": "hidden",
                "description": "Time budget per request.",
                "default": 40,
            },
            "proxyConfiguration": {
                "title": "Proxy",
                "type": "object",
                "editor": "proxy",
                "sectionCaption": "Advanced",
                "description": "Proxy settings.",
                "default": {"useApifyProxy": True},
            },
        },
        "required": ["query"],
    },
    "compass~crawler-google-places": {
        "title": "Places",
        "type": "object",
        "schemaVersion": 1,
        "properties": {
            "searchStringsArray": {
                "title": "Search terms",
                "type": "array",
                "editor": "stringList",
                "description": "What to search for on the map.",
            },
            "locationQuery": {
                "title": "Location",
                "type": "string",
                "editor": "textfield",
                "description": "Where to search.",
            },
            "maxCrawledPlacesPerSearch": {
                "title": "Places per search",
                "type": "integer",
                "description": "How many places to return per search term.",
                "minimum": 1,
            },
            "maxReviews": {
                "title": "Reviews per place",
                "type": "integer",
                "sectionCaption": "Reviews (paid add-on)",
                "description": "Reviews to extract per place; each review is billed.",
                "default": 0,
            },
        },
    },
    "apify~website-content-crawler": {
        "title": "Website crawler",
        "type": "object",
        "schemaVersion": 1,
        "properties": {
            "startUrls": {
                "title": "Start URLs",
                "type": "array",
                "editor": "requestListSources",
                "description": "Pages to start the crawl from.",
            },
            "maxCrawlPages": {
                "title": "Max pages",
                "type": "integer",
                "description": "Stop after this many pages.",
                "default": 10,
            },
        },
        "required": ["startUrls"],
    },
}

#: Canned datasets. The facts are invented, so an answer stating them came from the tool result.
DATASETS: dict[str, list[dict[str, Any]]] = {
    "apify~rag-web-browser": [
        {
            "searchResult": {
                "title": "Kettle valley council adopts quiet-hours bylaw",
                "url": "https://news.example.org/quiet-hours",
                "description": "Published 14 October 2026.",
            },
            "metadata": {"title": "Kettle valley council adopts quiet-hours bylaw"},
            "markdown": (
                "# Kettle valley council adopts quiet-hours bylaw\n\nOn 14 October 2026 the "
                "council voted 7 to 2 for quiet hours from 22:00 to 06:30, starting on "
                "3 March 2027. Fines for a first breach are 80 crowns."
            ),
        },
        {
            "searchResult": {
                "title": "Ferry timetable changes",
                "url": "https://news.example.org/ferry",
                "description": "Winter timetable.",
            },
            "metadata": {"title": "Ferry timetable changes"},
            "markdown": "# Ferry timetable\n\nThe winter ferry leaves at 07:15 and 18:40.",
        },
    ],
    "compass~crawler-google-places": [
        {
            "title": "Kiraku Ramen Alex",
            "categoryName": "Ramen restaurant",
            "address": "Rathausstraße 7, 10178 Berlin",
            "totalScore": 4.8,
        },
        {
            "title": "Nudelhaus Mitte",
            "categoryName": "Ramen restaurant",
            "address": "Karl-Liebknecht-Straße 13, 10178 Berlin",
            "totalScore": 4.3,
        },
    ],
    "apify~website-content-crawler": [
        {
            "url": "https://docs.example.org/setup/",
            "metadata": {"title": "Setup"},
            "markdown": "# Setup\n\nPlug the purple cable into port seven.",
        }
    ],
}

#: Store items in the public Store's shape (invented actors, real field names).
STORE_ITEMS: list[dict[str, Any]] = [
    {
        "id": "act0001",
        "title": "Web browser",
        "name": "rag-web-browser",
        "username": "apify",
        "userFullName": "Apify",
        "description": "Search the web and read the top pages as Markdown.",
        "categories": ["AI", "OPEN_SOURCE"],
        "url": "https://apify.com/apify/rag-web-browser",
        "stats": {
            "totalUsers30Days": 32000,
            "publicActorRunStats30Days": {"SUCCEEDED": 990, "FAILED": 6, "TOTAL": 1000},
            "actorReviewRating": 4.7,
            "actorReviewCount": 35,
        },
        "currentPricingInfo": {
            "pricingModel": "PAY_PER_EVENT",
            "pricingPerEvent": {
                "actorChargeEvents": {
                    "apify-actor-start": {
                        "eventTitle": "Actor start",
                        "isOneTimeEvent": True,
                        "eventPriceUsd": 0.0002,
                    },
                    "fetch": {
                        "eventTitle": "Fetched page",
                        "isPrimaryEvent": True,
                        "eventTieredPricingUsd": {
                            "FREE": {"tieredEventPriceUsd": 0.00228},
                            "GOLD": {"tieredEventPriceUsd": 0.00133},
                        },
                    },
                }
            },
        },
    },
    {
        "id": "act0002",
        "title": "Places",
        "name": "crawler-google-places",
        "username": "compass",
        "userFullName": "Compass",
        "description": "Places, ratings and opening hours from the map.",
        "categories": ["LEAD_GENERATION"],
        "url": "https://apify.com/compass/crawler-google-places",
        "stats": {
            "totalUsers30Days": 14000,
            "publicActorRunStats30Days": {"SUCCEEDED": 11, "TIMED-OUT": 36, "TOTAL": 47},
        },
        "currentPricingInfo": {"pricingModel": "FREE"},
    },
]


def check_input(actor_path: str, run_input: Any) -> str | None:
    """Apify-style input validation: why the input is refused, or ``None``."""
    if not isinstance(run_input, dict):
        return "Input must be a JSON object"
    schema = SCHEMAS.get(actor_path, {})
    props = schema.get("properties") or {}
    for name in schema.get("required") or []:
        if name not in run_input:
            return f"Field input.{name} is required"
    for name, value in run_input.items():
        prop = props.get(name)
        if prop is None:
            continue
        if prop.get("editor") == "requestListSources":
            ok = isinstance(value, list) and all(isinstance(v, dict) and "url" in v for v in value)
            if not ok:
                return f"Field input.{name} must be an array of objects with a url"
        py = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict}.get(
            prop.get("type")
        )
        if py and not isinstance(value, py):
            return f"Field input.{name} must be {prop.get('type')}"
    return None


def _matching(actor_path: str, run_input: dict[str, Any]) -> list[dict[str, Any]]:
    """The canned items relevant to the input, so a ramen search doesn't return the ferry."""
    items = DATASETS.get(actor_path, [])
    words = set(re.findall(r"[a-zà-ÿ]{4,}", json.dumps(run_input).lower()))
    scored = [
        (len(words & set(re.findall(r"[a-zà-ÿ]{4,}", json.dumps(i).lower()))), i) for i in items
    ]
    best = [i for score, i in sorted(scored, key=lambda s: -s[0]) if score > 0]
    return best or items[:1]


def _project(items: list[dict[str, Any]], query: dict[str, str]) -> list[dict[str, Any]]:
    limit = int(query.get("limit") or 1000)
    fields = [f for f in (query.get("fields") or "").split(",") if f]
    out = items[:limit]
    return [{k: i[k] for k in fields if k in i} for i in out] if fields else out


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    def _send(self, status: int, body: Any) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status: int, kind: str, message: str) -> None:
        self._send(status, {"error": {"type": kind, "message": message}})

    def _authorized(self) -> bool:
        if self.headers.get("Authorization", "").startswith("Bearer "):
            return True
        self._error(401, "token-not-provided", "Authentication token was not provided")
        return False

    def do_POST(self) -> None:
        url = urlparse(self.path)
        m = re.fullmatch(r"/v2/acts/([^/]+)/(run-sync-get-dataset-items|runs)", url.path)
        if not m:
            return self._error(404, "page-not-found", url.path)
        if not self._authorized():
            return None
        actor, mode = unquote(m.group(1)), m.group(2)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            run_input = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._error(400, "invalid-input", "Input is not valid JSON")
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        self.server.calls.append(
            {
                "actor": actor,
                "mode": mode,
                "input": run_input,
                "query": query,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if self.server.sync_status is not None and mode == "run-sync-get-dataset-items":
            status, kind = self.server.sync_status
            return self._error(status, kind, "scripted failure")
        problem = check_input(actor, run_input)
        if problem:
            return self._error(400, "invalid-input", problem)
        items = _matching(actor, run_input)
        if mode == "run-sync-get-dataset-items":
            return self._send(201, _project(items, query))
        run_id = f"run{len(self.server.calls)}"
        self.server.runs[run_id] = {"items": items, "polls": 0}
        return self._send(
            201, {"data": {"id": run_id, "status": "RUNNING", "defaultDatasetId": f"ds-{run_id}"}}
        )

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        # The public browse API — no token.
        if url.path == "/v2/store":
            self.server.store_hits += 1
            search = (query.get("search") or "").lower()
            items = [i for i in STORE_ITEMS if not search or search in json.dumps(i).lower()]
            offset, limit = int(query.get("offset") or 0), int(query.get("limit") or 24)
            page = items[offset : offset + limit]
            return self._send(
                200,
                {
                    "data": {
                        "total": len(items),
                        "count": len(page),
                        "offset": offset,
                        "limit": limit,
                        "items": page,
                    }
                },
            )
        m = re.fullmatch(r"/v2/acts/([^/]+)(/builds/default)?", url.path)
        if m:
            actor = unquote(m.group(1))
            item = next((i for i in STORE_ITEMS if f"{i['username']}~{i['name']}" == actor), None)
            if item is None or actor not in SCHEMAS:
                return self._error(404, "record-not-found", f"Actor {actor} was not found")
            if m.group(2):
                return self._send(
                    200,
                    {
                        "data": {
                            "inputSchema": json.dumps(SCHEMAS[actor]),
                            "actorDefinition": {"input": SCHEMAS[actor]},
                        }
                    },
                )
            detail = {k: v for k, v in item.items() if k not in ("currentPricingInfo", "url")}
            detail["pricingInfos"] = [
                {**item["currentPricingInfo"], "startedAt": "2026-01-01T00:00:00.000Z"},
                {"pricingModel": "FREE", "startedAt": "2099-01-01T00:00:00.000Z"},
            ]
            detail["readmeSummary"] = f"## {item['title']}\n\n{item['description']}"
            detail["defaultRunOptions"] = {"timeoutSecs": 900, "memoryMbytes": 1024}
            return self._send(200, {"data": detail})
        # The run API — token required.
        if not self._authorized():
            return None
        m = re.fullmatch(r"/v2/actor-runs/([^/]+)", url.path)
        if m and m.group(1) in self.server.runs:
            run_id = m.group(1)
            run = self.server.runs[run_id]
            run["polls"] += 1
            done = run["polls"] > self.server.running_polls
            return self._send(
                200,
                {
                    "data": {
                        "id": run_id,
                        "status": "SUCCEEDED" if done else "RUNNING",
                        "defaultDatasetId": f"ds-{run_id}",
                        "usageTotalUsd": 0.0123 if done else 0.004,
                    }
                },
            )
        m = re.fullmatch(r"/v2/datasets/ds-([^/]+)/items", url.path)
        if m and m.group(1) in self.server.runs:
            return self._send(200, _project(self.server.runs[m.group(1)]["items"], query))
        return self._error(404, "record-not-found", url.path)


class _Server(ThreadingHTTPServer):
    calls: list[dict[str, Any]]
    runs: dict[str, dict[str, Any]]
    running_polls: int
    sync_status: tuple[int, str] | None
    store_hits: int


class FakeApify:
    """``running_polls`` makes each started run report ``RUNNING`` for that many polls before it
    succeeds; ``sync_status`` makes every synchronous run answer that ``(status, error type)``."""

    def __init__(
        self, *, running_polls: int = 0, sync_status: tuple[int, str] | None = None
    ) -> None:
        self.server = _Server(("127.0.0.1", 0), _Handler)
        self.server.calls = []
        self.server.runs = {}
        self.server.running_polls = running_polls
        self.server.sync_status = sync_status
        self.server.store_hits = 0
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v2"

    @property
    def calls(self) -> list[dict[str, Any]]:
        return self.server.calls

    @property
    def store_hits(self) -> int:
        return self.server.store_hits

    def __enter__(self) -> FakeApify:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self._thread.join(timeout=5)
