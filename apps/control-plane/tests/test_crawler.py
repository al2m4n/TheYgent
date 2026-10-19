"""The ``crawler`` node: an actor's schema → node config, both wirings in both runtimes, the
spend ceiling, the result budget, and the Crawlers browser's catalog endpoints.

Runs go to ``_fake_apify`` — a real local HTTP server speaking the Apify run API's shapes (there is
no Apify account in the suite). The model is the threaded fake inference plane, scripted to call
the crawler by its node id and answer once the tool result is in the transcript.
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

import pytest
from _fake_apify import SCHEMAS, FakeApify
from _fake_inference import FakeInference
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_control_plane.crawlers import CrawlerCatalog
from theygent_control_plane.crawlers.catalog import card, headline_price
from theygent_control_plane.crawlers.schema import (
    budget,
    build_run_input,
    convert,
    form_fields,
    model_parameters,
)

_RAG = SCHEMAS["apify~rag-web-browser"]
_PLACES = SCHEMAS["compass~crawler-google-places"]
_SITE = SCHEMAS["apify~website-content-crawler"]


# ── schema → node config (pure) ───────────────────────────────────────────────


def test_default_expose_keeps_sizing_knobs_and_defaults_fixed() -> None:
    # Required fields first; optional fields the actor can't do without; never a result count or
    # a field that already has a default — a model offered those sets the spend.
    assert convert(_RAG).expose == ["query"]
    assert convert(_PLACES).expose == ["searchStringsArray", "locationQuery"]
    assert convert(_SITE).expose == ["startUrls"]


def test_list_fields_are_offered_as_one_value_by_default() -> None:
    conv = convert(_PLACES)
    assert conv.scalar == ["searchStringsArray"]
    assert conv.parameter_schema["properties"]["searchStringsArray"]["type"] == "string"
    # Opting out keeps the list shape.
    listy = convert(_PLACES, scalar=[])
    assert listy.parameter_schema["properties"]["searchStringsArray"]["type"] == "array"


def test_fixed_editors_and_secrets_are_never_exposed() -> None:
    conv = convert(_RAG, expose=["query", "requestTimeoutSecs", "proxyConfiguration"])
    assert conv.expose == ["query"]


def test_required_field_nobody_can_fill_is_reported() -> None:
    schema = {
        "properties": {
            "cookie": {"type": "array", "editor": "json"},
            "q": {"type": "string"},
        },
        "required": ["cookie", "q"],
    }
    conv = convert(schema)
    assert conv.unfillable == ["cookie"]
    # A required field with a prefill is fixed to it instead.
    assert convert(_RAG, expose=[]).input == {"query": "local-first agents"}


def test_model_values_are_reshaped_for_the_actor() -> None:
    places = convert(_PLACES)
    run = build_run_input(
        {"maxCrawledPlacesPerSearch": 5},
        {"searchStringsArray": "ramen", "locationQuery": "Berlin", "maxReviews": 50},
        parameter_schema=places.parameter_schema,
        scalar=places.scalar,
    )
    # The scalar is wrapped; the invented paid add-on is dropped; fixed input stays.
    assert run == {
        "maxCrawledPlacesPerSearch": 5,
        "searchStringsArray": ["ramen"],
        "locationQuery": "Berlin",
    }
    site = convert(_SITE)
    run = build_run_input(
        {},
        {"startUrls": '["https://a.example/", "https://b.example/"]'},  # a JSON list in a string
        parameter_schema=site.parameter_schema,
        scalar=site.scalar,
    )
    assert run == {"startUrls": [{"url": "https://a.example/"}, {"url": "https://b.example/"}]}


def test_the_coercion_marker_never_reaches_the_model() -> None:
    schema = convert(_SITE).parameter_schema
    assert "x-theygent-coerce" in json.dumps(schema)
    assert "x-theygent-coerce" not in json.dumps(model_parameters(schema))


def test_budget_shares_the_characters_across_items() -> None:
    items = [{"markdown": "a" * 5000, "url": f"https://x/{i}"} for i in range(4)]
    text = budget(items, max_chars=2000, fields=["markdown"])
    assert len(text) <= 2000 + 4  # four rows plus newlines
    assert text.count("…") == 4 and "url" not in text
    assert budget([], max_chars=100) == "[no items]"
    many = budget([{"n": "x" * 300}] * 50, max_chars=1000)
    assert many.endswith("more item(s) not shown]")


def test_form_fields_describe_every_field_for_the_fixed_input_form() -> None:
    fields = {f["name"]: f for f in form_fields(_RAG)}
    assert fields["query"]["required"] and fields["query"]["exposable"]
    assert fields["outputFormats"]["enum"] == ["text", "markdown", "html"]
    assert not fields["proxyConfiguration"]["exposable"]
    assert fields["query"]["description"] == "Search keywords, or the URL of one page to read."


def test_cards_show_pricing_and_the_30_day_success_rate() -> None:
    from _fake_apify import STORE_ITEMS

    web = card("apify", STORE_ITEMS[0])
    assert web["actor"] == "apify/rag-web-browser"
    assert web["successRate30d"] == 0.99
    assert web["pricing"] == {"model": "PAY_PER_EVENT", "priceUsd": 0.00228, "unit": "Fetched page"}
    places = card("apify", STORE_ITEMS[1])
    assert places["successRate30d"] == round(11 / 47, 4)  # an unreliable actor reads as such
    assert headline_price({"pricingModel": "FREE"})["priceUsd"] is None


# ── graphs ────────────────────────────────────────────────────────────────────


def _crawler_node(connection: str, **over: Any) -> dict[str, Any]:
    conv = convert(_RAG)
    cfg: dict[str, Any] = {
        "provider": "apify",
        "actor": "apify/rag-web-browser",
        "connection": connection,
        "input": {"maxResults": 2},
        "expose": conv.expose,
        "scalar": conv.scalar,
        "parameterSchema": conv.parameter_schema,
        "description": "Search the web and read the top pages.",
        "limits": {"itemLimit": 2, "resultChars": 3000, "fields": ["markdown"]},
    }
    cfg.update(over)
    return {
        "id": "web_search",
        "type": "crawler",
        "kind": "activity",
        "config": cfg,
        "ports": {
            "in": [{"id": "in", "type": "any", "required": False}],
            "out": [
                {"id": "out", "type": "any"},
                {"id": "err", "type": "error"},
                {"id": "use", "type": "any", "role": "tool"},
            ],
        },
    }


def _boundary(node_id: str, kind: str) -> dict[str, Any]:
    if kind == "input":
        return {
            "id": node_id,
            "type": "input",
            "kind": "boundary",
            "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
        }
    return {
        "id": node_id,
        "type": "output",
        "kind": "boundary",
        "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
    }


def _edge(eid: str, src: str, sh: str, tgt: str, th: str, channel: str = "data") -> dict:
    return {
        "id": eid,
        "source": src,
        "sourceHandle": sh,
        "target": tgt,
        "targetHandle": th,
        "channel": channel,
    }


def step_graph(connection: str, *, wire_err: bool = False, **over: Any) -> dict[str, Any]:
    """input → crawler (query = the run input) → output; with ``wire_err`` the err port feeds a
    second output so a failure is the run's output."""
    node = _crawler_node(
        connection, input={"query": "$in", "maxResults": 2}, expose=[], scalar=[], **over
    )
    node["config"]["parameterSchema"] = None
    nodes = [_boundary("n_in", "input"), node, _boundary("n_out", "output")]
    edges = [
        _edge("e1", "n_in", "out", "web_search", "in"),
        _edge("e2", "web_search", "out", "n_out", "in"),
    ]
    if wire_err:
        nodes.append(_boundary("n_err", "output"))
        edges.append(_edge("e3", "web_search", "err", "n_err", "in"))
    return {
        "schemaVersion": "1.0",
        "id": "agt_crawl_step",
        "name": "crawl-step",
        "version": "0.1.0",
        "models": {},
        "tools": {},
        "nodes": nodes,
        "edges": edges,
    }


def capability_graph(connection: str, **over: Any) -> dict[str, Any]:
    """input → llm (with the crawler wired into its tools port) → output."""
    return {
        "schemaVersion": "1.0",
        "id": "agt_crawl_cap",
        "name": "crawl-capability",
        "version": "0.1.0",
        "models": {"default": {"binding": "mlx", "model": "triage-fast", "params": {}}},
        "tools": {},
        "nodes": [
            _boundary("n_in", "input"),
            {
                "id": "n_llm",
                "type": "llm",
                "kind": "activity",
                "config": {
                    "model": "default",
                    "messages": [{"role": "user", "content": "$in"}],
                    "maxToolIterations": 3,
                },
                "ports": {
                    "in": [
                        {"id": "in", "type": "any"},
                        {"id": "tools", "type": "any", "required": False, "role": "tool"},
                    ],
                    "out": [{"id": "out", "type": "any"}],
                },
            },
            _crawler_node(connection, **over),
            _boundary("n_out", "output"),
        ],
        "edges": [
            _edge("e1", "n_in", "out", "n_llm", "in"),
            _edge("e2", "n_llm", "out", "n_out", "in"),
            _edge("e_cap", "web_search", "use", "n_llm", "tools", channel="tool"),
        ],
    }


def _apify_connection(client: TestClient, base: str, *, token: str | None = "tok-1") -> str:
    body: dict[str, Any] = {
        "name": "apify",
        "kind": "http_auth",
        "config": {"baseUrl": base, "auth": {"type": "bearer"}, "provider": "apify"},
    }
    if token:
        body["secret"] = token
    resp = client.post("/connections", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _run(client: TestClient, ir: dict[str, Any], input_value: Any) -> dict[str, Any]:
    resp = client.post("/graphs/runs", json={"ir": ir, "input": input_value, "stream": False})
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── step mode ────────────────────────────────────────────────────────────────


def test_step_crawler_returns_items_and_caps_spend(client: TestClient) -> None:
    with FakeApify() as fake:
        cid = _apify_connection(client, fake.base)
        body = _run(client, step_graph(cid), "quiet hours bylaw")
        assert body["status"] == "completed", body
        items = json.loads(body["output"])
        assert items == [{"markdown": items[0]["markdown"]}]  # projected to `fields`
        assert "80 crowns" in items[0]["markdown"]
        call = fake.calls[-1]
        assert call["mode"] == "run-sync-get-dataset-items"
        assert call["input"] == {"query": "quiet hours bylaw", "maxResults": 2}
        assert call["authorization"] == "Bearer tok-1"  # the token, from the connection
        # The node's cap (0.25 default) held under the platform ceiling (1.0 default).
        assert float(call["query"]["maxTotalChargeUsd"]) == 0.25
        assert call["query"]["limit"] == "2" and call["query"]["fields"] == "markdown"
        assert int(call["query"]["timeout"]) == 120

        spans = client.get(f"/runs/{body['runId']}/trace").json()["spans"]
        node = next(s for s in spans if s["name"] == "web_search")
        assert node["attributes"]["crawler.actor"] == "apify/rag-web-browser"
        assert node["attributes"]["crawler.items"] == 1
        # A synchronous run names no run, so no cost is claimed for it.
        assert "crawler.usage_total_usd" not in node["attributes"]


def test_the_platform_ceiling_caps_every_node(client: TestClient) -> None:
    resp = client.patch("/settings", json={"crawler.max_charge_usd": 0.1})
    assert resp.status_code == 200, resp.text
    with FakeApify() as fake:
        cid = _apify_connection(client, fake.base)
        # A node asking for more than the ceiling gets the ceiling; one asking less keeps its own.
        _run(client, step_graph(cid, limits={"maxChargeUsd": 5}), "ferry")
        assert float(fake.calls[-1]["query"]["maxTotalChargeUsd"]) == 0.1
        _run(client, step_graph(cid, limits={"maxChargeUsd": 0.02}), "ferry")
        assert float(fake.calls[-1]["query"]["maxTotalChargeUsd"]) == 0.02


def test_interactive_runs_stop_at_the_synchronous_limit(client: TestClient) -> None:
    with FakeApify() as fake:
        cid = _apify_connection(client, fake.base)
        _run(client, step_graph(cid, limits={"timeoutSeconds": 3600}), "ferry")
        assert int(fake.calls[-1]["query"]["timeout"]) == 300


def test_missing_token_binds_err_not_a_failed_run(client: TestClient) -> None:
    with FakeApify() as fake:
        cid = _apify_connection(client, fake.base, token=None)
        body = _run(client, step_graph(cid, wire_err=True), "ferry")
        assert body["status"] == "completed", body
        assert "has no API token" in body["output"]
        assert fake.calls == []  # nothing reached the provider


def test_a_run_past_the_sync_limit_says_to_run_durably(client: TestClient) -> None:
    with FakeApify(sync_status=(408, "run-timeout-exceeded")) as fake:
        cid = _apify_connection(client, fake.base)
        body = _run(client, step_graph(cid, wire_err=True), "ferry")
        assert "300-second synchronous limit" in body["output"]
        assert "durably" in body["output"]


def test_a_refused_input_is_the_providers_message(client: TestClient) -> None:
    with FakeApify() as fake:
        cid = _apify_connection(client, fake.base)
        ir = step_graph(cid, wire_err=True, actor="apify/website-content-crawler")
        ir["nodes"][1]["config"]["input"] = {"maxCrawlPages": 2}  # no startUrls
        body = _run(client, ir, "ferry")
        assert "invalid-input" in body["output"] and "startUrls" in body["output"]


def test_an_unknown_connection_binds_err(client: TestClient) -> None:
    body = _run(client, step_graph("con_missing", wire_err=True), "ferry")
    assert "not found or disabled" in body["output"]


# ── capability mode ──────────────────────────────────────────────────────────


def test_the_model_calls_the_crawler_and_reads_a_budgeted_result(pg_url: str) -> None:
    with (
        FakeApify() as fake,
        FakeInference(
            mode="tool_call",
            tool_name="web_search",
            tool_args={"query": "quiet hours bylaw", "maxResults": 40},
            response="Quiet hours start on 3 March 2027.",
        ) as inference,
    ):
        app = create_app(inference_base_url=inference.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            cid = _apify_connection(client, fake.base)
            body = _run(client, capability_graph(cid), "when do quiet hours start?")
            assert body["status"] == "completed", body
            assert body["output"] == "Quiet hours start on 3 March 2027."
            # The model was offered only the exposed field, with the node's description.
            tool = inference.captured["tools"][0]["function"]
            assert tool["name"] == "web_search"
            assert tool["description"] == "Search the web and read the top pages."
            assert list(tool["parameters"]["properties"]) == ["query"]
            # The invented maxResults never reached the actor: the node's fixed value did.
            assert fake.calls[-1]["input"] == {"maxResults": 2, "query": "quiet hours bylaw"}
            # The tool result is the budgeted text of the chosen fields.
            tool_msg = next(m for m in inference.captured["messages"] if m["role"] == "tool")
            assert "80 crowns" in tool_msg["content"] and "searchResult" not in tool_msg["content"]
            spans = client.get(f"/runs/{body['runId']}/trace").json()["spans"]
            call = next(s for s in spans if (s["phase"] or "").startswith("tool.web_search"))
            assert call["attributes"]["crawler.actor"] == "apify/rag-web-browser"
            assert call["attributes"]["crawler.max_charge_usd"] == 0.25


# ── the Crawlers browser's catalog endpoints ─────────────────────────────────


def _catalog_app(pg_url: str, fake: FakeApify, inference: FakeInference) -> Any:
    return create_app(
        inference_base_url=inference.v1_url,
        database_url=pg_url,
        crawler_catalog=CrawlerCatalog(base=fake.base),
    )


def test_browse_proxies_and_caches_the_store(pg_url: str, fake_inference: FakeInference) -> None:
    with FakeApify() as fake, TestClient(_catalog_app(pg_url, fake, fake_inference)) as client:
        page = client.get("/crawlers/catalog?search=web").json()
        assert page["total"] == 1
        entry = page["entries"][0]
        assert entry["actor"] == "apify/rag-web-browser"
        assert entry["successRate30d"] == 0.99
        assert entry["pricing"]["unit"] == "Fetched page"
        assert entry["users30d"] == 32000
        client.get("/crawlers/catalog?search=web")
        assert fake.store_hits == 1  # the second browse was served from the cache


def test_actor_detail_lists_fields_and_suggests_a_node(
    pg_url: str, fake_inference: FakeInference
) -> None:
    with FakeApify() as fake, TestClient(_catalog_app(pg_url, fake, fake_inference)) as client:
        resp = client.get("/crawlers/catalog/actor?actor=apify/rag-web-browser")
        assert resp.status_code == 200, resp.text
        detail = resp.json()
        assert [f["name"] for f in detail["fields"]][:2] == ["query", "maxResults"]
        assert detail["readmeSummary"].startswith("## Web browser")
        # The pricing in force, not a change scheduled for later.
        assert detail["entry"]["pricing"]["model"] == "PAY_PER_EVENT"
        suggested = detail["suggested"]
        assert suggested["suggestedId"] == "rag_web_browser"
        config = suggested["config"]
        assert config["expose"] == ["query"] and config["connection"] == ""
        assert config["limits"]["maxChargeUsd"] == 0.25


def test_convert_regenerates_the_schema_for_the_ticked_fields(
    pg_url: str, fake_inference: FakeInference
) -> None:
    with FakeApify() as fake, TestClient(_catalog_app(pg_url, fake, fake_inference)) as client:
        resp = client.post(
            "/crawlers/catalog/convert",
            json={
                "actor": "compass/crawler-google-places",
                "expose": ["searchStringsArray", "maxCrawledPlacesPerSearch"],
                "input": {"maxReviews": 0},
                "connection": "con_x",
            },
        )
        assert resp.status_code == 200, resp.text
        config = resp.json()["config"]
        assert config["expose"] == ["searchStringsArray", "maxCrawledPlacesPerSearch"]
        assert config["scalar"] == ["searchStringsArray"]
        assert config["input"] == {"maxReviews": 0}
        assert set(config["parameterSchema"]["properties"]) == set(config["expose"])


@pytest.mark.parametrize(
    ("url", "status", "code"),
    [
        ("/crawlers/catalog/actor?actor=../../users/me", 400, "invalid_actor"),
        ("/crawlers/catalog?provider=other", 400, "crawler_provider_unknown"),
        ("/crawlers/catalog/actor?actor=nobody/nothing", 404, "crawler_not_found"),
    ],
)
def test_catalog_errors_are_named(
    pg_url: str, fake_inference: FakeInference, url: str, status: int, code: str
) -> None:
    with FakeApify() as fake, TestClient(_catalog_app(pg_url, fake, fake_inference)) as client:
        resp = client.get(url)
        assert resp.status_code == status, resp.text
        assert resp.json()["error"]["code"] == code


# ── durable-runtime parity ───────────────────────────────────────────────────


async def _durable(pg_url: str, inference_url: str, ir: dict[str, Any], input_value: Any) -> Any:
    from _durable import reset_dbos_schema, save_agent
    from theygent_control_plane import db
    from theygent_control_plane.crawlers import CrawlerBackend
    from theygent_control_plane.durable.runtime import DurableRuntime
    from theygent_control_plane.mcp import McpManager
    from theygent_control_plane.secrets import SecretStore
    from theygent_control_plane.store import AgentStore, ConnectionStore, RunStore, TriggerStore
    from theygent_control_plane.tool_resolve import DbConnectionResolver
    from theygent_gateway_client import GatewayClient

    await reset_dbos_schema(pg_url)
    engine = db.create_engine(pg_url)
    sm = db.create_sessionmaker(engine)
    agents = AgentStore()
    aid, ver = await save_agent(sm, agents, ir)
    gw = GatewayClient(inference_url, max_retries=0)
    resolver = DbConnectionResolver(sm, ConnectionStore(), SecretStore.from_keys(_KEYS))
    rt = DurableRuntime(
        database_url=pg_url,
        gateway=gw,
        mcp=McpManager(),
        store=RunStore(),
        agents=agents,
        triggers=TriggerStore(),
        sessionmaker=sm,
        fast_polling=True,
        tool_auth=resolver,
        crawlers=CrawlerBackend(resolver, max_charge_usd=0.5),
    )
    rt.launch()
    try:
        handle = await rt.enqueue_run(
            {"agent_id": aid, "version": ver, "content_hash": None}, input_value
        )
        return await handle.get_result()
    finally:
        rt.shutdown()
        with contextlib.suppress(Exception):
            from dbos import DBOS

            DBOS.destroy(destroy_registry=False)
        await gw.aclose()
        await engine.dispose()


#: One fixed secret key, so the API (which creates the connection) and the durable runtime
#: (which resolves it) decrypt the same token.
_KEYS = [Fernet.generate_key().decode()]


async def test_durable_step_crawler_starts_polls_and_reads(pg_url: str) -> None:
    # The durable lowering splits the call into journaled steps — start, poll windows until the
    # run is terminal, read the dataset — and returns what the interactive walker returns.
    with FakeApify(running_polls=1) as fake, FakeInference() as inference:
        app = create_app(inference_base_url=inference.v1_url, database_url=pg_url, secret_key=_KEYS)
        with TestClient(app) as client:
            cid = _apify_connection(client, fake.base)
            ir = step_graph(cid)
            interactive = _run(client, ir, "quiet hours bylaw")
        ir["id"] = "agt_crawl_durable"
        durable = await _durable(pg_url, inference.v1_url, ir, "quiet hours bylaw")
        assert durable["status"] == "completed", durable
        assert json.loads(durable["output"]) == json.loads(interactive["output"])
        start = next(c for c in fake.calls if c["mode"] == "runs")
        assert start["input"] == {"query": "quiet hours bylaw", "maxResults": 2}
        # The durable backend's ceiling (0.5) sits above the node's cap (0.25): the node's wins.
        assert float(start["query"]["maxTotalChargeUsd"]) == 0.25
        run = next(iter(fake.server.runs.values()))
        assert run["polls"] == 2  # RUNNING once, then SUCCEEDED

        with TestClient(app) as client:
            spans = client.get(f"/runs/{durable['runId']}/trace").json()["spans"]
        node = next(s for s in spans if s["name"] == "web_search")
        assert node["attributes"]["crawler.usage_total_usd"] == 0.0123
        assert node["attributes"]["crawler.run_id"] == run_id_of(fake)


def run_id_of(fake: FakeApify) -> str:
    return next(iter(fake.server.runs))


async def test_durable_capability_crawler_matches_the_walker(pg_url: str) -> None:
    with (
        FakeApify() as fake,
        FakeInference(
            mode="tool_call",
            tool_name="web_search",
            tool_args={"query": "quiet hours bylaw"},
            response="From 22:00.",
        ) as inference,
    ):
        app = create_app(inference_base_url=inference.v1_url, database_url=pg_url, secret_key=_KEYS)
        with TestClient(app) as client:
            cid = _apify_connection(client, fake.base)
            ir = capability_graph(cid)
        ir["id"] = "agt_crawl_cap_durable"
        durable = await _durable(pg_url, inference.v1_url, ir, "quiet hours?")
        assert durable["status"] == "completed", durable
        assert durable["output"] == "From 22:00."
        assert [c["mode"] for c in fake.calls] == ["runs"]
        tool_msg = next(m for m in inference.captured["messages"] if m["role"] == "tool")
        assert "80 crowns" in tool_msg["content"]
