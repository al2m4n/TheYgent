"""Retrieval (RAG): sources + ingestion + hybrid search, and the ``rag`` node in both wirings.

Everything runs against the real seams: a real Postgres (pgvector) via the conftest container,
the real threaded fake inference plane for ``/v1/embeddings`` (deterministic bag-of-words
vectors — similarity ordering is assertable), and — for the crawl path — a real local HTTP site
served from a thread, so the crawler walks genuine links over genuine HTTP.
"""

from __future__ import annotations

import http.server
import itertools
import json
import textwrap
import threading
import time
from typing import Any

import asyncpg
import pytest
from _db import plain_dsn
from _fake_inference import EMBED_DIM, FakeInference
from fastapi.testclient import TestClient
from theygent_control_plane.app import create_app
from theygent_control_plane.rag.chunking import chunk_markdown, embedding_text
from theygent_control_plane.walker import execute_rag

# ── a tiny markdown corpus with two clearly-separable topics ─────────────────

_DOC = textwrap.dedent(
    """\
    # Handbook

    ## Brewing coffee

    Grind the beans coarsely and pour water at ninety degrees. A slow pour
    makes the coffee sweeter and less bitter.

    ## Feeding llamas

    Llamas eat hay and fresh grass every morning. Never feed a llama
    chocolate or coffee grounds.
    """
)


# ── pure chunker behaviour ────────────────────────────────────────────────────


def test_chunker_tracks_heading_paths_and_merges_crumbs() -> None:
    chunks = chunk_markdown(_DOC)
    headings = {c.heading for c in chunks}
    assert "Handbook > Brewing coffee" in headings
    assert "Handbook > Feeding llamas" in headings
    # The embedded text is heading-prefixed (cheap context), the stored text is raw.
    coffee = next(c for c in chunks if c.heading == "Handbook > Brewing coffee")
    assert embedding_text(coffee).startswith("Handbook > Brewing coffee")
    assert not coffee.text.startswith("Handbook")


def test_chunker_ignores_headings_inside_code_fences() -> None:
    md = "# Real\n\ntext here\n\n```\n# not a heading\ncode\n```\n"
    chunks = chunk_markdown(md)
    assert all((c.heading or "").startswith("Real") for c in chunks)
    assert any("# not a heading" in c.text for c in chunks)


def test_chunker_splits_oversized_blocks_with_overlap() -> None:
    # Numbered sentences so the overlap assertion below cannot pass by repetition alone.
    md = "# Big\n\n" + " ".join(
        f"Sentence number {i} carries unique payload {i}." for i in range(400)
    )
    chunks = chunk_markdown(md)
    assert len(chunks) > 1
    assert all(len(c.text) // 4 <= 460 for c in chunks)  # within budget (+slack)
    # Blind splits carry a tail overlap: each chunk's opening sentence already appeared at the
    # end of its predecessor, so a fact straddling the cut survives in at least one piece.
    for prev, nxt in itertools.pairwise(chunks):
        first_sentence = nxt.text.split(".")[0]
        assert first_sentence and first_sentence in prev.text


async def test_execute_rag_without_backend_binds_err() -> None:
    out = await execute_rag(None, source="rag_x", query="q", top_k=5, min_similarity=None)
    assert out.ok is False
    assert "no retrieval backend" in str(out.value)


# ── API helpers ───────────────────────────────────────────────────────────────


def _create_source(client: TestClient, *, kind: str = "upload", **config: Any) -> dict[str, Any]:
    resp = client.post(
        "/rag/sources",
        json={
            "name": f"kb-{kind}",
            "kind": kind,
            "embedding_model": "embed-small",
            "config": config,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _upload(client: TestClient, source_id: str, filename: str, text: str) -> None:
    resp = client.post(
        f"/rag/sources/{source_id}/documents?filename={filename}",
        content=text.encode("utf-8"),
        headers={"content-type": "text/markdown"},
    )
    assert resp.status_code == 202, resp.text


def _wait_settled(client: TestClient, source_id: str, timeout: float = 20.0) -> dict[str, Any]:
    """Poll the source row until the background ingest settles — the same loop the UI runs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        source = client.get(f"/rag/sources/{source_id}").json()
        if source.get("status") not in ("ingesting", "empty"):
            return source
        time.sleep(0.05)
    raise AssertionError(f"source {source_id} did not settle: {source}")


# ── source CRUD + validation ─────────────────────────────────────────────────


def test_create_source_rejects_engine_name_and_bad_crawl_config(client: TestClient) -> None:
    resp = client.post(
        "/rag/sources",
        json={"name": "x", "kind": "upload", "embedding_model": "llamacpp", "config": {}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "engine_name_not_allowed"

    resp = client.post(
        "/rag/sources",
        json={"name": "x", "kind": "crawl", "embedding_model": "embed-small", "config": {}},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_rag_source"


def test_source_crud_roundtrip(client: TestClient) -> None:
    source = _create_source(client)
    sid = source["id"]
    assert sid.startswith("rag_")
    assert source["status"] == "empty"

    listed = client.get("/rag/sources").json()["sources"]
    assert any(s["id"] == sid for s in listed)

    patched = client.patch(f"/rag/sources/{sid}", json={"name": "renamed"})
    assert patched.json()["name"] == "renamed"

    assert client.delete(f"/rag/sources/{sid}").status_code == 204
    assert client.get(f"/rag/sources/{sid}").status_code == 404


def test_list_sources_keyset_pagination(client: TestClient) -> None:
    ids = [_create_source(client)["id"] for _ in range(3)]
    page1 = client.get("/rag/sources?limit=2").json()["sources"]
    assert [s["id"] for s in page1] == [ids[2], ids[1]]  # newest first
    page2 = client.get(f"/rag/sources?limit=2&before={page1[-1]['id']}").json()["sources"]
    assert [s["id"] for s in page2] == [ids[0]]


def test_upload_rejects_unsupported_extension(client: TestClient) -> None:
    source = _create_source(client)
    resp = client.post(
        f"/rag/sources/{source['id']}/documents?filename=weights.gguf",
        content=b"binary",
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "unsupported_document"


def test_ingest_endpoint_is_crawl_only(client: TestClient) -> None:
    source = _create_source(client)  # upload kind
    resp = client.post(f"/rag/sources/{source['id']}:ingest")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_rag_source"


def test_query_before_any_ingest_is_a_clean_400(client: TestClient) -> None:
    source = _create_source(client)
    resp = client.post(f"/rag/sources/{source['id']}/query", json={"query": "anything"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "rag_query_failed"
    assert "no embedded content" in resp.json()["error"]["message"]


# ── upload → ingest → hybrid query ───────────────────────────────────────────


def test_upload_ingest_and_hybrid_query(client: TestClient, fake_inference: FakeInference) -> None:
    source = _create_source(client)
    sid = source["id"]
    _upload(client, sid, "handbook.md", _DOC)
    settled = _wait_settled(client, sid)

    assert settled["status"] == "ready", settled
    assert settled["embedding_dim"] == EMBED_DIM  # discovered from the embedding response
    assert settled["documents"] == 1
    assert settled["chunks"] >= 2
    assert fake_inference.captured["embed_model"] == "embed-small"

    docs = client.get(f"/rag/sources/{sid}/documents").json()["documents"]
    assert docs[0]["uri"] == "handbook.md"
    assert docs[0]["status"] == "embedded"

    result = client.post(
        f"/rag/sources/{sid}/query", json={"query": "what do llamas eat every morning"}
    ).json()
    assert result["source_id"] == sid
    matches = result["matches"]
    assert matches, result
    # Hybrid ranking: the llama section must outrank the coffee section for a llama query.
    assert "llama" in matches[0]["text"].lower()
    assert matches[0]["uri"] == "handbook.md"
    assert matches[0]["heading"] == "Handbook > Feeding llamas"
    assert matches[0]["score"] > 0


def test_reupload_unchanged_document_skips_reembedding(
    client: TestClient, fake_inference: FakeInference
) -> None:
    source = _create_source(client)
    sid = source["id"]
    _upload(client, sid, "handbook.md", _DOC)
    first = _wait_settled(client, sid)
    calls_after_first = fake_inference.captured["embed_calls"]

    _upload(client, sid, "handbook.md", _DOC)  # identical content
    second = _wait_settled(client, sid)

    assert second["chunks"] == first["chunks"]  # replaced nothing, duplicated nothing
    assert fake_inference.captured["embed_calls"] == calls_after_first  # hash-skip economy
    assert (second["progress"] or {}).get("unchanged", 0) >= 1


def test_failed_reembed_keeps_the_last_good_chunks(
    client: TestClient, fake_inference: FakeInference
) -> None:
    # The replacement lands atomically AFTER embedding succeeds — a transient embedding outage
    # during a re-ingest must degrade to "stale content + an honest failed row", never delete
    # the served chunks.
    source = _create_source(client)
    sid = source["id"]
    _upload(client, sid, "handbook.md", _DOC)
    first = _wait_settled(client, sid)
    assert first["chunks"] >= 2

    fake_inference.captured["embed_fail"] = True  # the inference plane goes down
    _upload(client, sid, "handbook.md", _DOC + "\n\nA new paragraph the outage loses.")
    second = _wait_settled(client, sid)

    assert second["chunks"] == first["chunks"]  # the old content still serves
    docs = client.get(f"/rag/sources/{sid}/documents").json()["documents"]
    assert docs[0]["status"] == "failed"  # …and the failed attempt is honestly recorded
    assert docs[0]["chunks"] == first["chunks"]
    fake_inference.captured["embed_fail"] = False  # the outage ends
    result = client.post(f"/rag/sources/{sid}/query", json={"query": "what do llamas eat"})
    assert "llama" in result.json()["matches"][0]["text"].lower()

    # The failed row left "embedded", so the next ingest retries instead of hash-skipping,
    # and the new paragraph becomes retrievable.
    _upload(client, sid, "handbook.md", _DOC + "\n\nA new paragraph the outage loses.")
    _wait_settled(client, sid)
    docs = client.get(f"/rag/sources/{sid}/documents").json()["documents"]
    assert docs[0]["status"] == "embedded"
    result = client.post(f"/rag/sources/{sid}/query", json={"query": "paragraph the outage loses"})
    assert any("outage loses" in m["text"] for m in result.json()["matches"])


# ── the rag node: step mode ──────────────────────────────────────────────────


def _rag_step_graph(source_id: str, *, wire_err: bool = False, top_k: int = 3) -> dict[str, Any]:
    handle = "err" if wire_err else "out"
    return {
        "schemaVersion": "1.0",
        "id": "agt_rag_step",
        "name": "rag-step",
        "version": "0.1.0",
        "models": {},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_rag",
                "type": "rag",
                "kind": "activity",
                "config": {"source": source_id, "topK": top_k},
                "ports": {
                    "in": [{"id": "in", "type": "any", "required": False}],
                    "out": [{"id": "out", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_rag",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_rag",
                "sourceHandle": handle,
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
        ],
    }


def test_rag_step_node_retrieves_into_the_run_output(client: TestClient) -> None:
    source = _create_source(client)
    sid = source["id"]
    _upload(client, sid, "handbook.md", _DOC)
    _wait_settled(client, sid)

    body = client.post(
        "/graphs/runs",
        json={
            "ir": _rag_step_graph(sid),
            "input": "how should I pour water for coffee",
            "stream": False,
        },
    ).json()
    assert body["status"] == "completed", body
    output = json.loads(body["output"])
    assert output["source_id"] == sid
    assert "coffee" in output["matches"][0]["text"].lower()


def test_rag_step_unknown_source_is_rejected_before_a_run(client: TestClient) -> None:
    resp = client.post(
        "/graphs/runs",
        json={
            "ir": _rag_step_graph("rag_00000000000000000000000000"),
            "input": "q",
            "stream": False,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "rag_source_not_found"


def test_rag_step_empty_source_binds_err(client: TestClient) -> None:
    source = _create_source(client)  # exists but nothing ingested
    body = client.post(
        "/graphs/runs",
        json={"ir": _rag_step_graph(source["id"], wire_err=True), "input": "q", "stream": False},
    ).json()
    assert body["status"] == "completed", body  # a retrieval failure is structured, not fatal
    assert "no embedded content" in body["output"]


# ── the rag node: llm capability ─────────────────────────────────────────────


def _rag_capability_graph(source_id: str) -> dict[str, Any]:
    return {
        "schemaVersion": "1.0",
        "id": "agt_rag_cap",
        "name": "rag-capability",
        "version": "0.1.0",
        "models": {"default": {"binding": "mlx", "model": "triage-fast", "params": {}}},
        "tools": {},
        "nodes": [
            {
                "id": "n_in",
                "type": "input",
                "kind": "boundary",
                "ports": {"in": [], "out": [{"id": "out", "type": "any"}]},
            },
            {
                "id": "n_llm",
                "type": "llm",
                "kind": "activity",
                "config": {
                    "model": "default",
                    "messages": [{"role": "user", "content": "$in"}],
                    "maxToolIterations": 4,
                },
                "ports": {
                    "in": [
                        {"id": "in", "type": "any"},
                        {"id": "tools", "type": "any", "required": False, "role": "tool"},
                    ],
                    "out": [{"id": "ok", "type": "any"}, {"id": "err", "type": "error"}],
                },
            },
            {
                "id": "n_rag",
                "type": "rag",
                "kind": "activity",
                "config": {"source": source_id, "description": "search the handbook"},
                "ports": {
                    "in": [{"id": "in", "type": "any", "required": False}],
                    "out": [{"id": "use", "type": "any", "role": "tool"}],
                },
            },
            {
                "id": "n_out",
                "type": "output",
                "kind": "boundary",
                "ports": {"in": [{"id": "in", "type": "any"}], "out": []},
            },
        ],
        "edges": [
            {
                "id": "e1",
                "source": "n_in",
                "sourceHandle": "out",
                "target": "n_llm",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e2",
                "source": "n_llm",
                "sourceHandle": "ok",
                "target": "n_out",
                "targetHandle": "in",
                "channel": "data",
            },
            {
                "id": "e_cap",
                "source": "n_rag",
                "sourceHandle": "use",
                "target": "n_llm",
                "targetHandle": "tools",
                "channel": "tool",
            },
        ],
    }


def test_rag_capability_the_model_retrieves_and_answers(pg_url: str) -> None:
    # The scripted model calls the rag NODE ID with a query, then answers once the tool
    # result is in the transcript — proving schema exposure, dispatch by node id, and the
    # retrieval result feeding back into the loop.
    with FakeInference(
        mode="tool_call",
        tool_name="n_rag",
        tool_args={"query": "what do llamas eat"},
        response="llamas eat hay",
    ) as fake:
        app = create_app(inference_base_url=fake.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            source = _create_source(client)
            sid = source["id"]
            _upload(client, sid, "handbook.md", _DOC)
            _wait_settled(client, sid)

            body = client.post(
                "/graphs/runs",
                json={"ir": _rag_capability_graph(sid), "input": "feeding?", "stream": False},
            ).json()
            assert body["status"] == "completed", body
            assert body["output"] == "llamas eat hay"
            # The model's second turn saw the retrieval result as a tool message.
            tool_messages = [m for m in fake.captured["messages"] if m.get("role") == "tool"]
            assert tool_messages, fake.captured["messages"]
            assert "hay" in tool_messages[0]["content"].lower()


# ── crawl ingestion over a real local site ───────────────────────────────────

_PAGE_STYLE = "<html><head><title>{title}</title></head><body><main>{body}</main></body></html>"
_SITE = {
    "/": _PAGE_STYLE.format(
        title="Docs home",
        body=(
            "<h1>Docs</h1><p>Welcome to the documentation for the gadget. This index links to "
            'the deeper pages of the manual.</p><a href="/setup.html">Setup</a> '
            '<a href="/usage.html">Usage</a>'
        ),
    ),
    "/setup.html": _PAGE_STYLE.format(
        title="Setup",
        body=(
            "<h1>Setup</h1><p>Install the gadget by plugging the purple cable into the port "
            "labelled seven. The gadget boots in about four seconds and blinks green twice "
            "when it is ready to use.</p>"
        ),
    ),
    "/usage.html": _PAGE_STYLE.format(
        title="Usage",
        body=(
            "<h1>Usage</h1><p>Press the round button to start a measurement. Hold it for two "
            "seconds to reset the gadget back to its factory settings without losing data.</p>"
        ),
    ),
}


class _SiteHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        page = _SITE.get(self.path)
        if page is None:
            self.send_response(404)
            self.end_headers()
            return
        body = page.encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: Any) -> None:  # silence per-request stderr noise
        return


@pytest.fixture
def local_site() -> Any:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_crawl_ingests_a_real_local_site(client: TestClient, local_site: str) -> None:
    source = _create_source(client, kind="crawl", root_url=local_site, max_pages=10)
    sid = source["id"]
    resp = client.post(f"/rag/sources/{sid}:ingest")
    assert resp.status_code == 202, resp.text
    settled = _wait_settled(client, sid, timeout=60.0)

    assert settled["status"] == "ready", settled
    assert settled["documents"] >= 2  # the index page may extract or not; the leaves must
    assert settled["chunks"] >= 2
    assert (settled["progress"] or {}).get("pages", 0) >= 3

    result = client.post(
        f"/rag/sources/{sid}/query", json={"query": "purple cable port seven install"}
    ).json()
    assert result["matches"], result
    top = result["matches"][0]
    assert "purple cable" in top["text"]
    assert top["uri"].startswith("http://127.0.0.1")


# ── restart honesty ──────────────────────────────────────────────────────────


async def _seed_ingesting_source(url: str, source_id: str) -> None:
    conn = await asyncpg.connect(dsn=plain_dsn(url))
    try:
        await conn.execute(
            "INSERT INTO rag_source (id, name, kind, config, embedding_model, status, "
            "created_at, updated_at) VALUES ($1, 'stuck', 'upload', '{}', 'embed-small', "
            "'ingesting', now(), now())",
            source_id,
        )
        await conn.execute(
            "INSERT INTO rag_document (id, source_id, uri, status, chars, created_at, "
            "updated_at) VALUES ('rdoc_stuck', $1, 'stuck.md', 'pending', 1, now(), now())",
            source_id,
        )
    finally:
        await conn.close()


def test_startup_sweep_fails_interrupted_ingests(pg_url: str) -> None:
    import asyncio

    asyncio.run(_seed_ingesting_source(pg_url, "rag_stuck"))
    with FakeInference() as fake:
        app = create_app(inference_base_url=fake.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            source = client.get("/rag/sources/rag_stuck").json()
            assert source["status"] == "failed"
            assert "interrupted" in source["error"]
            docs = client.get("/rag/sources/rag_stuck/documents").json()["documents"]
            assert docs[0]["status"] == "failed"


# ── durable-runtime parity ───────────────────────────────────────────────────


async def test_durable_rag_step_matches_the_walker(pg_url: str) -> None:
    # The same rag step graph produces the same retrieval output through the durable
    # theygent_run (a journaled _rag_step) as through the interactive walker — the
    # both-runtimes convention every activity type holds to.
    import contextlib

    from _durable import reset_dbos_schema, save_agent
    from theygent_control_plane import db
    from theygent_control_plane.durable.runtime import DurableRuntime
    from theygent_control_plane.mcp import McpManager
    from theygent_control_plane.rag import RagRetriever
    from theygent_control_plane.store import AgentStore, RunStore, TriggerStore
    from theygent_gateway_client import GatewayClient

    await reset_dbos_schema(pg_url)
    question = "what do llamas eat every morning"
    with FakeInference() as fake:
        app = create_app(
            inference_base_url=fake.v1_url, database_url=pg_url, start_dispatcher=False
        )
        with TestClient(app) as client:
            source = _create_source(client)
            sid = source["id"]
            _upload(client, sid, "handbook.md", _DOC)
            _wait_settled(client, sid)
            interactive = client.post(
                "/graphs/runs",
                json={"ir": _rag_step_graph(sid), "input": question, "stream": False},
            ).json()
            assert interactive["status"] == "completed"

        ir = _rag_step_graph(sid)
        ir["id"] = "agt_rag_durable"
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        gw = GatewayClient(fake.v1_url, max_retries=0)
        rt = DurableRuntime(
            database_url=pg_url,
            gateway=gw,
            mcp=McpManager(),
            store=RunStore(),
            agents=agents,
            triggers=TriggerStore(),
            sessionmaker=sm,
            fast_polling=True,
            rag=RagRetriever(sm, gw),
        )
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, question
            )
            durable = await handle.get_result()
            assert durable["status"] == "completed"
            durable_out = json.loads(durable["output"])
            interactive_out = json.loads(interactive["output"])
            assert "llama" in durable_out["matches"][0]["text"].lower()
            # Behavioral parity: identical matches through both runtimes.
            assert durable_out["matches"] == interactive_out["matches"]
        finally:
            rt.shutdown()
            with contextlib.suppress(Exception):
                from dbos import DBOS

                DBOS.destroy(destroy_registry=False)
            await gw.aclose()
            await engine.dispose()


def test_browser_install_hint_is_deployment_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix for a missing JS-rendering browser differs by deployment: a container needs
    a different IMAGE (a runtime install cannot work there), bare-metal needs one command.
    The hint must say the right thing in each."""
    from pathlib import Path

    from theygent_control_plane.rag.crawl import _browser_install_hint

    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.96.0.1")
    hint = _browser_install_hint()
    assert "WITH_JS_RENDER=1" in hint and "render_js off" in hint

    monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
    if not Path("/.dockerenv").exists():  # bare-metal branch (dev machines, CI VMs)
        assert "playwright install chromium" in _browser_install_hint()
