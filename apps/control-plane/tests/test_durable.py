"""Fast suite for the durable runtime — DBOS embedded, against a REAL
ephemeral Postgres (Alembic ``public`` + DBOS ``dbos`` schema). No SQLite, no create_all.

Thesis: a run survives a crash and resumes from the last completed step, and cron
schedules become DBOS-native durable schedules. So these prove: walker/compiler PARITY (the IR seam
is untouched); KILL-AND-RESUME with no duplicated side effects (the headline) + the determinism
guard (a journaled activity is not re-executed on replay); schedules run via DBOS, survive a
restart, and a disabled trigger has no live schedule; a webhook fires durably; a saved agent
runs unchanged; and the two migration chains coexist + round-trip.

DBOS is a process-global singleton: each test launches + destroys it and starts from
a freshly migrated ``dbos`` schema (``reset_dbos_schema``), with an autouse teardown that
force-destroys DBOS so one failing test can't wedge the next.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime

import _auth
import httpx
import pytest
from _durable import (
    BlockingInference,
    TransientFailInference,
    reset_dbos_schema,
    reset_side_effect,
    save_agent,
    side_effect_count,
    side_effect_entered,
)
from _fake_inference import FULL_MESSAGE, FakeInference
from _ir import _doc, _edge, _node, trivial_ir, vision_llm_ir
from fastapi.testclient import TestClient
from theygent_control_plane import db
from theygent_control_plane.app import create_app
from theygent_control_plane.durable.runtime import DurableRuntime, _schedule_name
from theygent_control_plane.mcp import McpManager
from theygent_control_plane.store import AgentStore, RunStore, TriggerStore
from theygent_ir import content_hash, parse_document

TOKEN = "durable-token"


@pytest.fixture(autouse=True)
def _ensure_dbos_destroyed() -> Iterator[None]:
    # DBOS is process-global; guarantee a clean slate after every durable test (even on failure) so
    # a leftover launched instance can't break the next test's launch.
    yield
    with contextlib.suppress(Exception):
        from dbos import DBOS

        DBOS.destroy(destroy_registry=False)


def _two_step_ir(agent_id: str) -> dict:
    """input → llm(n_a) → llm(n_b) → output. Two activities so a crash mid-``n_b`` leaves ``n_a``
    journaled — the kill-and-resume substrate. ``n_b`` reads ``n_a``'s output via ``$in.in``."""
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


def _build_runtime(pg_url: str, fake_url: str, agents: AgentStore, triggers: TriggerStore, sm):
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


# ── walker/compiler parity (the IR seam is untouched) ────────────────────────────


async def test_durable_run_matches_the_walker(pg_url: str) -> None:
    # The same saved agent produces the same final output + Run record through the durable
    # theygent_run as through the interactive walker (/graphs/runs) — proof the compiler re-targets
    # lowering ONLY (the one rule: the IR seam is untouched; parity is structural).
    await reset_dbos_schema(pg_url)
    ir = trivial_ir()
    ir["id"] = "agt_parity"
    with FakeInference() as fake:
        # interactive baseline (non-durable, interactive walker)
        app = create_app(
            inference_base_url=fake.v1_url, database_url=pg_url, start_dispatcher=False
        )
        with TestClient(app) as c:
            interactive = c.post(
                "/graphs/runs", json={"ir": ir, "input": "hi", "stream": False}
            ).json()
            assert interactive["status"] == "completed"
            interactive_row = c.get(f"/runs/{interactive['runId']}").json()

        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, "hi"
            )
            durable = await handle.get_result()
            assert durable["status"] == "completed"
            async with sm() as s:
                row = await RunStore().get_run(s, durable["runId"])
            assert row is not None
            # Behavioral parity: same final OUTPUT through both paths (the IR seam is untouched).
            assert durable["output"] == interactive["output"] == FULL_MESSAGE
            # Run-RECORD parity: the durable run carries the same graph identity the walker recorded
            # (id / version / contentHash / status / output), and stamps completed_at.
            assert row.status == interactive_row["status"] == "completed"
            assert row.output == interactive_row["output"] == FULL_MESSAGE
            assert row.graph_id == interactive_row["graph_id"] == aid
            assert row.graph_version == interactive_row["graph_version"] == ver
            assert (
                row.content_hash
                == interactive_row["content_hash"]
                == content_hash(parse_document(ir))
            )
            assert row.completed_at is not None
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


async def test_durable_tool_loop_runs_to_a_final_answer(pg_url: str) -> None:
    # An llm with tools runs its bounded tool loop on the DURABLE runtime — each model
    # turn is a journaled `_llm_step`, each tool call a journaled step (`_durable_tool_call`).
    # The scripted fake calls `echo` once, then answers; the durable run completes with the
    # post-tool answer. Same primitives as the interactive loop, here through theygent_run.
    await reset_dbos_schema(pg_url)
    ir = trivial_ir()
    ir["id"] = "agt_tool_loop"
    ir["tools"] = {"echo": {"kind": "builtin", "ref": "echo"}}
    ir["nodes"][1]["config"]["tools"] = ["echo"]
    ir["nodes"][1]["config"]["maxToolIterations"] = 4
    with FakeInference(
        mode="tool_call",
        tool_name="echo",
        tool_args={"value": "hi"},
        response="durable tool answer",
    ) as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, "do it"
            )
            durable = await handle.get_result()
            assert durable["status"] == "completed"
            assert durable["output"] == "durable tool answer"
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


async def test_durable_wired_capability_tool_then_answers(pg_url: str) -> None:
    # A tool node wired to the llm's `tools` port (NO config.tools) is callable on the
    # DURABLE runtime too — the model calls it by NODE id, `_durable_tool_call` resolves the binding
    # from the node's inline config and journals each step. Same primitives as the interactive
    # wired-tool path (test_tool_wiring.py), here through theygent_run.
    await reset_dbos_schema(pg_url)
    ir = trivial_ir()
    ir["id"] = "agt_wired_capability"
    llm = ir["nodes"][1]
    llm["ports"]["in"].append({"id": "tools", "type": "any", "required": False, "role": "tool"})
    llm["config"]["maxToolIterations"] = 4
    ir["nodes"].append(
        {
            "id": "n_echo",
            "type": "tool",
            "kind": "activity",
            "config": {"tool": "echo", "description": "echo the value back"},
            "ports": {
                "in": [{"id": "in", "type": "any", "required": True}],
                "out": [{"id": "out", "type": "any"}, {"id": "err", "type": "error"}],
            },
        }
    )
    ir["edges"].append(
        {
            "id": "e_tool",
            "source": "n_echo",
            "sourceHandle": "out",
            "target": llm["id"],
            "targetHandle": "tools",
            "channel": "tool",
        }
    )
    with FakeInference(
        mode="tool_call",
        tool_name="n_echo",
        tool_args={"value": "hi"},
        response="durable wired answer",
    ) as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, "do it"
            )
            durable = await handle.get_result()
            assert durable["status"] == "completed"
            assert durable["output"] == "durable wired answer"
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


async def test_durable_multimodal_content_survives_the_journal(pg_url: str) -> None:
    # Durable-path multimodal: the rendered ``messages`` are a DBOS step argument, so
    # multimodal content (text + image_url) is serialized into the journal and handed to the
    # gateway from there. Prove the image block survives that round-trip unchanged AND that
    # the image enters from the run input via ``$in`` inside the image part — a vision agent
    # on a durable graph. (The interactive parity for the same shape lives in
    # test_vision_content.py.)
    await reset_dbos_schema(pg_url)
    image = "https://example.com/cat.png"
    ir = vision_llm_ir("Describe the image.", "$in")
    ir["id"] = "agt_vision_durable"
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, image
            )
            durable = await handle.get_result()
            assert durable["status"] == "completed"
            # The image content block crossed the wire VERBATIM after a trip through the DBOS
            # journal, with ``$in`` substituted to the run input — the multimodal payload survives.
            assert fake.captured["messages"] == [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe the image."},
                        {"type": "image_url", "image_url": {"url": image}},
                    ],
                }
            ]
            assert fake.captured["model"] == "qwen2-vl-vision"
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── kill-and-resume (the headline) + the determinism guard ───────────────────────


async def test_kill_and_resume_completed_step_runs_exactly_once(pg_url: str) -> None:
    # Start a 2-activity graph; freeze the worker mid-``n_b`` (after ``n_a`` COMPLETED + journaled);
    # CRASH (DBOS.destroy); restart → DBOS recovers the pending workflow and resumes at ``n_b``,
    # NOT from scratch. ``n_a`` (the COMPLETED, journaled step) is replayed from the journal — its
    # executor is **exactly-once**, NOT re-invoked (its tokens are not re-streamed — journaled
    # replay). The at-least-once guarantee for the INTERRUPTED step is proven separately below.
    await reset_dbos_schema(pg_url)
    ir = _two_step_ir("agt_resume")
    responses = {"GO": "OUT1", "OUT1": "FINAL"}
    with BlockingInference(responses, block_prompt="OUT1") as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        ref = {"agent_id": aid, "version": ver, "content_hash": None}

        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        handle = await rt.enqueue_run(ref, "GO")
        wid = handle.workflow_id
        # Wait until n_b's first call is blocked → n_a has completed and journaled.
        for _ in range(1000):
            if fake.blocked_entered.is_set():
                break
            await _sleep()
        assert fake.blocked_entered.is_set()
        assert fake.calls.get("GO") == 1  # n_a executed exactly once

        rt.shutdown()  # CRASH: DBOS.destroy() mid-n_b (gateway intentionally NOT closed yet)

        from dbos import DBOS

        rt2, _gw2 = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        # rt2 reuses the SAME gateway resources; launch triggers recovery of the pending workflow.
        rt2.launch()
        try:
            rh = await DBOS.retrieve_workflow_async(wid)
            res = await rh.get_result()
            assert res["status"] == "completed"
            assert res["output"] == "FINAL"
            # The headline assertions: n_a was NOT re-executed on resume (journaled replay), and n_b
            # ran twice (the interrupted attempt + the recovered one).
            assert fake.calls["GO"] == 1, "n_a re-executed on resume — duplicated side effect!"
            assert fake.calls["OUT1"] == 2
            async with sm() as s:
                row = await RunStore().get_run(s, wid)
            assert row.status == "completed" and row.output == "FINAL"
        finally:
            rt2.shutdown()
            await gw.aclose()
            await _gw2.aclose()
            await engine.dispose()


def _sideeffect_ir(agent_id: str) -> dict:
    """input → tool(_durable_sideeffect, {value: $in}) → output. A single activity whose body has a
    non-idempotent OBSERVABLE side effect (a counter increment), used to prove the at-least-once
    guarantee for an interrupted step."""
    ir = _doc(
        [
            _node("n_in", "input", "boundary", outs=["out"]),
            _node(
                "n_tool",
                "tool",
                "activity",
                config={"tool": "_durable_sideeffect", "args": {"value": "$in"}},
                ins=["in"],
                outs=["ok", "err"],
            ),
            _node("n_out", "output", "boundary", ins=["in"]),
        ],
        [_edge("e1", "n_in", "out", "n_tool"), _edge("e2", "n_tool", "ok", "n_out")],
    )
    ir["id"] = agent_id
    return ir


async def test_interrupted_step_is_at_least_once(pg_url: str) -> None:
    # The HONEST guarantee: a COMPLETED step is exactly-once (proven above), but the step that
    # was IN-FLIGHT at the crash RE-EXECUTES on resume — at-least-once. A non-idempotent side effect
    # therefore repeats. This is the contract the `human` node inherits; state it and test it, don't
    # paper over it. The side-effecting tool increments a counter (the effect), then its first call
    # blocks; we crash mid-step, resume, and assert the effect happened TWICE.
    await reset_dbos_schema(pg_url)
    reset_side_effect()
    with FakeInference() as fake:  # gateway is required by the runtime; this graph has no llm node
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, _sideeffect_ir("agt_atleastonce"))
        ref = {"agent_id": aid, "version": ver, "content_hash": None}

        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        handle = await rt.enqueue_run(ref, "x")
        wid = handle.workflow_id
        for _ in range(1000):
            if side_effect_entered():
                break
            await _sleep()
        assert side_effect_entered()
        assert side_effect_count() == 1  # the effect happened once (the interrupted attempt)

        rt.shutdown()  # CRASH while the tool step is in-flight (after the effect, before returning)

        from dbos import DBOS

        rt2, _gw2 = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt2.launch()
        try:
            rh = await DBOS.retrieve_workflow_async(wid)
            res = await rh.get_result()
            assert res["status"] == "completed"
            # The interrupted step re-executed → the NON-IDEMPOTENT effect repeated. At-least-once,
            # by design — exactly-once is only for journaled (completed) steps.
            assert side_effect_count() == 2, "interrupted step should re-execute on resume"
        finally:
            rt2.shutdown()
            await gw.aclose()
            await _gw2.aclose()
            await engine.dispose()


# ── transient-failure retry (regression guard) ───────────────────────────────────


async def test_transient_503_is_retried_not_failed(pg_url: str) -> None:
    # The durable gateway turns provider-client retry OFF so DBOS owns retry (no double-retry). That
    # only holds if the steps actually retry — else a single transient 503 fails the whole run with
    # no retry at any layer (the double-retry hazard). Inject ONE 503, then success: the run must
    # COMPLETE (the step retried), not fail. (max_attempts/interval are the sane defaults; tuning
    # is deferred.)
    await reset_dbos_schema(pg_url)
    ir = trivial_ir()
    ir["id"] = "agt_retry"
    with TransientFailInference(fail_count=1) as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, "hi"
            )
            res = await handle.get_result()
            assert res["status"] == "completed", res  # the 503 was retried, not surfaced as failure
            assert res["output"] == "RECOVERED"
            assert fake.calls == 2  # one 503 + one success = DBOS retried the step
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── completed_at on a FAILED terminal transition (evidence-gate honesty) ─────────


async def test_failed_durable_run_stamps_completed_at(pg_url: str) -> None:
    # A failed run's duration must be exact too, else failed-run duration stays lossy. An
    # engine-name binding fails fast in the workflow (no retry) → the FAILED terminal transition
    # stamps completed_at (only a reconcile-swept zombie stays NULL — unknown end time).
    await reset_dbos_schema(pg_url)
    ir = trivial_ir()
    ir["id"] = "agt_failed"
    ir["models"]["default"]["model"] = "mlx"  # an ENGINE name, not a logical id → fails fast
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            handle = await rt.enqueue_run(
                {"agent_id": aid, "version": ver, "content_hash": None}, "hi"
            )
            res = await handle.get_result()
            assert res["status"] == "failed"
            async with sm() as s:
                row = await RunStore().get_run(s, res["runId"])
            assert row.status == "failed"
            assert row.completed_at is not None  # failed transition stamps completed_at too
            assert row.completed_at >= row.created_at
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── multi-instance schedule dedup (two instances must never double-fire) ──────────


async def test_schedule_fires_once_across_instances(pg_url: str) -> None:
    # "The single-dispatcher constraint is lifted" rests on DBOS deduping a schedule's per-tick
    # workflow across instances. DBOS's scheduler keys each tick's workflow id on
    # (schedule_name, scheduled_time), so two executors firing the SAME cron instant collide on that
    # id and DBOS runs it ONCE. DBOS is process-global (can't launch two in one test process), so we
    # prove the underlying property directly: two enqueues with the same deterministic workflow id
    # produce ONE execution and ONE run row — exactly the dedup two real instances rely on.
    await reset_dbos_schema(pg_url)
    ir = trivial_ir()
    ir["id"] = "agt_dedup"
    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        aid, ver = await save_agent(sm, agents, ir)
        ref = {"agent_id": aid, "version": ver, "content_hash": None}
        rt, gw = _build_runtime(pg_url, fake.v1_url, agents, TriggerStore(), sm)
        rt.launch()
        try:
            from dbos import DBOS, SetWorkflowID

            det_id = "trigger-dedup-2026-01-01T00:00:00"  # the deterministic per-tick id
            with SetWorkflowID(det_id):
                h1 = await rt.enqueue_run(ref, "hi")
            with SetWorkflowID(det_id):
                h2 = await rt.enqueue_run(ref, "hi")
            r1 = await h1.get_result()
            r2 = await h2.get_result()
            # Both handles resolve to the SAME workflow (the run id IS the workflow id) → deduped.
            assert r1["runId"] == r2["runId"] == det_id
            steps = await DBOS.list_workflow_steps_async(det_id)
            assert len([s for s in steps if "_llm_step" in s["function_name"]]) == 1  # ran once
            async with sm() as s:
                runs = await RunStore().list_runs(s, limit=10)
            assert len([r for r in runs if r.graph_id == aid]) == 1  # exactly one run, not two
        finally:
            rt.shutdown()
            await gw.aclose()
            await engine.dispose()


# ── schedules run via DBOS, survive a restart, and disable removes the schedule ──


async def _save_agent_http(c, ir: dict) -> str:
    r = await c.post("/agents", json={"ir": ir})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _agent_ir(prompt: str = "$in", agent_id: str = "agt_sched") -> dict:
    ir = trivial_ir()
    ir["id"] = agent_id
    ir["nodes"][1]["config"]["messages"][0]["content"] = prompt
    return ir


async def _fire_schedule(tid: str):
    # Fire a schedule deterministically (no cron wait): start the scheduled workflow directly. This
    # is exactly what a DBOS cron tick invokes — theygent_scheduled_fire(scheduled_time, tid).
    from dbos import DBOS
    from theygent_control_plane.durable.compiler import theygent_scheduled_fire

    h = await DBOS.start_workflow_async(
        theygent_scheduled_fire, datetime(2026, 1, 1, tzinfo=UTC), tid
    )
    await h.get_result()


async def test_schedule_runs_via_dbos_and_disable_removes_it(pg_url: str) -> None:
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
                await _auth.attach_async(ac)
                agent_id = await _save_agent_http(ac, _agent_ir(prompt="SCHED: $in"))
                created = await ac.post(
                    "/triggers",
                    json={
                        "agent_id": agent_id,
                        "kind": "schedule",
                        "version": "0.1.0",
                        "config": {"cron": "* * * * *", "input": "q"},
                    },
                )
                assert created.status_code == 201, created.text
                tid = created.json()["id"]

                from dbos import DBOS

                # The trigger is mirrored into a live DBOS dynamic schedule.
                assert await DBOS.get_schedule_async(_schedule_name(tid)) is not None

                # Firing it runs the pinned agent durably, with lineage + the scheduled input.
                await _fire_schedule(tid)
                runs = (await ac.get("/runs")).json()["runs"]
                mine = [r for r in runs if r["trigger_id"] == tid]
                assert len(mine) == 1 and mine[0]["status"] == "completed"
                assert mine[0]["graph_version"] == "0.1.0"
                assert fake.captured["messages"] == [{"role": "user", "content": "SCHED: q"}]

                # Disabling the trigger removes its live DBOS schedule (no orphan keeps firing).
                assert (
                    await ac.patch(f"/triggers/{tid}", json={"enabled": False})
                ).status_code == 200
                assert await DBOS.get_schedule_async(_schedule_name(tid)) is None


async def test_schedule_persists_and_reconciles_across_restart(pg_url: str) -> None:
    # The schedule lives in theygent's `trigger` table (the frozen source of truth). A fresh
    # control-plane reconciles DBOS schedules to the enabled triggers on boot — so a schedule
    # registered by one instance is re-established (and still fires) by a restarted one.
    await reset_dbos_schema(pg_url)
    # Drive the two runtime lifecycles directly (the create_app lifespan is exercised by the other
    # durable tests). DBOS is process-global, so the "restart" is rt1.shutdown() (DBOS.destroy) then
    # a fresh rt2.launch() in the same process — exactly the crash-recovery lifecycle the kill test
    # uses, here proving boot reconciliation re-establishes a persisted schedule.
    from dbos import DBOS

    with FakeInference() as fake:
        engine = db.create_engine(pg_url)
        sm = db.create_sessionmaker(engine)
        agents = AgentStore()
        triggers = TriggerStore()
        aid, ver = await save_agent(sm, agents, _agent_ir(prompt="R: $in"))
        async with sm() as s, s.begin():
            trig = await triggers.create(
                s,
                agent_id=aid,
                kind="schedule",
                version=ver,
                content_hash=None,
                config={"cron": "* * * * *", "input": "q"},
                enabled=True,
            )

        rt1, gw1 = _build_runtime(pg_url, fake.v1_url, agents, triggers, sm)
        rt1.launch()
        async with sm() as s:
            enabled = await triggers.list_enabled_schedules(s)
        await rt1.reconcile_schedules(enabled)
        assert await DBOS.get_schedule_async(_schedule_name(trig.id)) is not None
        rt1.shutdown()  # "restart": tear DBOS down

        rt2, gw2 = _build_runtime(pg_url, fake.v1_url, agents, triggers, sm)
        rt2.launch()
        try:
            # Boot reconciliation re-establishes the schedule from the persisted enabled trigger —
            # schedules are re-read from Postgres, never from in-memory dispatcher state.
            async with sm() as s:
                enabled = await triggers.list_enabled_schedules(s)
            await rt2.reconcile_schedules(enabled)
            assert await DBOS.get_schedule_async(_schedule_name(trig.id)) is not None
            await _fire_schedule(trig.id)
            async with sm() as s:
                runs = await RunStore().list_runs(s, limit=10)
            assert [r.trigger_id for r in runs if r.trigger_id] == [trig.id]
        finally:
            rt2.shutdown()
            await gw1.aclose()
            await gw2.aclose()
            await engine.dispose()


# ── a webhook fires durably ──────────────────────────────────────────────────────


async def test_webhook_fires_durably_with_lineage(pg_url: str) -> None:
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
                await _auth.attach_async(ac)
                agent_id = await _save_agent_http(
                    ac, _agent_ir(prompt="$in.in.text", agent_id="agt_hook")
                )
                created = await ac.post(
                    "/triggers",
                    json={
                        "agent_id": agent_id,
                        "kind": "webhook",
                        "version": "0.1.0",
                        "config": {"secret": "shh"},
                    },
                )
                tid = created.json()["id"]
                raw = json.dumps({"text": "from-webhook"}).encode()
                sig = "sha256=" + hmac.new(b"shh", raw, hashlib.sha256).hexdigest()
                resp = await ac.post(
                    f"/hooks/{tid}",
                    content=raw,
                    headers={"X-TheYgent-Signature": sig, "Content-Type": "application/json"},
                )
                assert resp.status_code == 202, resp.text
                body = resp.json()
                assert body["status"] == "completed"
                # Durable run carries trigger lineage; the payload mapped to the agent's input.
                got = (await ac.get(f"/runs/{body['runId']}")).json()
                assert got["trigger_id"] == tid
                assert fake.captured["messages"] == [{"role": "user", "content": "from-webhook"}]


# ── migration round-trip: the two chains coexist (own throwaway PG) ──────────────


@pytest.mark.parametrize("_", [0])
def test_alembic_and_dbos_schemas_coexist_and_roundtrip(_: int) -> None:
    # Alembic owns `public`; DBOS owns `dbos`; they never touch each other. On its OWN
    # throwaway PG (a downgrade-to-base would wipe `public` the session DB shares): upgrade head +
    # dbos migrate coexist; alembic down→base leaves `dbos` intact; re-upgrade head is clean.
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers not installed")
    import psycopg
    from alembic import command
    from alembic.config import Config
    from theygent_control_plane.durable import run_dbos_migrations
    from theygent_control_plane.durable.config import to_dbos_url

    try:
        # pgvector image: the migration chain includes CREATE EXTENSION vector (retrieval).
        container = PostgresContainer("pgvector/pgvector:pg16", driver="asyncpg")
        container.start()
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"Docker/Postgres unavailable: {exc}")

    def _schemas() -> set[str]:
        with psycopg.connect(to_dbos_url(url)) as conn:
            rows = conn.execute("SELECT schema_name FROM information_schema.schemata").fetchall()
        return {r[0] for r in rows}

    # This test points Alembic at its OWN container via DATABASE_URL; restore it afterward so the
    # rest of the suite (which reads os.environ["DATABASE_URL"] for the session container) is not
    # left pointing at this now-stopped throwaway PG.
    saved_db_url = os.environ.get("DATABASE_URL")
    try:
        url = container.get_connection_url()
        os.environ["DATABASE_URL"] = url
        cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
        command.upgrade(cfg, "head")
        run_dbos_migrations(url)
        schemas = _schemas()
        assert "public" in schemas and "dbos" in schemas
        # Downgrade Alembic to base — `public` app tables go, `dbos` is untouched (DBOS owns it).
        command.downgrade(cfg, "base")
        assert "dbos" in _schemas()
        # And re-upgrade is clean (the chains never collide).
        command.upgrade(cfg, "head")
        assert {"public", "dbos"} <= _schemas()
    finally:
        container.stop()
        if saved_db_url is not None:
            os.environ["DATABASE_URL"] = saved_db_url
        else:  # pragma: no cover
            os.environ.pop("DATABASE_URL", None)


async def _sleep() -> None:
    import asyncio

    await asyncio.sleep(0.02)
