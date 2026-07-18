"""The real durable proof (real Postgres + real DBOS + real MLX).

The fast suite proves kill-and-resume against **real embedded DBOS** (the same runtime), with a fake
model frozen mid-activity. This env-gated test adds the one thing the fast suite can't: a real
multi-step agent whose activities are real MLX inference calls, executed through the durable
``theygent_run`` workflow end to end — the "reliable to run unattended" proof on the real surface.

Skipped by default (``-m 'not integration'``); skips clean without prerequisites. Run (Apple
Silicon, an inference plane already serving a logical id ``triage-fast``)::

    DATABASE_URL=postgresql+asyncpg://localhost/theygent \
    THEYGENT_INFERENCE_PLANE_BASE_URL=http://127.0.0.1:8081/v1 \
        uv run --package theygent-control-plane pytest -m integration \
        apps/control-plane/tests/test_integration_durable.py

The full **kill-the-worker-mid-MLX-run** demo is the hand-driven proof: start a run,
``kill`` the worker process, restart it, watch it resume and finish without re-doing the completed
step. The deterministic version of that property is the fast suite's kill-and-resume test.
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


def _two_step_ir() -> dict:
    # input → llm(extract) → llm(summarise) → output, both real MLX calls. The 2nd consumes the
    # 1st's output ($in.in), so a resume that re-ran the 1st would change the 2nd's input.
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_a",
                "llm",
                "activity",
                config={
                    "model": "default",
                    "messages": [{"role": "user", "content": "Reply with one word: $in"}],
                },
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_b",
                "llm",
                "activity",
                config={
                    "model": "default",
                    "messages": [{"role": "user", "content": "Echo this verbatim: $in.in"}],
                },
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_a"),
            _edge("e2", "n_a", "ok", "n_b"),
            _edge("e3", "n_b", "ok", "n_out"),
        ],
        models={
            "default": {"binding": "mlx", "model": _MODEL, "params": {"maxTokens": 64}},
        },
    )
    ir["id"] = "agt_durable_integration"
    return ir


@_skip
async def test_real_multistep_agent_runs_durably_on_mlx() -> None:
    assert _DATABASE_URL and _INFERENCE_BASE_URL
    # Alembic's async env.py calls asyncio.run() internally; run the sync upgrade OFF this running
    # event loop (pytest-asyncio) so the two loops don't collide.
    await asyncio.to_thread(_prepare_db, _DATABASE_URL)

    from theygent_control_plane import db
    from theygent_control_plane.durable.runtime import DurableRuntime
    from theygent_control_plane.mcp import McpManager
    from theygent_control_plane.store import AgentStore, RunStore, TriggerStore
    from theygent_gateway_client import GatewayClient

    # Confirm the inference plane is actually serving the logical id (else skip clean).
    async with httpx.AsyncClient(base_url=_INFERENCE_BASE_URL, timeout=10) as probe:
        try:
            models = (await probe.get("/models")).json()
        except Exception as exc:  # pragma: no cover
            pytest.skip(f"inference plane unreachable: {exc}")
    ids = {m["id"] for m in models.get("data", [])}
    if _MODEL not in ids:
        pytest.skip(f"logical id {_MODEL!r} not served (have {sorted(ids)})")

    from _durable import save_agent

    engine = db.create_engine(_DATABASE_URL)
    sm = db.create_sessionmaker(engine)
    agents = AgentStore()
    aid, ver = await save_agent(sm, agents, _two_step_ir())
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
    rt.launch()
    try:
        handle = await rt.enqueue_run(
            {"agent_id": aid, "version": ver, "content_hash": None}, "blue"
        )
        result = await handle.get_result()
        assert result["status"] == "completed", result
        assert result["output"].strip()  # a real MLX answer came back through the durable workflow
        async with sm() as s:
            row = await RunStore().get_run(s, result["runId"])
        assert row.status == "completed" and row.completed_at is not None
    finally:
        rt.shutdown()
        await gw.aclose()
        await engine.dispose()
