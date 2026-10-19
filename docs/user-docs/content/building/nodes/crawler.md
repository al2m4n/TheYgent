# Crawler

A **crawler** node runs a hosted scraper or crawler — an [Apify](https://apify.com/store) **Actor** — and returns the results it collects: web search results with the pages as Markdown, places from a map, a site's pages, product listings. It is an **activity** node.

Like a [tool node](tools.md) and a [rag node](rag.md), it has two lives: a **step** in the dataflow, or a **capability** the model calls on demand. The difference from giving a model an Actor through the [MCP hub](../../mcp/apify.md) is who decides what each call does. Here, the person building the agent chooses which fields the model fills, fixes everything else on the node, and caps what a call may cost and return.

## Adding a crawler

Crawlers come from the **Crawlers browser**:

- On an llm node, press **Add crawler** under **Tools the model can call**. The installed node is wired into that llm's `tools` port.
- Or drop a `crawler` node from the palette and press **Browse crawlers** in its inspector.

The browser searches the Apify Store. Each card shows what the store reports about the Actor:

| On the card | What it tells you |
|---|---|
| Success rate | The share of the Actor's public runs over the last 30 days that succeeded. An Actor that times out on most runs reads as such before you install it. |
| Pricing | The pricing model and its headline price — for pay-per-event Actors, the price of the main billed event (a fetched page, a scraped place) on the free plan. |
| Users | How many users ran it in the last 30 days. |
| Category, author | Where it sits in the store, and who publishes it. |

Open an Actor to see its input fields. The fields a model can usefully fill are listed with checkboxes; the suggested ones come pre-ticked — the required fields and the few the Actor can't do without. Result counts, paid add-ons and anything with a default stay unticked, because a model offered those sets the spend.

The first time, the browser asks for your **Apify API token** and stores it as a connection — encrypted, and never shown again. Later installs reuse that connection. Press **Add crawler** and the configured node lands on the canvas.

!!! note "The browser goes through TheYgent"
    Store searches go from your control plane to Apify's public Store API, which needs no token, and are cached for a few minutes. Your browser never calls Apify directly.

## Ports

| Port | Direction | Required | Description |
|---|---|---|---|
| `in` | in | no | Step mode: the value the fixed input's `$in` references read. Left unfed in capability mode. |
| `out` | out | — | Step mode: the dataset items on success. |
| `err` | out | — | The error message when the crawl fails. Its port type is `error`. |
| `use` | out | — | The violet capability handle. Wire it into an llm's `tools` port to let the model call the crawler. |

## Configuration

The inspector edits all of it; the Code view shows the stored config.

| Field | Type | Default | Description |
|---|---|---|---|
| `provider` | string | `apify` | The crawler service. `apify` is the only one today. |
| `actor` | string | *(required)* | The Actor's id, such as `apify/rag-web-browser`. **Change** in the inspector re-points the node at another Actor and keeps its id and wiring. |
| `connection` | string | *(required)* | The connection whose secret is your Apify API token. The node stores the connection's id, never the token. A connection without a token shows a **Set token** field in the inspector. |
| `input` | object | `{}` | The **fixed input**, sent on every call. The inspector renders it as a form generated from the Actor's own input schema — text, numbers, dropdowns, URL lists — with an **Advanced** section for proxy and other settings. An empty field uses the Actor's default. |
| `expose` | list | `[]` | The fields the model fills when the node is a capability. |
| `scalar` | list | `[]` | List fields the model fills as **one value per call**, wrapped into a one-item list for the Actor. On by default for exposed list fields — see [Local models](#local-models). |
| `parameterSchema` | object | — | The function schema the model sees, generated from the Actor's input schema. Ticking a field regenerates it; you never edit it by hand. |
| `description` | string | the Actor's description (from the browser) | The "use this when…" hint the model reads to decide when to call it. |
| `limits` | object | see below | What one call may cost, take and return. |

### Limits

| Limit | Default | Description |
|---|---|---|
| `timeoutSeconds` | `120` | How long the Actor run may take. Interactive runs stop at 300 seconds whatever this says — see [Long crawls](#long-crawls). |
| `maxChargeUsd` | `0.25` | The most one call may charge. Never above the platform ceiling, **Settings → Crawlers**. |
| `maxItems` | `10` | The most results a pay-per-result Actor may bill. |
| `itemLimit` | `10` (`5` from the browser) | How many dataset items are read back. |
| `resultChars` | `6000` | Capability mode: how many characters of results the model reads, shared equally across the items. |
| `fields` | all | Keep only these top-level item fields — for example `markdown` and `searchResult` for web search. |

## As a capability (the model crawls)

Wire the node's violet **`use`** handle into an llm's violet **`tools`** port (the browser does this for you when opened from an llm). The model sees a function named after the node's id, with only the exposed fields as arguments.

When it calls:

1. TheYgent builds the Actor input: the fixed input, plus the model's values for the exposed fields. A value for any other field is dropped, so a model can't raise a result count or switch on a paid add-on by inventing an argument.
2. The Actor runs under the node's limits.
3. The model gets the results back as text: only the chosen `fields`, each item given an equal share of `resultChars`, long items cut with a marker. A crawl can return megabytes; the model reads what fits.

!!! tip "Name the node meaningfully"
    The node **id is the function name** the model sees. The browser names it after the Actor (`rag_web_browser`); rename it to what it does for your agent — `web_search` is easier for a model to choose.

## As a step (a fixed crawl)

Wire the node inline with data edges: `input → crawler → llm → output`. The fixed input is the whole Actor input. A value that is exactly a [`$in` reference](../input-references.md) reads the node's `in` port — for example `{"query": "$in"}` searches for the run input, and `{"startUrls": "$in.in.urls"}` crawls a list of URLs from an object input. Any other string is sent as written, `$` included, so page functions and selectors are safe.

The dataset items — at most `itemLimit`, projected to `fields` — bind to `out` as a list and flow downstream.

## Spend

Apify bills the account that owns the token. Three things bound what a crawler node can spend:

- **The node's charge cap**, `limits.maxChargeUsd`, sent with every run. It is the cap that matters for pay-per-event Actors, which most popular Actors are.
- **The platform ceiling**, **Settings → Crawlers** (`crawler.max_charge_usd`, default $1.00). Every run's cap is held under it, so no agent can raise its own. Lowering it applies to the next call. An administrator can cap it from the environment with `THEYGENT_CRAWLER_MAX_CHARGE_USD`.
- **The fixed input**: result counts and add-ons the model can't touch.

The [run waterfall](../../running/observability.md) shows what each crawl returned and cost: a crawler node's row (or, in capability mode, the tool call's row inside the llm) shows the number of items and the charge Apify reported. Only durable runs report the charge — the synchronous call an interactive run uses names no run to ask about.

## Long crawls

An interactive run — the editor's **Test** panel, chat, `POST /graphs/runs` — waits for the crawl in one request, which Apify holds for at most **300 seconds**. TheYgent caps the Actor's own timeout there too, so a run never keeps going after the agent has given up. A crawl that needs longer fails with a message saying so.

A [durable run](../../running/durable.md) starts the crawl, checks on it about once a minute, and reads the results when it finishes, so a crawl can take as long as `timeoutSeconds` and survives a restart in between. A resumed run keeps polling the crawl it already started instead of starting (and paying for) another.

## Local models

Small local models are better at filling one or two simple fields than many. Two defaults help:

- **Few exposed fields.** Each exposed field adds to every prompt; an Actor offered with every field can cost thousands of tokens per call.
- **One value per call** (`scalar`). Local models write list arguments unreliably — and on MLX, a list written as a bare string can drop the request. A scalar field takes one search term or URL; TheYgent wraps it into the list the Actor wants, and unpacks a model's JSON-encoded list if it writes one anyway.

## Failure modes

The crawler follows the tool ok/err contract: a failure binds a clear message to `err` (or, in capability mode, goes back to the model as the tool result), and the run continues.

- The connection is missing, disabled, or **has no token** — set it on the node's connection.
- Apify **refuses the input** — the message is Apify's, naming the field.
- Apify **refuses the token** — check it on the connection.
- The crawl **exceeds 300 seconds** on an interactive run — run the agent durably, or lower the work per call.
- A durable crawl **ends without succeeding**, or is still running after `timeoutSeconds` — the message names Apify's run id so you can look it up in the Apify console.

## Privacy

A crawler node sends its input — the fixed input and, in capability mode, the model's arguments — to the provider it names, and the crawled results come back into your run. Your token stays in the control plane's encrypted secret store and is sent only to the connection's API address. See [Privacy and data flow](../../concepts/privacy.md).

## Related pages

- [Apify through the MCP hub](../../mcp/apify.md) — every Actor field offered to the model, no node config
- [LLM](llm.md) — the `tools` port and the tool-calling loop
- [Tools](tools.md) — connections and the error contract
- [Samples](../../samples/index.md) — the Web researcher sample agent
