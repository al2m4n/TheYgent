# AGENTS.md — control plane · `crawlers/`

The `crawler` node's provider seam: run a hosted crawler or scraper (an Apify actor today) as a
graph step or as a tool an `llm` node can call. It also proxies the provider's public store for the
interface's Crawlers browser. Rules in [apps/control-plane/AGENTS.md](../../../AGENTS.md) apply
first; the node's behaviour is in the "Crawler capabilities" section of
[graph-execution.md](../../../../../docs/dev-docs/graph-execution.md).

## Where it sits

- **Called by:** the walker (`execute_crawler`, and `crawler_*` helpers for the capability path)
  and the durable compiler (`_crawler_start_step` → `_crawler_poll_step` → `_crawler_items_step`);
  `/crawlers/catalog`, `/crawlers/catalog/actor` and `/crawlers/catalog/convert` in
  [app.py](../app.py).
- **Calls out to:** the Apify API. Browsing needs no token; running uses the user's token from an
  `http_auth` connection, resolved through [tool_resolve.py](../tool_resolve.py) inside the call.
- **Node config** (`CrawlerConfig`, `CrawlerLimits`) lives in
  [packages/ir](../../../../../packages/ir/AGENTS.md); the interface's `CrawlersDialog` and
  `CrawlerNodePanel` build it from `/crawlers/catalog/convert`.
- **Setting:** `crawler.max_charge_usd`, the platform spend ceiling (read live).

## Map

<!-- agents-map: covers=. -->

| File | What it does |
|------|--------------|
| [`apify.py`](apify.py) | `ApifyClient`: store search, actor detail and input schema; `run_sync` (one request, at most 300 s, cost unreported) for the interactive walker; `start` + `get_run` + `dataset_items` (short polls, cost reported) for the durable runtime. |
| [`schema.py`](schema.py) | Pure. An actor's input schema → the few *exposed* fields the model fills (the tool's `parameterSchema`) plus the *fixed* input the builder sets once; `build_run_input` coerces model values back into the actor's shapes; `budget` trims results to what a model should read. |
| [`runtime.py`](runtime.py) | `CrawlerBackend`, the injected backend both runtimes call: resolves the token per call, caps each run's charge at min(node cap, platform ceiling), dispatches by `provider` (an unknown one is a loud `CrawlerError`). |
| [`catalog.py`](catalog.py) | `CrawlerCatalog`: the cached, normalized store (category, pricing headline, 30-day users and success rate) and an actor's suggested node config. Never writes the database. |

## Invariants specific to this package

- **The token never reaches the IR, a span, or the journal.** It becomes a bearer header for one
  request.
- **No agent can raise its own spend.** The platform ceiling is applied server-side on every run.
- **The durable start step never retries** (`retries_allowed=False`): a retried start would launch
  and bill a second run.

## Tests

`tests/test_crawler.py`, against the real local stand-in in `tests/_fake_apify.py`.
