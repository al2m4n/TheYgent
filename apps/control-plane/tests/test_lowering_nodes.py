"""Fast suite for the additive-lowering tier (``human`` · ``subgraph`` · ``loop`` · ``map``),
run with DBOS embedded, against a REAL ephemeral Postgres (Alembic ``public`` + DBOS ``dbos``).

These four node types need NO new subsystem — each is a new handler + a new lowering onto a DBOS
primitive already built (recv/send, child workflows, durable queue). So these prove, per type, the
headline DURABLE behavior the IR was designed for:

* ``human``   — a run pauses at a durable wait (status ``waiting``, NOT swept by the reconcile
                sweep), survives a
                worker crash, and resumes from the checkpoint when input is delivered.
* ``subgraph``— an agent runs another SAVED, PINNED agent as a child workflow; the pin is immutable
                (runs the pinned content even after the child publishes a newer version);
                ``maxDepth`` exceeded fails honestly; a crash mid-child resumes, parent collects.
* ``loop``    — bounded, deterministic repetition; a crash mid-loop re-runs NO completed iteration;
                an unbounded loop (no ``maxIterations``) is rejected at compile.
* ``map``     — durable fan-out/join; a crash after k of N complete re-runs only N-k; ``fail_fast``
                vs ``collect`` behave per config.

Plus: saved agents using the new types round-trip through the registry unchanged (``contentHash``
stable). DBOS is process-global: each test resets the ``dbos`` schema and an autouse teardown
force-destroys DBOS so one failure can't wedge the next.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import httpx
import pytest
from _durable import BlockingInference, canonical_ir, reset_dbos_schema, save_agent
from _fake_inference import FakeInference
from _ir import _doc, _edge, _node, trivial_ir
from fastapi.testclient import TestClient
from theygent_control_plane import db
from theygent_control_plane.app import create_app
from theygent_control_plane.store import AgentStore, RunStore, TriggerStore
from theygent_ir import GraphValidationError, content_hash, parse_document, validate_graph

TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _ensure_dbos_destroyed() -> Iterator[None]:
    yield
    with contextlib.suppress(Exception):
        from dbos import DBOS

        DBOS.destroy(destroy_registry=False)


def _build_runtime(pg_url: str, fake_url: str, agents: AgentStore, triggers: TriggerStore, sm):
    from theygent_control_plane.durable.runtime import DurableRuntime
    from theygent_control_plane.mcp import McpManager
    from theygent_gateway_client import GatewayClient

    gw = GatewayClient(fake_url, max_retries=0)
    rt = DurableRuntime(
        database_url=pg_url,
        gateway=gw,
        mcp=McpManager(),
        store=RunStore(),
        agents=agents,
        triggers=triggers,
        sessionmaker=sm,
        fast_polling=True,
    )
    return rt, gw


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.02)


async def _wait_status(sm, run_id: str, status: str, *, tries: int = 1000):
    for _ in range(tries):
        async with sm() as s:
            run = await RunStore().get_run(s, run_id)
        if run is not None and run.status == status:
            return run
        await _sleep()
    raise AssertionError(f"run {run_id} never reached status {status!r}")


async def _add_version(sm, agents: AgentStore, ir_dict: dict) -> str:
    """Publish another immutable version of an existing agent; returns its contentHash."""
    doc, chash, canon = canonical_ir(ir_dict)
    async with sm() as session, session.begin():
        await agents.add_version(
            session, agent_id=doc.id, version=doc.version, content_hash=chash, ir=canon, view=None
        )
    return chash


# ── IR builders for the four new node types (no IR shape change — config only) ───


def _llm_body(agent_id: str, prompt: str = "$in") -> dict:
    """A trivial body agent: input → llm(<prompt>) → output. Used as a subgraph/loop/map body."""
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
        models={"default": {"binding": "mlx", "model": "triage-fast", "params": {}}},
    )
    ir["id"] = agent_id
    return ir


def _human_ir(agent_id: str, **config) -> dict:
    """input → human → output. The human's awaited input becomes the run output.
    ``config`` overrides the node config (timeout/on_timeout/default/inputSchema)."""
    cfg = {"prompt": "ok?", **config}
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node("n_h", "human", "boundary", config=cfg, ins=["in"], outs=["ok"]),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_h"), _edge("e2", "n_h", "ok", "n_out")],
    )
    ir["id"] = agent_id
    return ir


def _subgraph_ir(agent_id: str, child_id: str, *, content_hash_pin=None, version=None, max_depth=8):
    """input → subgraph(child, pin) → output."""
    cfg: dict = {"agent": child_id, "maxDepth": max_depth}
    if content_hash_pin is not None:
        cfg["contentHash"] = content_hash_pin
    if version is not None:
        cfg["version"] = version
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node("n_sg", "subgraph", "boundary", config=cfg, ins=["in"], outs=["ok", "err"]),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_sg"), _edge("e2", "n_sg", "ok", "n_out")],
    )
    ir["id"] = agent_id
    return ir


def _loop_ir(agent_id: str, body_id: str, *, version: str, max_iterations: int, condition=None):
    """input → loop(body, pin, maxIterations[, condition]) → output."""
    cfg: dict = {"agent": body_id, "version": version, "maxIterations": max_iterations}
    if condition is not None:
        cfg["condition"] = condition
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node("n_loop", "loop", "orchestration", config=cfg, ins=["in"], outs=["ok", "err"]),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_loop"), _edge("e2", "n_loop", "ok", "n_out")],
    )
    ir["id"] = agent_id
    return ir


def _map_ir(agent_id: str, body_id: str, *, version: str, on_error="fail_fast", concurrency=None):
    """input → map(body, pin, onError, concurrency) → output."""
    cfg: dict = {"agent": body_id, "version": version, "onError": on_error}
    if concurrency is not None:
        cfg["concurrency"] = concurrency
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node("n_map", "map", "orchestration", config=cfg, ins=["in"], outs=["ok", "err"]),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_map"), _edge("e2", "n_map", "ok", "n_out")],
    )
    ir["id"] = agent_id
    return ir


def _router_body(agent_id: str) -> dict:
    """A body that SUCCEEDS when its input selects a declared handle and FAILS (RouterError) when it
    doesn't — so a map element can be made to fail deterministically for the fail_fast/collect test.
    input → router(select $in.in.handle, outs [ok]) → output."""
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_route",
                "router",
                "orchestration",
                config={"select": "$in.in.handle"},
                ins=["in"],
                outs=["ok"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_route"), _edge("e2", "n_route", "ok", "n_out")],
    )
    ir["id"] = agent_id
    return ir


# ── human: durable wait, survive a crash, resume from the checkpoint (the headline) ──


async def test_human_waits_survives_crash_and_resumes(pg_url: str) -> None:
    await reset_dbos_schema(pg_url)
    with FakeInference() as fake:  # gateway required by the runtime; this graph has no llm node
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, _human_ir("agt_human"))
        ref = {"agent_id": aid, "version": ver, "content_hash": None}

        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        handle = await rt.enqueue_run(ref, "context")
        wid = handle.workflow_id

        # The run pauses at the human node: status `waiting`, with the awaiting node recorded.
        run = await _wait_status(sm, wid, "waiting")
        assert run.awaiting_node == "n_h"

        # A `waiting` run is NOT swept by the reconciliation sweep — the whole point
        # of the durable wait. Run the sweep explicitly and assert the wait survives untouched.
        async with sm() as s, s.begin():
            await RunStore().reconcile_orphaned_runs(s)
        async with sm() as s:
            still = await RunStore().get_run(s, wid)
        assert still.status == "waiting", "the reconcile sweep must not touch a waiting run"

        rt.shutdown()  # CRASH: destroy DBOS while the workflow is parked at recv

        from dbos import DBOS

        rt2, gw2 = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt2.launch()  # recovers the pending workflow → it re-enters the durable wait
        try:
            await rt2.resume(wid, "APPROVED")  # deliver the awaited input (→ DBOS.send)
            rh = await DBOS.retrieve_workflow_async(wid)
            res = await rh.get_result()
            assert res["status"] == "completed"
            assert res["output"] == "APPROVED"  # the human's input became the run output
            async with sm() as s:
                row = await RunStore().get_run(s, wid)
            assert row.status == "completed" and row.awaiting_node is None
        finally:
            rt2.shutdown()
            await gw.aclose()
            await gw2.aclose()
            await engine.dispose()


# ── subgraph: composition + immutable pin + maxDepth ─────────────────────────────


async def test_subgraph_runs_pinned_child_even_after_newer_version(pg_url: str) -> None:
    # The pin is frozen into the parent IR: the parent runs the PINNED child content
    # even after the child agent publishes a newer (latest) version. Distinct per-version prompts +
    # a prompt-keyed fake make "which version actually ran" observable.
    await reset_dbos_schema(pg_url)
    v1 = _llm_body("agt_child", prompt="A: $in")
    _, v1_hash, _ = canonical_ir(v1)
    v2 = _llm_body("agt_child", prompt="B: $in")
    v2["version"] = "0.2.0"
    responses = {"A: GO": "FROM_V1", "B: GO": "FROM_V2"}
    with BlockingInference(responses) as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        await save_agent(sm, agents, v1)  # child v0.1.0
        await _add_version(sm, agents, v2)  # child v0.2.0 (now the LATEST)
        parent = _subgraph_ir("agt_parent", "agt_child", content_hash_pin=v1_hash)
        paid, pver = await save_agent(sm, agents, parent)

        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": paid, "version": pver, "content_hash": None}, "GO"
            )
            res = await handle.get_result()
            assert res["status"] == "completed"
            # The PINNED v1 ran (prompt "A: …" → FROM_V1), NOT latest v2 — composition is immutable.
            assert res["output"] == "FROM_V1"
            assert "B: GO" not in fake.calls  # v2 was never invoked
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


async def test_subgraph_max_depth_exceeded_fails_honestly(pg_url: str) -> None:
    # maxDepth bounds composition. With maxDepth=0 the parent may spawn NO child, so
    # the guard trips at the top and the run fails honestly with a named reason — never a hang.
    await reset_dbos_schema(pg_url)
    child = _llm_body("agt_d_child")
    _, child_hash, _ = canonical_ir(child)
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        await save_agent(sm, agents, child)
        parent = _subgraph_ir(
            "agt_d_parent", "agt_d_child", content_hash_pin=child_hash, max_depth=0
        )
        paid, pver = await save_agent(sm, agents, parent)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": paid, "version": pver, "content_hash": None}, "x"
            )
            res = await handle.get_result()
            assert res["status"] == "failed"
            assert "maxDepth" in res["error"]
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


async def test_subgraph_child_resumes_after_crash(pg_url: str) -> None:
    # A crash mid-child resumes the child and the parent collects its result. Block the
    # child's llm; crash; restart → DBOS recovers BOTH parent and child workflow (deterministic
    # child id), the child's interrupted step re-runs, the parent awaits and completes.
    await reset_dbos_schema(pg_url)
    child = _llm_body("agt_rc_child")  # prompt "$in"
    _, child_hash, _ = canonical_ir(child)
    with BlockingInference({"GO": "CHILD_OUT"}, block_prompt="GO") as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        await save_agent(sm, agents, child)
        parent = _subgraph_ir("agt_rc_parent", "agt_rc_child", content_hash_pin=child_hash)
        paid, pver = await save_agent(sm, agents, parent)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        handle = await rt.enqueue_run(
            {"agent_id": paid, "version": pver, "content_hash": None}, "GO"
        )
        wid = handle.workflow_id
        for _ in range(1000):
            if fake.blocked_entered.is_set():
                break
            await _sleep()
        assert fake.blocked_entered.is_set()  # the child's llm is in-flight

        rt.shutdown()  # CRASH mid-child

        from dbos import DBOS

        rt2, gw2 = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt2.launch()
        try:
            rh = await DBOS.retrieve_workflow_async(wid)
            res = await rh.get_result()
            assert res["status"] == "completed"
            assert res["output"] == "CHILD_OUT"
            assert fake.calls["GO"] == 2  # interrupted attempt + recovered attempt
        finally:
            rt2.shutdown()
            await gw.aclose()
            await gw2.aclose()
            await engine.dispose()


# ── loop: bounded + deterministic resume (no completed iteration re-runs) ────────


async def test_loop_runs_to_max_iterations_and_resumes_without_rerun(pg_url: str) -> None:
    # A 3-iteration loop feeds each output into the next input. Block iteration 1's body; crash
    # after iteration 0 completed (journaled); restart → iteration 0 does NOT re-run (its child
    # replays from the journal), iterations 1+2 run. The chained outputs prove ordering; the call
    # counts prove the exactly-once-for-completed guarantee at the loop granularity.
    await reset_dbos_schema(pg_url)
    body = _llm_body("agt_loop_body")  # prompt "$in"
    bid, bver = None, "0.1.0"
    responses = {"GO": "S1", "S1": "S2", "S2": "S3"}
    with BlockingInference(responses, block_prompt="S1") as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        bid, bver = await save_agent(sm, agents, body)
        loop = _loop_ir("agt_loop", bid, version=bver, max_iterations=3)
        laid, lver = await save_agent(sm, agents, loop)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        handle = await rt.enqueue_run(
            {"agent_id": laid, "version": lver, "content_hash": None}, "GO"
        )
        wid = handle.workflow_id
        for _ in range(1000):
            if fake.blocked_entered.is_set():
                break
            await _sleep()
        assert fake.blocked_entered.is_set()
        assert fake.calls.get("GO") == 1  # iteration 0 ran exactly once

        rt.shutdown()  # CRASH mid-iteration-1

        from dbos import DBOS

        rt2, gw2 = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt2.launch()
        try:
            rh = await DBOS.retrieve_workflow_async(wid)
            res = await rh.get_result()
            assert res["status"] == "completed"
            assert res["output"] == "S3"  # the last iteration's output (3 iterations chained)
            assert fake.calls["GO"] == 1, "completed iteration 0 re-ran on resume!"
            assert fake.calls["S1"] == 2  # iteration 1: interrupted + recovered
            assert fake.calls["S2"] == 1  # iteration 2 ran once
        finally:
            rt2.shutdown()
            await gw.aclose()
            await gw2.aclose()
            await engine.dispose()


async def test_loop_condition_stops_early(pg_url: str) -> None:
    # A condition over the iteration output stops the loop BEFORE maxIterations. The
    # body emits an object {"stop": true}; condition "$in.in.stop" resolves truthy after iter 0,
    # so exactly ONE body iteration runs (counted via child run rows) despite maxIterations=5.
    await reset_dbos_schema(pg_url)
    body = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_echo",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": {"stop": True}}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_echo"), _edge("e2", "n_echo", "ok", "n_out")],
    )
    body["id"] = "agt_cond_body"
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        bid, bver = await save_agent(sm, agents, body)
        loop = _loop_ir("agt_cond", bid, version=bver, max_iterations=5, condition="$in.in.stop")
        laid, lver = await save_agent(sm, agents, loop)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": laid, "version": lver, "content_hash": None}, "GO"
            )
            res = await handle.get_result()
            assert res["status"] == "completed"
            async with sm() as s:
                runs = await RunStore().list_runs(s, limit=50)
            iterations = [r for r in runs if r.graph_id == bid]
            assert len(iterations) == 1, "condition should stop the loop after one iteration, not 5"
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


def test_unbounded_loop_rejected_at_compile() -> None:
    # No maxIterations → rejected at validation (compile), never a runtime surprise.
    bad = _loop_ir("agt_unbounded", "agt_body", version="0.1.0", max_iterations=1)
    del bad["nodes"][1]["config"]["maxIterations"]
    with pytest.raises(GraphValidationError):
        validate_graph(parse_document(bad))
    # Zero iterations is likewise rejected (a loop runs at least once).
    zero = _loop_ir("agt_zero", "agt_body", version="0.1.0", max_iterations=0)
    with pytest.raises(GraphValidationError):
        validate_graph(parse_document(zero))


def test_non_positive_max_depth_rejected_at_compile() -> None:
    # maxDepth 0 (or negative) would otherwise only trip the durable depth guard at run time —
    # it is rejected at validation instead, for every pinned-body type.
    zero = _subgraph_ir("agt_depth0", "agt_child", content_hash_pin="deadbeef", max_depth=0)
    with pytest.raises(GraphValidationError, match="maxDepth"):
        validate_graph(parse_document(zero))
    neg = _loop_ir("agt_depthneg", "agt_body", version="0.1.0", max_iterations=2)
    neg["nodes"][1]["config"]["maxDepth"] = -1
    with pytest.raises(GraphValidationError, match="maxDepth"):
        validate_graph(parse_document(neg))


# ── map: durable fan-out/join + partial resume + partial-failure policy ──────────


async def test_map_fans_out_and_resumes_only_incomplete_branches(pg_url: str) -> None:
    # Fan out over 3 elements; block "b"; crash after "a" and "c" complete; restart → only the
    # incomplete branch ("b") re-runs, results join in element order.
    await reset_dbos_schema(pg_url)
    body = _llm_body("agt_map_body")  # prompt "$in"
    responses = {"a": "RA", "b": "RB", "c": "RC"}
    with BlockingInference(responses, block_prompt="b") as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        bid, bver = await save_agent(sm, agents, body)
        mp = _map_ir("agt_map", bid, version=bver)
        maid, mver = await save_agent(sm, agents, mp)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        from dbos import DBOS

        handle = await rt.enqueue_run(
            {"agent_id": maid, "version": mver, "content_hash": None}, ["a", "b", "c"]
        )
        wid = handle.workflow_id

        async def _child_journaled(index: int) -> bool:
            # fake.calls only proves the HTTP request landed, not that DBOS durably journaled the
            # step outcome — under xdist's added CPU/IO contention that gap can outlive this poll
            # loop, so branch "a"/"c" would legitimately (per the at-least-once guarantee) re-run
            # on resume. Poll the child workflow's OWN persisted status instead — the true signal.
            status = await DBOS.get_workflow_status_async(f"{wid}-map-n_map-{index}")
            return status is not None and status.status == "SUCCESS"

        for _ in range(1000):
            if (
                fake.blocked_entered.is_set()
                and await _child_journaled(0)
                and await _child_journaled(2)
            ):
                break
            await _sleep()
        assert fake.blocked_entered.is_set()
        assert await _child_journaled(0) and await _child_journaled(2)  # a, c durably journaled
        assert fake.calls.get("a") == 1 and fake.calls.get("c") == 1  # a, c completed once

        rt.shutdown()  # CRASH after a + c are durably journaled, mid-b

        rt2, gw2 = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt2.launch()
        try:
            rh = await DBOS.retrieve_workflow_async(wid)
            res = await rh.get_result()
            assert res["status"] == "completed"
            # Results join in element order; only "b" re-ran (a, c replayed from the journal).
            assert res["output"] == '["RA", "RB", "RC"]'
            assert fake.calls["a"] == 1, "completed branch a re-ran on resume!"
            assert fake.calls["c"] == 1, "completed branch c re-ran on resume!"
            assert fake.calls["b"] == 2  # interrupted + recovered
        finally:
            rt2.shutdown()
            await gw.aclose()
            await gw2.aclose()
            await engine.dispose()


async def test_map_fail_fast_vs_collect(pg_url: str) -> None:
    # An element fails when its object selects an undeclared router handle. fail_fast → map fails;
    # collect → the map completes, gathering successes + per-element errors.
    await reset_dbos_schema(pg_url)
    body = _router_body("agt_mf_body")
    elements = [{"handle": "ok"}, {"handle": "nope"}]  # element 1 selects a handle the body lacks
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        bid, bver = await save_agent(sm, agents, body)
        # fail_fast → run fails
        ff = _map_ir("agt_mf_ff", bid, version=bver, on_error="fail_fast")
        ffid, ffver = await save_agent(sm, agents, ff)
        # collect → run completes with per-element results
        col = _map_ir("agt_mf_col", bid, version=bver, on_error="collect")
        colid, colver = await save_agent(sm, agents, col)

        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            ff_res = await (
                await rt.enqueue_run(
                    {"agent_id": ffid, "version": ffver, "content_hash": None}, elements
                )
            ).get_result()
            assert ff_res["status"] == "failed"
            assert "element 1 failed" in ff_res["error"]

            col_res = await (
                await rt.enqueue_run(
                    {"agent_id": colid, "version": colver, "content_hash": None}, elements
                )
            ).get_result()
            assert col_res["status"] == "completed"
            import json

            collected = json.loads(col_res["output"])
            assert collected[0]["status"] == "completed"
            assert collected[1]["status"] == "failed" and collected[1]["index"] == 1
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── observability: loop/map emit a span per iteration/branch ─────────────────────


class _BranchCapture:
    """Capture ``durable.branch`` log records (the per-iteration/branch span attach-point) across
    the DBOS worker threads that run the workflow."""

    def __init__(self) -> None:
        import logging

        self.spans: list[str] = []
        self._lg = logging.getLogger("theygent.control_plane.durable")
        self._prev_level = self._lg.level

        class _H(logging.Handler):
            def emit(_self, record: logging.LogRecord) -> None:
                if record.getMessage() == "durable.branch":
                    self.spans.append(record.span_name)  # type: ignore[attr-defined]

        self._handler = _H()

    def __enter__(self) -> _BranchCapture:
        import logging

        # The durable logger inherits the root WARNING level by default, which would drop the INFO
        # span records before they reach the handler — lower it for the capture, then restore.
        self._lg.setLevel(logging.INFO)
        self._lg.addHandler(self._handler)
        return self

    def __exit__(self, *exc: object) -> None:
        self._lg.removeHandler(self._handler)
        self._lg.setLevel(self._prev_level)


async def test_loop_and_map_emit_per_branch_spans(pg_url: str) -> None:
    # Each loop iteration and each map branch emits a span named ``<node_id>#<i>`` so a trace
    # reads against the drawn graph. (The real OTLP exporter is deferred; this asserts the
    # attach-point seam and that each branch is also a DBOS span.)
    await reset_dbos_schema(pg_url)
    body = _llm_body("agt_span_body")  # prompt "$in"
    responses = {"a": "RA", "b": "RB", "c": "RC", "GO": "L1", "L1": "L2"}
    with _BranchCapture() as cap, BlockingInference(responses) as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        bid, bver = await save_agent(sm, agents, body)
        maid, mver = await save_agent(sm, agents, _map_ir("agt_span_map", bid, version=bver))
        laid, lver = await save_agent(
            sm, agents, _loop_ir("agt_span_loop", bid, version=bver, max_iterations=2)
        )
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            await (
                await rt.enqueue_run(
                    {"agent_id": maid, "version": mver, "content_hash": None}, ["a", "b", "c"]
                )
            ).get_result()
            await (
                await rt.enqueue_run(
                    {"agent_id": laid, "version": lver, "content_hash": None}, "GO"
                )
            ).get_result()
            spans = set(cap.spans)
            assert {"n_map#0", "n_map#1", "n_map#2"} <= spans  # one span per fan-out branch
            assert {"n_loop#0", "n_loop#1"} <= spans  # one span per iteration
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── determinism guard: orchestration control does no I/O, replay re-runs nothing ─


async def test_orchestration_control_does_no_io_and_replay_reruns_nothing(pg_url: str) -> None:
    # Determinism guard: the loop/map control path (fan-out, counter, condition, join) does NO
    # I/O — all inference lives in CHILD workflows — so the parent map workflow has zero activity
    # steps. And a journaled result is not re-executed on replay: re-retrieving the completed
    # workflow runs no further inference. (This is the invariant the whole orchestration kind rests
    # on, asserted directly rather than inferred from the resume tests.)
    await reset_dbos_schema(pg_url)
    body = _llm_body("agt_det_body")
    with BlockingInference({"a": "RA", "b": "RB"}) as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        bid, bver = await save_agent(sm, agents, body)
        maid, mver = await save_agent(sm, agents, _map_ir("agt_det_map", bid, version=bver))
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            from dbos import DBOS

            handle = await rt.enqueue_run(
                {"agent_id": maid, "version": mver, "content_hash": None}, ["a", "b"]
            )
            wid = handle.workflow_id
            assert (await handle.get_result())["status"] == "completed"
            steps = await DBOS.list_workflow_steps_async(wid)
            names = [s["function_name"] for s in steps]
            # The orchestration control did NO I/O: the parent map workflow has NO activity steps
            # (inference ran in the child workflows, journaled there).
            assert not any(
                any(act in n for act in ("_llm_step", "_tool_step", "_mcp_step")) for n in names
            ), names
            # Replay re-runs nothing: re-retrieving the completed result adds no inference calls.
            before = dict(fake.calls)
            await (await DBOS.retrieve_workflow_async(wid)).get_result()
            assert fake.calls == before
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── human timeout: honest fail OR declared default ───────────────────────────────


async def test_human_timeout_fails_or_takes_default(pg_url: str) -> None:
    await reset_dbos_schema(pg_url)
    fail_ir = _human_ir("agt_to_fail", timeout=0.4, on_timeout="fail")
    def_ir = _human_ir("agt_to_def", timeout=0.4, on_timeout="default", default="DEFAULTED")
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        fid, fver = await save_agent(sm, agents, fail_ir)
        did, dver = await save_agent(sm, agents, def_ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            # No resume arrives within the timeout → on_timeout=fail → honest failed run.
            r_fail = await (
                await rt.enqueue_run({"agent_id": fid, "version": fver, "content_hash": None}, "x")
            ).get_result()
            assert r_fail["status"] == "failed" and "no input within" in r_fail["error"]
            # on_timeout=default → completed, binding the declared default.
            r_def = await (
                await rt.enqueue_run({"agent_id": did, "version": dver, "content_hash": None}, "x")
            ).get_result()
            assert r_def["status"] == "completed" and r_def["output"] == "DEFAULTED"
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── subgraph: genuine (mutual) recursion is bounded by maxDepth ──────────────────


async def test_subgraph_mutual_recursion_bounded_by_max_depth(pg_url: str) -> None:
    # The case that bites: two agents that each call the other (A→B→A→…), pinned by version. Real
    # nested expansion proceeds until the depth bound trips — NOT a trivial maxDepth=0 reject. The
    # deepest run fails honestly with a maxDepth reason, proving mutual recursion is bounded.
    await reset_dbos_schema(pg_url)
    a = _subgraph_ir("agt_A", "agt_B", version="0.1.0", max_depth=3)
    b = _subgraph_ir("agt_B", "agt_A", version="0.1.0", max_depth=3)
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        await save_agent(sm, agents, a)
        await save_agent(sm, agents, b)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            await (
                await rt.enqueue_run(
                    {"agent_id": "agt_A", "version": "0.1.0", "content_hash": None}, "x"
                )
            ).get_result()
            async with sm() as s:
                runs = await RunStore().list_runs(s, limit=50)
            # The expansion really nested (runs at multiple depths) and the bound tripped honestly.
            assert any(r.status == "failed" and "maxDepth" in (r.error or "") for r in runs)
            assert len(runs) >= 3, "recursion did not actually nest"
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── subgraph: parent→child data mapping via named ports ──────────────────────────


async def test_subgraph_multi_port_input_mapping(pg_url: str) -> None:
    # A subgraph node with TWO in-ports maps to the child as a {port: value} object the child drills
    # with $in.in.<port> — the named-port mapping at the composition seam. echo tools feed distinct
    # values into ports a and b.
    await reset_dbos_schema(pg_url)
    child = _llm_body("agt_mp_child", prompt="$in.in.a-$in.in.b")
    _, child_hash, _ = canonical_ir(child)
    parent = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_a",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "AA"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_b",
                "tool",
                "activity",
                config={"tool": "echo", "args": {"value": "BB"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node(
                "n_sg",
                "subgraph",
                "boundary",
                config={"agent": "agt_mp_child", "contentHash": child_hash},
                ins=["a", "b"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [
            _edge("e1", "n_in", "out", "n_a"),
            _edge("e2", "n_in", "out", "n_b"),
            _edge("e3", "n_a", "ok", "n_sg", "a"),
            _edge("e4", "n_b", "ok", "n_sg", "b"),
            _edge("e5", "n_sg", "ok", "n_out"),
        ],
    )
    parent["id"] = "agt_mp_parent"
    with BlockingInference({"AA-BB": "BOTH"}) as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        await save_agent(sm, agents, child)
        paid, pver = await save_agent(sm, agents, parent)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            res = await (
                await rt.enqueue_run(
                    {"agent_id": paid, "version": pver, "content_hash": None}, "go"
                )
            ).get_result()
            # The child saw BOTH ports as $in.in.a / $in.in.b → rendered "AA-BB" → "BOTH".
            assert res["status"] == "completed" and res["output"] == "BOTH"
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── POST /runs/{id}/resume — the human entry point's guards ──────────────────────


def test_resume_requires_durable_mode(fake_inference, pg_url: str) -> None:
    # Non-durable mode can't durably wait, so resume is an honest 400 (durable_required), not a 500.
    # A valid token gets past the auth gate so we reach the durable check itself.
    app = create_app(
        inference_base_url=fake_inference.v1_url,
        database_url=pg_url,
        invoke_token=TOKEN,
        start_dispatcher=False,
    )
    with TestClient(app) as c:
        r = c.post(
            "/runs/anything/resume",
            json={"input": "x"},
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "durable_required"


async def test_resume_guards_unknown_and_non_waiting(pg_url: str) -> None:
    await reset_dbos_schema(pg_url)
    with FakeInference() as fake:
        app = create_app(
            inference_base_url=fake.v1_url,
            database_url=pg_url,
            invoke_token=TOKEN,
            durable=True,
            dbos_fast_polling=True,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as ac:
                hdr = {"Authorization": f"Bearer {TOKEN}"}
                # Resume is an INTERACTIVE surface (the person answering the gate is the cockpit
                # user, same auth as starting the durable run) — no invoke token required. The
                # token gates only the unattended surfaces (/invoke, /hooks).
                no_token = await ac.post("/runs/nope/resume", json={"input": "x"})
                assert no_token.status_code == 404  # past auth; unknown run
                # Unknown run → 404.
                r404 = await ac.post("/runs/nope/resume", json={"input": "x"}, headers=hdr)
                assert r404.status_code == 404
                # A completed (non-waiting) run → 409: nothing to resume.
                gr = await ac.post(
                    "/graphs/runs", json={"ir": trivial_ir(), "input": "hi", "stream": False}
                )
                rid = gr.json()["runId"]
                r409 = await ac.post(f"/runs/{rid}/resume", json={"input": "x"}, headers=hdr)
                assert r409.status_code == 409
                assert r409.json()["error"]["code"] == "run_not_waiting"


async def test_resume_validates_against_declared_schema(pg_url: str) -> None:
    # The resume payload is validated against the human node's declared input schema. The
    # agent declares inputSchema.required=["approve"]; a waiting run pointed at that node rejects a
    # payload missing the key with a loud 422 (before any DBOS.send), and accepts one carrying it.
    await reset_dbos_schema(pg_url)
    ir = _human_ir("agt_schema", inputSchema={"required": ["approve"]})
    _, chash, _ = canonical_ir(ir)
    with FakeInference() as fake:
        app = create_app(
            inference_base_url=fake.v1_url,
            database_url=pg_url,
            invoke_token=TOKEN,
            durable=True,
            dbos_fast_polling=True,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as ac:
                hdr = {"Authorization": f"Bearer {TOKEN}"}
                assert (await ac.post("/agents", json={"ir": ir})).status_code == 201
                # Hand-create a waiting run row pointing at the human node (the durable workflow's
                # checkpoint is irrelevant to the validation gate, which runs before any send).
                sm = app.state.sessionmaker
                async with sm() as s, s.begin():
                    run = await RunStore().create_run(
                        s,
                        model="",
                        session_id=None,
                        params=None,
                        graph_id="agt_schema",
                        graph_version="0.1.0",
                        content_hash=chash,
                    )
                    await RunStore().mark_waiting(s, run.id, "n_h")
                # Missing the required key → loud 422 (rejected before any DBOS.send). A non-object
                # payload likewise fails the gate. (The satisfied-payload happy path — gate passes,
                # workflow resumes, run completes — is proven end-to-end by the kill/resume test
                # above; here we hand-create the row so there is no live workflow to send to.)
                bad = await ac.post(f"/runs/{run.id}/resume", json={"input": {}}, headers=hdr)
                assert bad.status_code == 422
                assert bad.json()["error"]["code"] == "resume_schema_mismatch"
                bad2 = await ac.post(
                    f"/runs/{run.id}/resume", json={"input": "not-an-object"}, headers=hdr
                )
                assert bad2.status_code == 422


async def test_resume_refuses_waiting_run_without_awaiting_node(pg_url: str) -> None:
    # A waiting run always records its awaiting node (mark_waiting writes both); a row that says
    # waiting but records none cannot be targeted — a bare-topic send would be accepted while
    # nothing ever recvs it. Refused loudly instead.
    await reset_dbos_schema(pg_url)
    with FakeInference() as fake:
        app = create_app(
            inference_base_url=fake.v1_url,
            database_url=pg_url,
            invoke_token=TOKEN,
            durable=True,
            dbos_fast_polling=True,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as ac:
                sm = app.state.sessionmaker
                async with sm() as s, s.begin():
                    run = await RunStore().create_run(
                        s, model="", session_id=None, params=None, graph_id="agt_x"
                    )
                    await RunStore().set_status(s, run.id, "waiting")
                r = await ac.post(f"/runs/{run.id}/resume", json={"input": "x"})
                assert r.status_code == 409
                assert r.json()["error"]["code"] == "awaiting_node_missing"


async def test_rule_guardrail_before_input_preserves_run_input(pg_url: str) -> None:
    # A rule guardrail with an OPTIONAL, unfed in-port has zero indegree, so topo order can visit
    # it BEFORE an input node that sorts after it. The guardrail branch must evaluate its own
    # in-port value under a branch-local name — rebinding the workflow's `input_value` there
    # would corrupt the run input every later input node binds.
    await reset_dbos_schema(pg_url)
    ir = _doc(
        [
            {
                "id": "a_guard",  # sorts BEFORE z_in — runs first
                "type": "guardrail",
                "kind": "orchestration",
                "config": {
                    "check": {"type": "rule", "rule": {"kind": "deny", "spec": {"values": []}}},
                    "onBlock": {"message": "blocked"},
                },
                "ports": {
                    "in": [{"id": "in", "type": "any", "required": False}],
                    "out": [{"id": "pass", "type": "any"}, {"id": "block", "type": "any"}],
                },
            },
            _node("z_in", "input", "boundary", outs=["out"]),
            _node("z_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "z_in", "out", "z_out")],
    )
    ir["id"] = "agt_guard_order"
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, "REAL-INPUT"
            )
            res = await handle.get_result()
            assert res["status"] == "completed"
            assert res["output"] == "REAL-INPUT"  # not the guardrail's None/"" leak
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


async def test_subgraph_failed_child_binds_err_and_flows(pg_url: str) -> None:
    # A failed child binds the subgraph node's error-typed out-port (the tool ok/err contract) —
    # with the `err` port wired to an output, the failure message flows instead of vanishing.
    await reset_dbos_schema(pg_url)
    child = _router_body("agt_err_child")  # fails when input names no declared handle
    _, child_hash, _ = canonical_ir(child)
    parent = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_sg",
                "subgraph",
                "boundary",
                config={"agent": "agt_err_child", "contentHash": child_hash, "maxDepth": 3},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_sg"), _edge("e2", "n_sg", "err", "n_out")],
    )
    parent["id"] = "agt_err_parent"
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        await save_agent(sm, agents, child)
        paid, pver = await save_agent(sm, agents, parent)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": paid, "version": pver, "content_hash": None},
                {"handle": "nonexistent"},
            )
            res = await handle.get_result()
            assert res["status"] == "completed"
            assert "failed" in str(res["output"]) and "agt_err_child" in str(res["output"])
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


async def test_durable_two_live_outputs_fail_loudly(pg_url: str) -> None:
    # Parity with the interactive walker: two output nodes that BOTH execute fail the durable run
    # loudly (the run output would silently be whichever topo order visits last).
    await reset_dbos_schema(pg_url)
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node("n_out_a", "output", "boundary", ins=["in"]),
            _node("n_out_z", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_out_a"), _edge("e2", "n_in", "out", "n_out_z")],
    )
    ir["id"] = "agt_two_out_durable"
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, "x"
            )
            res = await handle.get_result()
            assert res["status"] == "failed"
            assert "second output node" in res["error"]
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


async def test_durable_runs_endpoint_rejects_unexecutable_types(pg_url: str) -> None:
    # A saved agent with a schema-legal-but-unexecutable node (code) passes validate_graph, so it
    # saves fine — but the durable-run endpoint must refuse it up front (400), like the
    # interactive initiators, instead of enqueueing a workflow that can only die.
    await reset_dbos_schema(pg_url)
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node("n_code", "code", "activity", config={}, ins=["in"], outs=["out"]),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_code"), _edge("e2", "n_code", "out", "n_out")],
    )
    ir["id"] = "agt_code_durable"
    with FakeInference() as fake:
        app = create_app(
            inference_base_url=fake.v1_url,
            database_url=pg_url,
            invoke_token=TOKEN,
            durable=True,
            dbos_fast_polling=True,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as ac:
                assert (await ac.post("/agents", json={"ir": ir})).status_code == 201
                r = await ac.post(f"/agents/{ir['id']}/durable-runs", json={"input": "x"})
                assert r.status_code == 400
                assert r.json()["error"]["code"] == "node_type_unavailable"
                # The same agent can't be DEPLOYED either: a trigger for it is refused at create
                # time (an unattended fire of an unrunnable agent helps nobody).
                t = await ac.post(
                    "/triggers",
                    json={
                        "agent_id": ir["id"],
                        "kind": "webhook",
                        "version": "0.1.0",
                        "config": {"secret": "s3cret"},
                    },
                )
                assert t.status_code == 400
                assert t.json()["error"]["code"] == "node_type_unavailable"


# ── no-IR-change: the new types round-trip through the registry, hash stable ─────


@pytest.mark.parametrize(
    "ir_dict",
    [
        _human_ir("agt_rt_human"),
        _subgraph_ir("agt_rt_sg", "agt_child", content_hash_pin="deadbeef"),
        _loop_ir("agt_rt_loop", "agt_body", version="0.1.0", max_iterations=2),
        _map_ir("agt_rt_map", "agt_body", version="0.1.0"),
    ],
)
async def test_new_types_roundtrip_through_registry_with_stable_hash(
    pg_url: str, ir_dict: dict
) -> None:
    # A saved agent using a new node type stores + reloads unchanged, and its contentHash is stable
    # across save/load — the IR shape (and the hashing rule) is untouched by the new node types.
    engine = db.create_engine(pg_url)
    sm = db.create_sessionmaker(engine)
    agents = AgentStore()
    try:
        doc, chash, _ = canonical_ir(ir_dict)
        await save_agent(sm, agents, ir_dict)
        async with sm() as s:
            stored = await agents.get_version_by_hash(s, doc.id, chash)
        assert stored is not None
        # Re-hashing the stored, view-stripped IR yields the same hash (the registry fixpoint holds
        # for the new node types — they are view-stripped graph content, covered unchanged).
        assert content_hash(parse_document(stored.ir)) == chash
    finally:
        await engine.dispose()
