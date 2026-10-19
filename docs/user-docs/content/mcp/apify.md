# Apify through the MCP hub

[Apify](https://apify.com/store) runs a store of hosted scrapers, crawlers and automations, which it calls **Actors**: web search that returns pages as Markdown, Google Maps places, website crawls, social media profiles and thousands more. Apify publishes an MCP server, so you can give an agent Actors as tools today with the [MCP hub](index.md) alone.

This page covers that path. For agents on smaller local models, or whenever you want to fix what each call may cost, the [crawler node](../building/nodes/crawler.md) is the better fit — see [When to use a crawler node instead](#when-to-use-a-crawler-node-instead).

## What you need

- An **Apify account** and its **API token** (Apify Console → Settings → API & Integrations). Actor runs are billed to the account that owns the token. Apify's free plan includes a monthly usage allowance.
- A **model that calls tools reliably**. Actor tools carry every input field the Actor accepts, which is a lot to read: see [the costs](#what-it-costs-in-context-and-in-money).

## Install the server

The server is `com.apify/apify-mcp-server` in the Official MCP Registry. It is a remote server at `https://mcp.apify.com/`, reached over streamable HTTP.

1. Open the **MCP** page and press **Browse hubs**.
2. Pick **Official MCP Registry** and search for `apify`.
3. Expand `com.apify/apify-mcp-server` and pick its **Remote server (streamable-http)** form.
4. Authenticate in one of two ways:
    - **Token.** Fill the `Authorization` field with `Bearer ` followed by your API token, for example `Bearer apify_api_…`. The value is stored encrypted and never shown again.
    - **Sign in.** Check **Sign in with the provider instead**, install, then press **Connect** on the new row and approve access on Apify's page. See [Authorize with OAuth](index.md#authorize-with-oauth-sign-in).
5. Give the connection a name, such as `apify`, and press **Install**.

You can also add it by hand: **Add server** → **Remote**, URL `https://mcp.apify.com/`, auth **Bearer token** with your API token.

## Choose which Actors become tools

The server's URL takes a `tools` parameter: a comma-separated list of Actor names, tool categories, or single tool names. Each Actor you list becomes one tool.

```text
https://mcp.apify.com/?tools=apify/rag-web-browser,compass/crawler-google-places
```

To change the list, open the server's **Settings** on the MCP page and edit its **URL**, then **Close** the server so it reconnects with the new list.

Without `tools`, the server serves its defaults. They include the generic `search-actors`, `fetch-actor-details` and `call-actor` tools, documentation search, and a couple of web-browsing Actors. Apify notes that the defaults may change, so list your tools explicitly for agents you depend on. `call-actor` lets the model run *any* Actor it finds — leave it out unless that is what you want.

!!! note "Browsing works without a token"
    With no token, the server answers only its discovery tools — `search-actors`, `fetch-actor-details` and the documentation tools — and only when `tools` lists nothing else. That is enough to explore the store from an agent. Running an Actor always needs a token.

## Wire Actors into an agent

Each Actor tool is an ordinary MCP tool:

1. Select an llm node, and in **Tools the model can call** press **Add MCP tools**.
2. Pick the Apify connection, tick the Actor tools, and press **Add**. Each lands as an `mcp_tool` node wired into the llm's `tools` port.
3. Give the llm a prompt that says when to search, and run it.

The model decides when to call an Actor and fills its arguments. The run's tool result is what Apify's server returns for the call. See the [mcp_tool node](../building/nodes/mcp.md) for ports, step mode and the error contract.

## What it costs, in context and in money

An Actor tool offers the model **every input field** the Actor declares — proxy settings, result counts, paid add-ons, tuning knobs. That has three consequences worth knowing before you rely on this path:

- **It takes a lot of context.** Three popular Actors offered this way add about 13,500 tokens to every model call. A model with a small context window has little room left for the conversation, and the first call on a local engine is slow until the engine has cached the tool definitions.
- **The model sets the spend.** Pay-per-event Actors charge per result and per add-on. A model offered every field can ask for 50 results, or switch on a paid add-on, where 5 results and none would have done. Apify bills the token's account for whatever the model asks for.
- **Results come back whole.** A crawl can return far more text than a local model's context holds, and the tool result is passed to the model as it is.

On Apple Silicon with MLX there is one more: some models write a list argument (a list of search terms or URLs) as a bare string, and the engine drops the request instead of answering. Actors with list inputs are the ones that trigger it.

## When to use a crawler node instead

A [crawler node](../building/nodes/crawler.md) runs the same Actors without the MCP server, and moves each of those decisions to whoever builds the agent:

| | MCP hub | Crawler node |
|---|---|---|
| What the model sees | every input field | only the fields you expose, usually one or two |
| Result counts, add-ons, proxies | the model chooses | fixed on the node |
| Spend per call | whatever the model asks for | capped by the node and by a platform-wide ceiling |
| Result size | whole | trimmed to a character budget you set |
| List inputs on MLX | can drop the request | offered as a single value and wrapped for the Actor |
| Long crawls | limited by the server's call timeout | any length on a [durable run](../running/durable.md) |

Use the MCP hub when a capable model should explore the store and pick Actors on its own. Use a crawler node when you know which Actor the agent needs.

## Privacy

The model's tool arguments — search terms, URLs, place names — go to Apify, and the Actor's results come back into your run. Apify's servers do the crawling. Your token stays encrypted in the control plane's secret store and is sent only to the URL you configured. See [Privacy and data flow](../concepts/privacy.md).

## Related pages

- [MCP servers](index.md) — the hub, connections and OAuth sign-in
- [The mcp_tool node](../building/nodes/mcp.md) — step and capability wiring
- [The crawler node](../building/nodes/crawler.md) — Actors as nodes with fixed inputs and spend caps
