"""Observability on the DURABLE path (DBOS) — worker attribution + queue.wait + the crash-resume
worker-hop, the user's "see which worker handled what" ask made concrete.

Reuses the kill-and-resume harness (``_build_runtime`` auto-wires a Telemetry, so durable spans
land through the SAME wrapper the interactive walker uses). DBOS is process-global, so each test
resets the ``dbos`` schema and the autouse teardown in this module force-destroys it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from _durable import BlockingInference, reset_dbos_schema, save_agent
from _fake_inference import FakeInference
from _ir import _doc, _edge, _node, trivial_ir
from theygent_control_plane import db
from theygent_control_plane.durable.runtime import DurableRuntime
from theygent_control_plane.mcp import McpManager
from theygent_control_plane.observability import TraceStore, now_ns
from theygent_control_plane.store import AgentStore, RunStore, TriggerStore


def _set_executor(executor_id: str) -> None:
    # The DBOS executor id is read once at import into GlobalParams (so DBOS__VMID env set at test
    # time is too late). Patch it directly to simulate a NAMED worker — in production each worker
    # process imports dbos fresh with its own DBOS__VMID, so DBOS.executor_id is the real worker id;
    # here we set it to demo the crash-resume worker hop within one test process.
    from dbos._utils import GlobalParams

    GlobalParams.executor_id = executor_id


@pytest.fixture(autouse=True)
def _ensure_dbos_destroyed() -> Iterator[None]:
    yield
    with contextlib.suppress(Exception):
        from dbos import DBOS

        DBOS.destroy(destroy_registry=False)
    with contextlib.suppress(Exception):
        _set_executor("local")  # restore the default so other durable tests are unaffected


def _build_runtime(
    pg_url: str, fake_url: str, agents: AgentStore, sm
) -> tuple[DurableRuntime, object]:
    from theygent_gateway_client import GatewayClient

    gw = GatewayClient(fake_url, max_retries=0)
    rt = DurableRuntime(
        database_url=pg_url,
        gateway=gw,
        mcp=McpManager(),
        store=RunStore(),
        agents=agents,
        triggers=TriggerStore(),
        sessionmaker=sm,
        fast_polling=True,
    )
    return rt, gw


def _two_step_ir(agent_id: str) -> dict:
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_a",
                "llm",
                "activity",
                config={"model": "default", "messages": [{"role": "user", "content": "$in"}]},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_b",
                "llm",
                "activity",
                config={"model": "default", "messages": [{"role": "user", "content": "$in.in"}]},
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
        models={"default": {"binding": "mlx", "model": "triage-fast", "params": {}}},
    )
    ir["id"] = agent_id
    return ir


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.02)


async def test_durable_run_records_worker_attribution_and_queue_wait(pg_url: str) -> None:
    # A durable fire writes one span per node, each stamped with the DBOS executor that handled it
    # (worker attribution), plus a queue.wait phase span (enqueue → worker pickup).
    await reset_dbos_schema(pg_url)
    _set_executor("worker-1")
    ir = trivial_ir()
    ir["id"] = "agt_obs_durable"
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, sm)
        rt.launch()
        try:
            # An agent_ref carrying enqueued_ns (as the fire() seam stamps) → a queue.wait span.
            ref = {
                "agent_id": aid,
                "version": ver,
                "content_hash": None,
                "enqueued_ns": now_ns() - 5_000_000,  # 5ms "in the queue" before pickup
            }
            handle = await rt.enqueue_run(ref, "hi")
            result = await handle.get_result()
            assert result["status"] == "completed"
            async with sm() as s:
                spans = await TraceStore().list_spans(s, result["runId"])
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()

    by_name = {s.name: s for s in spans}
    assert "n_llm" in by_name  # node spans landed on the durable path
    for s in spans:
        assert s.executor_id == "worker-1"  # the DBOS executor that handled it (attribution)
        assert s.worker_host  # host:pid
    queue_wait = next((s for s in spans if s.phase == "queue_wait"), None)
    assert queue_wait is not None and queue_wait.end_ns >= queue_wait.start_ns
    # The streamed token usage the llm step journals lands on the model.generate span on the
    # DURABLE path too — the quota gate reads it back regardless of which runtime ran the agent.
    gen = next(s for s in spans if s.phase == "model.generate")
    gen_attrs = gen.attributes or {}
    assert gen_attrs["gen_ai.usage.total_tokens"] == 3
    assert gen_attrs["gen_ai.usage.input_tokens"] == 1
    assert gen_attrs["gen_ai.usage.output_tokens"] == 2


def _model_guardrail_ir(agent_id: str) -> dict:
    """input -> guardrail(model judge) -> output: the pass branch carries the input through."""
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_g",
                "guardrail",
                "activity",
                config={
                    "check": {
                        "type": "model",
                        "model": {
                            "model": "default",
                            "prompt": "in scope? yes/no",
                            "passOn": "yes",
                        },
                    },
                    "onBlock": {"message": "blocked"},
                },
                ins=["in"],
                outs=["pass", "block"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_g"), _edge("e2", "n_g", "pass", "n_out")],
        models={"default": {"binding": "mlx", "model": "judge-fast", "params": {}}},
    )
    ir["id"] = agent_id
    return ir


async def test_durable_guardrail_usage_lands_on_generate_span(pg_url: str) -> None:
    # The judge call's journaled usage lands on the guardrail's model.generate phase span on the
    # DURABLE path too — the quota gate meters guardrail calls regardless of which runtime ran
    # the agent. Exactly ONE span carries it (no mirror onto the node span — the gate sums).
    await reset_dbos_schema(pg_url)
    ir = _model_guardrail_ir("agt_obs_guard")
    with FakeInference(response="yes, in scope") as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, sm)
        rt.launch()
        try:
            ref = {"agent_id": aid, "version": ver, "content_hash": None}
            handle = await rt.enqueue_run(ref, "please book a table")
            result = await handle.get_result()
            assert result["status"] == "completed"
            assert result["output"] == "please book a table"  # passed → carried through
            async with sm() as s:
                spans = await TraceStore().list_spans(s, result["runId"])
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()

    gen = next(s for s in spans if s.phase == "model.generate")
    attrs = gen.attributes or {}
    assert attrs["gen_ai.usage.total_tokens"] == 3
    assert attrs["gen_ai.usage.input_tokens"] == 1
    assert attrs["gen_ai.usage.output_tokens"] == 2
    node = next(s for s in spans if s.name == "n_g" and s.phase is None)
    assert "gen_ai.usage.total_tokens" not in (node.attributes or {})  # no double-count mirror


async def test_resume_does_not_duplicate_spans_and_preserves_worker(pg_url: str) -> None:
    # The resume-idempotency invariant, on the real durable path: n_a COMPLETES + journals; the
    # worker CRASHES mid-n_b; the run is recovered and finishes n_b. The recovered workflow
    # re-executes the walk body — re-opening n_a's span — but the deterministic-id ON CONFLICT DO
    # NOTHING means n_a's row is NOT duplicated and KEEPS the first-completing worker. Both
    # nodes carry worker attribution. (DBOS recovery is executor-scoped, so the SAME executor id is
    # kept across launches here; the TRUE distinct-worker hop is the store-level core test
    # `test_reemit_is_idempotent_first_writer_wins` + the two-process integration test.)
    await reset_dbos_schema(pg_url)
    _set_executor("local")
    ir = _two_step_ir("agt_obs_resume")
    responses = {"GO": "OUT1", "OUT1": "FINAL"}
    with BlockingInference(responses, block_prompt="OUT1") as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        ref = {"agent_id": aid, "version": ver, "content_hash": None}

        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, sm)
        rt.launch()
        handle = await rt.enqueue_run(ref, "GO")
        wid = handle.workflow_id
        for _ in range(1000):  # wait until n_b blocks → n_a has completed + journaled
            if fake.blocked_entered.is_set():
                break
            await _sleep()
        assert fake.blocked_entered.is_set()

        rt.shutdown()  # CRASH mid-n_b

        from dbos import DBOS

        rt2, gw2 = _build_runtime(pg_url, fake.v1_url, agents, sm)
        rt2.launch()  # recovers the pending workflow and resumes at n_b
        try:
            rh = await DBOS.retrieve_workflow_async(wid)
            res = await rh.get_result()
            assert res["status"] == "completed" and res["output"] == "FINAL"
            async with sm() as s:
                spans = await TraceStore().list_spans(s, wid)
        finally:
            rt2.shutdown()
            await gw.aclose()
            await gw2.aclose()
            await engine.dispose()

    # No duplicate span rows after resume — the deterministic-id ON CONFLICT DO NOTHING.
    node_spans = [s for s in spans if s.node_id and s.phase is None]
    names = [s.name for s in node_spans]
    assert len(names) == len(set(names)), f"duplicate node spans after resume: {names}"
    by_name = {s.name: s for s in node_spans}
    assert "n_a" in by_name and "n_b" in by_name  # both completed nodes' spans landed
    assert by_name["n_a"].status == "ok"  # n_a's pre-crash span preserved (not clobbered on replay)
    for s in node_spans:  # worker attribution stamped on every span
        assert s.executor_id and s.worker_host
