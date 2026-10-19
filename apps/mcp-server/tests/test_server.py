"""The MCP server over an in-memory MCP client, against fake planes answered by an httpx transport.

Each test drives a tool the way a coding agent would (by name, with JSON arguments) and checks
both sides: what the tool returns, and the exact request it sent to the plane.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from theygent_mcp.config import Config
from theygent_mcp.server import build_server

API_KEY = "tyk_test-key"
_TRIVIAL_IR: dict[str, Any] = {
    "schemaVersion": "1.0",
    "id": "agt_trivial",
    "name": "trivial",
    "version": "0.1.0",
    "models": {"default": {"binding": "mlx", "model": "local-chat", "params": {}}},
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
            "config": {"model": "default", "messages": [{"role": "user", "content": "$in"}]},
            "ports": {"in": [{"id": "in", "type": "any"}], "out": [{"id": "out", "type": "any"}]},
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
            "sourceHandle": "out",
            "target": "n_out",
            "targetHandle": "in",
            "channel": "data",
        },
    ],
}


class FakePlanes:
    """Answers both planes' routes and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[tuple[str, str], Any] = {
            ("GET", "/admin/models"): {
                "models": [
                    {
                        "logicalId": "local-embed",
                        "binding": {"binding": "llamacpp", "modality": "embeddings", "model": "e"},
                    },
                    {
                        "logicalId": "local-chat",
                        "binding": {"binding": "mlx", "modality": "chat", "model": "q"},
                    },
                ]
            },
            ("POST", "/v1/chat/completions"): {
                "choices": [
                    {"message": {"content": "a summary"}, "finish_reason": "stop"},
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 3},
            },
            ("GET", "/agents"): {
                "agents": [
                    {
                        "id": "agt_triage",
                        "name": "triage",
                        "latest_version": "1.2.0",
                        "version_count": 3,
                        "updated_at": "2026-10-18T10:00:00Z",
                        "latest_content_hash": "sha256:abc",
                    }
                ]
            },
            ("POST", "/agents/agt_triage/runs"): {
                "runId": "run_1",
                "status": "completed",
                "output": "routed to billing",
            },
            ("POST", "/graphs/runs"): {"runId": "run_2", "status": "completed", "output": "hi"},
            ("GET", "/runs/run_1"): {
                "id": "run_1",
                "status": "completed",
                "output": "routed to billing",
                "error": None,
                "graph_id": "agt_triage",
                "internal": "not passed on",
            },
            ("POST", "/agents"): {
                "id": "agt_trivial",
                "name": "trivial",
                "versions": [{"version": "0.1.0"}],
            },
            ("GET", "/agents/agt_trivial"): {
                "id": "agt_trivial",
                "name": "trivial",
                "versions": [{"version": "0.2.0"}, {"version": "0.1.0"}],
            },
            ("GET", "/agents/agt_trivial/versions/0.2.0"): {"ir": _TRIVIAL_IR},
            ("GET", "/rag/sources"): {"sources": [{"id": "rag_1", "name": "Team docs"}]},
            ("POST", "/rag/sources/rag_1/query"): {"matches": [{"text": "chunk", "score": 0.9}]},
            ("POST", "/rag/sources/rag_1/documents"): {"id": "doc_1", "status": "pending"},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.routes:
            return httpx.Response(
                404, json={"error": {"message": f"no route {key}", "code": "not_found"}}
            )
        body = self.routes[key]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    def last(self, method: str, path: str) -> httpx.Request:
        return next(r for r in reversed(self.requests) if r.method == method and r.url.path == path)


@pytest.fixture
def planes() -> FakePlanes:
    return FakePlanes()


def _client(planes: FakePlanes, **config: Any) -> Client:
    cfg = Config(api_key=API_KEY, **config)
    return Client(build_server(cfg, transport=httpx.MockTransport(planes.handler)))


async def _call(client: Client, tool: str, **args: Any) -> Any:
    result = await client.call_tool(tool, args)
    return result.structured_content if result.structured_content is not None else result.data


async def test_every_tool_is_listed_with_a_description(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        tools = {t.name: t for t in await client.list_tools()}
    expected = {
        "list_models",
        "ask_model",
        "list_agents",
        "get_agent",
        "run_agent",
        "run_graph",
        "get_run",
        "agent_authoring_guide",
        "validate_agent",
        "create_agent",
        "publish_agent_version",
        "list_samples",
        "install_samples",
        "list_rag_sources",
        "search_rag",
        "create_rag_source",
        "add_rag_files",
        "add_rag_text",
        "ingest_rag_source",
        "get_rag_source",
    }
    assert expected <= set(tools)
    assert all(tools[name].description for name in expected)


# ── models ────────────────────────────────────────────────────────────────────


async def test_list_models_reports_engines_modalities_and_the_default(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(client, "list_models")
    assert out["default_chat_model"] == "local-chat"
    assert {"id": "local-chat", "engine": "mlx", "modality": "chat", "model": "q"} in out["models"]


async def test_ask_model_calls_the_inference_plane_directly(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(client, "ask_model", prompt="Summarize this", system="Be brief")
    assert out["answer"] == "a summary" and out["model"] == "local-chat"
    sent = planes.last("POST", "/v1/chat/completions")
    assert sent.url.host == "127.0.0.1" and sent.url.port == 8081
    body = json.loads(sent.content)
    assert body["messages"] == [
        {"role": "system", "content": "Be brief"},
        {"role": "user", "content": "Summarize this"},
    ]
    # Model calls go straight to the inference plane: no control-plane credential rides along.
    assert "authorization" not in sent.headers


async def test_ask_model_explains_an_answer_lost_to_the_token_cap(planes: FakePlanes) -> None:
    planes.routes[("POST", "/v1/chat/completions")] = {
        "choices": [
            {"message": {"content": "", "reasoning": "thinking…"}, "finish_reason": "length"}
        ]
    }
    async with _client(planes) as client:
        out = await _call(client, "ask_model", prompt="hi", model="local-chat", max_tokens=8)
    assert "larger max_tokens" in out["note"] and out["reasoning"] == "thinking…"


async def test_ask_model_can_turn_thinking_off_and_keeps_reasoning_out_of_answers(
    planes: FakePlanes,
) -> None:
    planes.routes[("POST", "/v1/chat/completions")] = {
        "choices": [
            {
                "message": {"content": "\n\nfeat: add retries", "reasoning_content": "long…"},
                "finish_reason": "stop",
            }
        ]
    }
    async with _client(planes) as client:
        out = await _call(client, "ask_model", prompt="title this", think=False)
        default = await _call(client, "ask_model", prompt="title this")
    assert out["answer"] == "feat: add retries" and "reasoning" not in out
    sent = [json.loads(r.content) for r in planes.requests if r.url.path == "/v1/chat/completions"]
    assert sent[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in sent[1] and default["answer"]


async def test_ask_model_reads_local_files_into_the_prompt_itself(
    planes: FakePlanes, tmp_path: Path
) -> None:
    log = tmp_path / "build.log"
    log.write_text("ERROR: test_refunds failed\n")
    blob = tmp_path / "model.bin"
    blob.write_bytes(b"\0\1\2")
    async with _client(planes) as client:
        await _call(client, "ask_model", prompt="Summarize the failures.", files=[str(log)])
        with pytest.raises(ToolError, match="binary"):
            await client.call_tool("ask_model", {"prompt": "x", "files": [str(blob)]})
    content = json.loads(planes.last("POST", "/v1/chat/completions").content)["messages"][-1][
        "content"
    ]
    assert content.startswith("Summarize the failures.")
    assert f"--- {log} ---" in content and "ERROR: test_refunds failed" in content


async def test_ask_model_without_any_chat_model_says_how_to_add_one(planes: FakePlanes) -> None:
    planes.routes[("GET", "/admin/models")] = {"models": []}
    async with _client(planes) as client:
        with pytest.raises(ToolError, match="No chat model"):
            await client.call_tool("ask_model", {"prompt": "hi"})


# ── agents and runs ───────────────────────────────────────────────────────────


async def test_list_agents_sends_the_api_key_and_summarizes(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(client, "list_agents")
    assert out["agents"] == [
        {
            "id": "agt_triage",
            "name": "triage",
            "latest_version": "1.2.0",
            "versions": 3,
            "updated_at": "2026-10-18T10:00:00Z",
        }
    ]
    sent = planes.last("GET", "/agents")
    assert sent.headers["authorization"] == f"Bearer {API_KEY}"
    assert sent.url.port == 8080


async def test_run_agent_waits_for_the_output(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(
            client, "run_agent", agent_id="agt_triage", input={"ticket": "refund?"}, version="1.2.0"
        )
    assert out == {"run_id": "run_1", "status": "completed", "output": "routed to billing"}
    body = json.loads(planes.last("POST", "/agents/agt_triage/runs").content)
    assert body == {"input": {"ticket": "refund?"}, "stream": False, "version": "1.2.0"}


async def test_an_agent_can_be_named_instead_of_its_id(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(client, "run_agent", agent_id="Triage", input="refund?")
    assert out["run_id"] == "run_1"
    assert planes.last("POST", "/agents/agt_triage/runs")


async def test_get_run_returns_the_public_fields(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(client, "get_run", run_id="run_1")
    assert out["status"] == "completed" and "internal" not in out


async def test_run_graph_tests_an_unpublished_document(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(client, "run_graph", ir=_TRIVIAL_IR, input="hello")
    assert out["run_id"] == "run_2"
    body = json.loads(planes.last("POST", "/graphs/runs").content)
    assert body["ir"]["id"] == "agt_trivial" and body["stream"] is False


async def test_create_and_read_back_an_agent(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        created = await _call(client, "create_agent", ir=_TRIVIAL_IR, name="Trivial")
        read = await _call(client, "get_agent", agent_id="agt_trivial")
    assert created == {"id": "agt_trivial", "name": "trivial", "versions": ["0.1.0"]}
    assert json.loads(planes.last("POST", "/agents").content)["name"] == "Trivial"
    assert read["version"] == "0.2.0" and read["ir"]["id"] == "agt_trivial"


async def test_a_plane_error_reaches_the_agent_with_its_code(planes: FakePlanes) -> None:
    planes.routes[("POST", "/agents")] = httpx.Response(
        409, json={"error": {"message": "agent 'agt_trivial' exists", "code": "agent_exists"}}
    )
    async with _client(planes) as client:
        with pytest.raises(ToolError, match="agent_exists"):
            await client.call_tool("create_agent", {"ir": _TRIVIAL_IR})


async def test_a_missing_api_key_says_where_to_get_one(planes: FakePlanes) -> None:
    client = Client(
        build_server(Config(api_key=None), transport=httpx.MockTransport(planes.handler))
    )
    async with client:
        with pytest.raises(ToolError, match="THEYGENT_API_KEY"):
            await client.call_tool("list_agents", {})
    assert not planes.requests


async def test_a_rejected_key_and_an_unreachable_plane_are_explained(planes: FakePlanes) -> None:
    planes.routes[("GET", "/agents")] = httpx.Response(401, json={"detail": "bad token"})
    async with _client(planes) as client:
        with pytest.raises(ToolError, match="rejected the API key"):
            await client.call_tool("list_agents", {})

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    down = Client(build_server(Config(api_key=API_KEY), transport=httpx.MockTransport(unreachable)))
    async with down:
        with pytest.raises(ToolError, match="make up"):
            await down.call_tool("list_models", {})


# ── authoring ─────────────────────────────────────────────────────────────────


async def test_the_guide_lists_runnable_node_types_with_ports(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(client, "agent_authoring_guide")
        detail = await _call(client, "agent_authoring_guide", node_types=["rag"])
    assert "$in" in out["guide"]
    types = {t["type"]: t for t in out["node_types"]}
    assert types["llm"]["kind"] == "activity" and types["llm"]["ports"]["in"] == ["in", "tools"]
    assert "configSchema" in detail["node_types"][0]


async def test_validate_agent_accepts_a_good_document_and_names_problems(
    planes: FakePlanes,
) -> None:
    broken = json.loads(json.dumps(_TRIVIAL_IR))
    broken["nodes"][1]["kind"] = "boundary"
    async with _client(planes) as client:
        good = await _call(client, "validate_agent", ir=_TRIVIAL_IR)
        bad = await _call(client, "validate_agent", ir=broken)
    assert good == {"valid": True, "id": "agt_trivial", "version": "0.1.0", "nodes": 3}
    assert bad["valid"] is False and bad["problems"]
    assert not planes.requests  # validation is local


# ── retrieval ─────────────────────────────────────────────────────────────────


async def test_search_rag_passes_query_and_top_k(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(client, "search_rag", source_id="rag_1", query="refunds", top_k=3)
    assert out["matches"][0]["text"] == "chunk"
    assert json.loads(planes.last("POST", "/rag/sources/rag_1/query").content) == {
        "query": "refunds",
        "top_k": 3,
    }


async def test_a_source_can_be_named_and_an_unknown_name_lists_the_real_ones(
    planes: FakePlanes,
) -> None:
    async with _client(planes) as client:
        out = await _call(client, "search_rag", source_id="team docs", query="refunds")
        with pytest.raises(ToolError, match="'Team docs'"):
            await client.call_tool("search_rag", {"source_id": "wiki", "query": "refunds"})
    assert out["matches"][0]["text"] == "chunk"


async def test_add_rag_files_uploads_local_files_and_reports_bad_paths(
    planes: FakePlanes, tmp_path: Path
) -> None:
    doc = tmp_path / "guide.md"
    doc.write_text("# Refunds\nWithin 30 days.")
    async with _client(planes) as client:
        out = await _call(
            client,
            "add_rag_files",
            source_id="rag_1",
            paths=[str(doc), str(tmp_path / "missing.md")],
        )
    assert out["uploaded"] == 1 and out["failed"] == 1
    by_path = {r["path"]: r for r in out["results"]}
    assert by_path["guide.md"] == {
        "path": "guide.md",
        "uploaded": True,
        "bytes": doc.stat().st_size,
    }
    assert by_path[str(tmp_path / "missing.md")]["error"] == "not a file"
    sent = planes.last("POST", "/rag/sources/rag_1/documents")
    assert sent.url.params["filename"] == "guide.md"
    assert sent.headers["content-type"] == "text/markdown"
    assert sent.content == doc.read_bytes()


async def test_add_rag_files_leaves_the_size_limit_to_the_control_plane(
    planes: FakePlanes, tmp_path: Path
) -> None:
    # The upload limit is the control plane's live setting, raisable past its 50 MiB default: a
    # bigger file streams up with its declared length, and only a 413 from the control plane
    # skips it.
    big = tmp_path / "manual.pdf"
    with big.open("wb") as fh:
        fh.truncate(60 * 1024 * 1024)  # sparse: no disk is written
    huge = tmp_path / "archive.pdf"
    with huge.open("wb") as fh:
        fh.truncate(70 * 1024 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if int(request.headers["content-length"]) > 64 * 1024 * 1024:
            return httpx.Response(
                413,
                json={
                    "error": {
                        "message": "document exceeds the 64 MiB upload limit",
                        "code": "document_too_large",
                    }
                },
            )
        return planes.handler(request)

    client = Client(build_server(Config(api_key=API_KEY), transport=httpx.MockTransport(handler)))
    async with client:
        out = await _call(client, "add_rag_files", source_id="rag_1", paths=[str(big), str(huge)])
    assert (out["uploaded"], out["skipped"], out["failed"]) == (1, 1, 0)
    by_path = {r["path"]: r for r in out["results"]}
    assert by_path["manual.pdf"]["bytes"] == 60 * 1024 * 1024
    assert by_path["archive.pdf"]["skipped"] == (
        f"{70 * 1024 * 1024} bytes: document exceeds the 64 MiB upload limit. An admin "
        "can raise the limit in TheYgent under Settings → RAG."
    )
    sent = planes.last("POST", "/rag/sources/rag_1/documents")
    assert sent.url.params["filename"] == "manual.pdf"
    assert sent.headers["content-length"] == str(60 * 1024 * 1024)
    assert len(sent.content) == 60 * 1024 * 1024


async def test_add_rag_files_walks_a_project_tree(
    planes: FakePlanes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for rel in (
        "README.md",
        "src/app/index.ts",
        "src/lib/index.ts",
        "src/logo.png",
        "src/empty.py",
        "node_modules/dep/index.js",
        ".git/config",
        "src/.env",
    ):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"" if rel.endswith("empty.py") else b"content")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("filename", "").endswith(".png"):
            return httpx.Response(
                422, json={"error": {"message": "unsupported", "code": "unsupported_document"}}
            )
        return planes.handler(request)

    monkeypatch.chdir(tmp_path)
    client = Client(build_server(Config(api_key=API_KEY), transport=httpx.MockTransport(handler)))
    async with client:
        tree = await _call(client, "add_rag_files", source_id="rag_1", paths=["."])
        pattern = await _call(client, "add_rag_files", source_id="rag_1", paths=["src/**/*.ts"])
        nothing = await _call(client, "add_rag_files", source_id="rag_1", paths=["*.rs"])
    # Same-named files stay distinct documents; hidden, dependency and empty files never upload.
    sent = [r.url.params["filename"] for r in planes.requests if r.method == "POST"]
    assert sent[:3] == ["README.md", "src/app/index.ts", "src/lib/index.ts"]
    assert (tree["uploaded"], tree["skipped"], tree["failed"]) == (3, 2, 0)
    assert {r["path"]: r.get("skipped") for r in tree["results"] if "skipped" in r} == {
        "src/empty.py": "empty file",
        "src/logo.png": "TheYgent can't read this type",
    }
    assert pattern["uploaded"] == 2
    assert nothing["results"] == [{"path": "*.rs", "error": "no files match this pattern"}]


async def test_add_rag_text_names_a_bare_document_as_markdown(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        out = await _call(
            client, "add_rag_text", source_id="Team docs", filename="standup", text="# Notes"
        )
    assert out["filename"] == "standup.md"
    sent = planes.last("POST", "/rag/sources/rag_1/documents")
    assert sent.url.params["filename"] == "standup.md" and sent.content == b"# Notes"


async def test_create_rag_source_requires_a_root_url_to_crawl(planes: FakePlanes) -> None:
    async with _client(planes) as client:
        with pytest.raises(ToolError, match="root_url"):
            await client.call_tool(
                "create_rag_source",
                {"name": "docs", "embedding_model": "local-embed", "kind": "crawl"},
            )
