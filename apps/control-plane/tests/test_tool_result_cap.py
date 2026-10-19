"""The tool-result cap: how much of one tool result an llm's tool loop shows the model.

Every tool kind feeds its result back through ``tool_result_message``, so the cap lives there and
both runtimes apply it the same way: the platform's ``tools.result_max_chars`` setting by default,
an llm node's ``maxToolResultChars`` when it sets one. A cut result ends with a marker that says
how much was left out, so the model never takes a partial result for the whole one. The run
inspector's record keeps the whole result.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
from _durable import reset_dbos_schema, save_agent
from _fake_inference import FakeInference
from _ir import llm_ir, trivial_ir
from fastapi.testclient import TestClient
from theygent_control_plane import db
from theygent_control_plane.app import create_app
from theygent_control_plane.durable.runtime import DurableRuntime
from theygent_control_plane.mcp import McpManager
from theygent_control_plane.settings import CATALOG
from theygent_control_plane.store import AgentStore, RunStore, TriggerStore
from theygent_control_plane.walker import (
    DEFAULT_TOOL_RESULT_MAX_CHARS,
    ActivityOutcome,
    ToolCall,
    cap_tool_result,
    tool_result_message,
)

#: A tool result well over the default cap — what three crawled pages look like to the loop.
BIG = "".join(f"line {i:05d} of a crawled page\n" for i in range(2_000))  # 58,000 characters
_ECHO = {"echo": {"kind": "builtin", "ref": "echo"}}


def _marker(shown: int, total: int) -> str:
    return (
        f"\n\n[tool result truncated: showing the first {shown:,} of {total:,} characters; "
        f"{total - shown:,} characters were left out]"
    )


def _tool_message(messages: Any) -> str:
    assert isinstance(messages, list)
    tool = [m for m in messages if isinstance(m, dict) and m.get("role") == "tool"]
    assert len(tool) == 1, messages
    return tool[0]["content"]


# ── the cap itself ───────────────────────────────────────────────────────────


def test_a_result_within_the_cap_is_unchanged() -> None:
    assert cap_tool_result("short", 10) == "short"
    assert cap_tool_result("x" * 10, 10) == "x" * 10
    assert cap_tool_result(BIG, None) == BIG


def test_a_longer_result_is_cut_and_says_how_much_was_left_out() -> None:
    capped = cap_tool_result(BIG, 1000)
    assert capped == BIG[:1000] + _marker(1000, len(BIG))
    assert "57,000 characters were left out" in capped


def test_the_message_caps_the_serialized_result_whatever_its_shape() -> None:
    # An http tool returns a {status, body, headers} envelope; the cap applies to the JSON the
    # model reads, not to one field of it.
    call = ToolCall(id="c1", name="fetch", arguments={})
    outcome = ActivityOutcome(ok=True, value={"status": 200, "body": BIG, "headers": {}})
    msg = tool_result_message(call, outcome, 2000)
    assert msg["role"] == "tool" and msg["tool_call_id"] == "c1"
    assert msg["content"].startswith('{"status": 200, "body": "line 00000')
    assert "[tool result truncated: showing the first 2,000 of" in msg["content"]


def test_the_platform_setting_defaults_to_the_built_in_cap() -> None:
    spec = next(s for s in CATALOG if s.key == "tools.result_max_chars")
    assert spec.default == DEFAULT_TOOL_RESULT_MAX_CHARS == 16_000
    assert spec.group == "tools"
    assert spec.constraints == {"min": 1000, "max": 1_000_000}


# ── the interactive walker ───────────────────────────────────────────────────


def _tool_graph(*, result_cap: int | None = None) -> dict:
    ir = llm_ir("$in")  # input → llm → output
    ir["tools"] = _ECHO
    ir["nodes"][1]["config"]["tools"] = ["echo"]
    if result_cap is not None:
        ir["nodes"][1]["config"]["maxToolResultChars"] = result_cap
    return ir


@contextlib.contextmanager
def _client(pg_url: str) -> Iterator[tuple[TestClient, FakeInference]]:
    with FakeInference(
        mode="tool_call", tool_name="echo", tool_args={"value": BIG}, response="done"
    ) as fake:
        app = create_app(inference_base_url=fake.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            yield client, fake


def _run(client: TestClient, ir: dict) -> dict:
    resp = client.post("/graphs/runs", json={"ir": ir, "input": "go", "stream": False})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed" and body["output"] == "done", body
    return body


def test_walker_applies_the_platform_default(pg_url: str) -> None:
    with _client(pg_url) as (client, fake):
        body = _run(client, _tool_graph())
        spans = client.get(f"/runs/{body['runId']}/trace").json()["spans"]
    content = _tool_message(fake.captured["messages"])
    assert content == BIG[:16_000] + _marker(16_000, len(BIG))
    # The waterfall's tool span says the model saw only part of the result.
    tool_span = next(s for s in spans if s["name"] == "tool.echo")
    assert tool_span["attributes"]["tool.result_chars"] == len(BIG)
    assert tool_span["attributes"]["tool.result_truncated"] is True


def test_walker_applies_a_changed_platform_setting(pg_url: str) -> None:
    with _client(pg_url) as (client, fake):
        resp = client.patch("/settings", json={"tools.result_max_chars": 2500})
        assert resp.status_code == 200, resp.text
        _run(client, _tool_graph())
    assert _tool_message(fake.captured["messages"]) == BIG[:2500] + _marker(2500, len(BIG))


def test_walker_node_cap_overrides_the_platform_setting(pg_url: str) -> None:
    with _client(pg_url) as (client, fake):
        client.patch("/settings", json={"tools.result_max_chars": 2500})
        _run(client, _tool_graph(result_cap=40_000))
    # Above the platform setting: the node's own cap wins in both directions.
    assert _tool_message(fake.captured["messages"]) == BIG[:40_000] + _marker(40_000, len(BIG))


def test_walker_small_result_reaches_the_model_whole(pg_url: str) -> None:
    with FakeInference(
        mode="tool_call", tool_name="echo", tool_args={"value": "tiny"}, response="done"
    ) as fake:
        app = create_app(inference_base_url=fake.v1_url, database_url=pg_url)
        with TestClient(app) as client:
            _run(client, _tool_graph(result_cap=1000))
    assert _tool_message(fake.captured["messages"]) == "tiny"


def test_setting_rejects_a_cap_below_its_floor(pg_url: str) -> None:
    with _client(pg_url) as (client, _):
        resp = client.patch("/settings", json={"tools.result_max_chars": 10})
    assert resp.status_code == 422
    assert "tools.result_max_chars" in resp.json()["error"]["message"]


# ── the durable runtime ──────────────────────────────────────────────────────


@pytest.fixture
def _dbos_teardown() -> Iterator[None]:
    yield
    with contextlib.suppress(Exception):
        from dbos import DBOS

        DBOS.destroy(destroy_registry=False)


async def _durable_tool_message(pg_url: str, *, platform: int | None, node: int | None) -> str:
    await reset_dbos_schema(pg_url)
    ir = trivial_ir()
    ir["id"] = f"agt_cap_{platform}_{node}"
    ir["tools"] = _ECHO
    ir["nodes"][1]["config"]["tools"] = ["echo"]
    if node is not None:
        ir["nodes"][1]["config"]["maxToolResultChars"] = node
    from theygent_gateway_client import GatewayClient

    with FakeInference(
        mode="tool_call", tool_name="echo", tool_args={"value": BIG}, response="durable done"
    ) as fake:
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
            tool_result_max_chars=(lambda: platform) if platform is not None else None,
        )
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, "do it"
            )
            result = await handle.get_result()
            assert result["status"] == "completed" and result["output"] == "durable done"
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()
        return _tool_message(fake.captured["messages"])


@pytest.mark.usefixtures("_dbos_teardown")
async def test_durable_applies_the_built_in_default_without_a_setting(pg_url: str) -> None:
    content = await _durable_tool_message(pg_url, platform=None, node=None)
    assert content == BIG[:16_000] + _marker(16_000, len(BIG))


@pytest.mark.usefixtures("_dbos_teardown")
async def test_durable_applies_the_platform_setting(pg_url: str) -> None:
    content = await _durable_tool_message(pg_url, platform=3000, node=None)
    assert content == BIG[:3000] + _marker(3000, len(BIG))


@pytest.mark.usefixtures("_dbos_teardown")
async def test_durable_node_cap_overrides_the_platform_setting(pg_url: str) -> None:
    content = await _durable_tool_message(pg_url, platform=3000, node=1200)
    assert content == BIG[:1200] + _marker(1200, len(BIG))
