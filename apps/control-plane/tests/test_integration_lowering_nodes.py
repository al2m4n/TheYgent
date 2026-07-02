"""Real durable integration proof — real Postgres + real DBOS + real MLX.

The fast suite proves each lowering against real embedded DBOS with a fake model frozen mid-step.
This env-gated test adds the headline demo on the real surface: an agent with a ``map`` fan-out
**and** a ``human`` gate, on real MLX, killed mid-run, resumes — the wait survives the restart and
the map branches ran for real ("durable, expressive, air-gapped").

Skipped by default (``-m 'not integration'``); skips clean without prerequisites. Run (Apple
Silicon, an inference plane already serving a logical id ``triage-fast``)::

    DATABASE_URL=postgresql+asyncpg://localhost/theygent \
    THEYGENT_INFERENCE_PLANE_BASE_URL=http://127.0.0.1:8081/v1 \
        uv run --package theygent-control-plane pytest -m integration \
        apps/control-plane/tests/test_integration_lowering_nodes.py

The full hand-driven clip — an agent that calls a sub-agent, fans out over a list,
then pauses for human approval, killed mid-run and resumed — is the 30-second architecture story.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
import pytest
from _ir import _doc, _edge, _node

pytestmark = pytest.mark.integration

_DATABASE_URL = os.environ.get("DATABASE_URL")
_INFERENCE_BASE_URL = os.environ.get("THEYGENT_INFERENCE_PLANE_BASE_URL")
_MODEL = os.environ.get("THEYGENT_LOGICAL_MODEL", "triage-fast")

_skip = pytest.mark.skipif(
    not _DATABASE_URL or not _INFERENCE_BASE_URL,
    reason="needs DATABASE_URL (real PG) and THEYGENT_INFERENCE_PLANE_BASE_URL "
    "(a live inference plane)",
)


def _prepare_db(url: str) -> None:
    from alembic import command
    from alembic.config import Config
    from theygent_control_plane.durable import run_dbos_migrations

    ini = Path(__file__).resolve().parents[1] / "alembic.ini"
    os.environ["DATABASE_URL"] = url
    cfg = Config(str(ini))
    command.upgrade(cfg, "head")
    run_dbos_migrations(url)


def _llm_agent(agent_id: str, prompt: str) -> dict:
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_llm",
                "llm",
                "activity",
                config={"model": "default", "messages": [{"role": "user", "content": prompt}]},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_llm"), _edge("e2", "n_llm", "ok", "n_out")],
        models={"default": {"binding": "mlx", "model": _MODEL, "params": {"maxTokens": 16}}},
    )
    ir["id"] = agent_id
    return ir


def _body_ir() -> dict:
    # The map body: a real one-word MLX classification per element.
    return _llm_agent("agt_m14_body", "Reply with one word: $in")


def _summarizer_ir() -> dict:
    # The sub-agent the demo CALLS (subgraph): a real MLX summary over the map's results.
    return _llm_agent("agt_m14_sum", "In one word, summarize: $in")


def _demo_ir(body_version: str, sum_version: str) -> dict:
    # The headline shape — all three new features in one run: the agent CALLS a sub-agent
    # (subgraph), FANS OUT over a list (map), then PAUSES for human approval (human):
    #   input → map(body per element) → subgraph(summarizer) → human(approve) → output.
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_map",
                "map",
                "orchestration",
                config={"agent": "agt_m14_body", "version": body_version, "onError": "collect"},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_sg",
                "subgraph",
                "boundary",
                config={"agent": "agt_m14_sum", "version": sum_version},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_h", "human", "boundary", config={"prompt": "ok?"}, ins=["in"], outs=["ok"]),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_map"),
            _edge("e2", "n_map", "ok", "n_sg"),
            _edge("e3", "n_sg", "ok", "n_h"),
            _edge("e4", "n_h", "ok", "n_out"),
        ],
    )
    ir["id"] = "agt_m14_demo"
    return ir


@_skip
async def test_human_gate_and_map_fanout_resumes_on_mlx() -> None:
    assert _DATABASE_URL and _INFERENCE_BASE_URL
    # Run the sync Alembic upgrade off this event loop — its env.py uses asyncio.run() internally,
    # which collides with pytest-asyncio's running loop (latent: this env-gated test never ran).
    await asyncio.to_thread(_prepare_db, _DATABASE_URL)

    from _durable import save_agent
    from theygent_control_plane import db
    from theygent_control_plane.durable.runtime import DurableRuntime
    from theygent_control_plane.mcp import McpManager
    from theygent_control_plane.store import AgentStore, RunStore, TriggerStore
    from theygent_gateway_client import GatewayClient

    async with httpx.AsyncClient(base_url=_INFERENCE_BASE_URL, timeout=10) as probe:
        try:
            models = (await probe.get("/models")).json()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"inference plane unreachable: {exc}")
    if _MODEL not in {m["id"] for m in models.get("data", [])}:
        pytest.skip(f"logical id {_MODEL!r} not served")

    engine = db.create_engine(_DATABASE_URL)
    sm = db.create_sessionmaker(engine)
    agents = AgentStore()
    _, bver = await save_agent(sm, agents, _body_ir())
    _, sver = await save_agent(sm, agents, _summarizer_ir())
    aid, ver = await save_agent(sm, agents, _demo_ir(bver, sver))

    def _runtime() -> tuple[DurableRuntime, GatewayClient]:
        gw = GatewayClient(_INFERENCE_BASE_URL, max_retries=0)
        rt = DurableRuntime(
            database_url=_DATABASE_URL,
            gateway=gw,
            mcp=McpManager(),
            store=RunStore(),
            agents=agents,
            triggers=TriggerStore(),
            sessionmaker=sm,
        )
        return rt, gw

    rt, gw = _runtime()
    rt.launch()
    ref = {"agent_id": aid, "version": ver, "content_hash": None}
    handle = await rt.enqueue_run(ref, ["sky", "grass"])
    wid = handle.workflow_id
    # The map fans out over MLX, then the run pauses at the human gate.
    for _ in range(2000):
        async with sm() as s:
            run = await RunStore().get_run(s, wid)
        if run is not None and run.status == "waiting":
            break
        await asyncio.sleep(0.05)
    assert run is not None and run.status == "waiting" and run.awaiting_node == "n_h"

    rt.shutdown()  # KILL the worker while paused at the human gate

    from dbos import DBOS

    rt2, gw2 = _runtime()
    rt2.launch()  # the wait survives the restart
    try:
        await rt2.resume(wid, "approved")
        rh = await DBOS.retrieve_workflow_async(wid)
        res = await rh.get_result()
        assert res["status"] == "completed", res
        assert res["output"] == "approved"
        # All three features ran for real on MLX: the sub-agent was CALLED (one subgraph child run)
        # and the map FANNED OUT (one child run per element) — and the human wait survived the kill.
        async with sm() as s:
            runs = await RunStore().list_runs(s, limit=50)
        assert len([r for r in runs if r.graph_id == "agt_m14_body"]) == 2  # map: 2 branches
        assert len([r for r in runs if r.graph_id == "agt_m14_sum"]) == 1  # subgraph: 1 child
    finally:
        rt2.shutdown()
        await gw.aclose()
        await gw2.aclose()
        await engine.dispose()
