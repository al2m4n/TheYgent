"""Core observability unit tests (M17) — the wrapper + stores against real PG, no walker yet.

These prove the seam mechanics in isolation (the walker/API tests build on them): a node span +
node_io land, capture policy gates payloads, the cap truncates, worker attribution is stamped, and
the deterministic-id ON CONFLICT DO NOTHING makes a re-emit idempotent (the resume property).
"""

from __future__ import annotations

from theygent_control_plane import db
from theygent_control_plane.observability import Telemetry
from theygent_control_plane.observability.spans import resolve_effective_capture
from theygent_control_plane.store import RunStore


async def _seed_run(sessionmaker, model: str = "triage-fast") -> str:
    store = RunStore()
    async with sessionmaker() as s, s.begin():
        run = await store.create_run(s, model=model, thread_id=None, params=None)
    return run.id


async def _make(pg_url: str):
    engine = db.create_engine(pg_url)
    sm = db.create_sessionmaker(engine)
    return engine, sm


async def test_node_span_and_io_roundtrip(pg_url: str) -> None:
    engine, sm = await _make(pg_url)
    try:
        run_id = await _seed_run(sm)
        tel = Telemetry(sessionmaker=sm, ceiling="full", topology="full")
        rt = tel.begin_run(run_id, executor_id="inproc", capture_level="full")
        async with rt.node_span(_FakeNode("n_llm", "llm", "activity")) as scope:
            scope.set_io(inputs={"in": "the file text"}, outputs={"ok": "the summary"})
            scope.set_attributes({"gen_ai.request.model": "triage-fast", "ttft_ms": 12})
        await rt.finish(status="ok")

        async with sm() as s:
            spans = await tel.trace_store.list_spans(s, run_id)
            io = await tel.io_store.get_io(s, run_id, "n_llm")
        names = {sp.name for sp in spans}
        assert run_id in names and "n_llm" in names  # root + node span
        node = next(sp for sp in spans if sp.node_id == "n_llm")
        assert node.status == "ok" and node.start_ns <= (node.end_ns or 0)
        assert node.executor_id == "inproc" and node.worker_host  # worker attribution stamped
        assert node.parent_span_id is not None  # parented to the run root
        assert io is not None and io.capture_level == "full"
        assert io.inputs == {"in": "the file text"} and io.outputs == {"ok": "the summary"}
        assert io.bytes_in > 0 and io.bytes_out > 0
    finally:
        await engine.dispose()


async def test_metadata_capture_writes_sizes_not_payloads(pg_url: str) -> None:
    engine, sm = await _make(pg_url)
    try:
        run_id = await _seed_run(sm)
        tel = Telemetry(sessionmaker=sm, ceiling="full", topology="full")
        rt = tel.begin_run(run_id, executor_id="inproc", capture_level="metadata")
        async with rt.node_span(_FakeNode("n", "tool", "activity")) as scope:
            scope.set_io(inputs={"in": "secret payload"}, outputs={"ok": "result"})
        await rt.finish()
        async with sm() as s:
            io = await tel.io_store.get_io(s, run_id, "n")
        assert io is not None and io.capture_level == "metadata"
        assert io.inputs is None and io.outputs is None  # payloads NOT persisted
        assert io.bytes_in > 0 and io.bytes_out > 0  # sizes ARE
    finally:
        await engine.dispose()


async def test_off_capture_writes_no_io_but_span_still_lands(pg_url: str) -> None:
    engine, sm = await _make(pg_url)
    try:
        run_id = await _seed_run(sm)
        tel = Telemetry(sessionmaker=sm, ceiling="full", topology="full")
        rt = tel.begin_run(run_id, executor_id="inproc", capture_level="off")
        async with rt.node_span(_FakeNode("n", "llm", "activity")) as scope:
            scope.set_io(inputs={"in": "x"}, outputs={"ok": "y"})
        await rt.finish()
        async with sm() as s:
            spans = await tel.trace_store.list_spans(s, run_id)
            io = await tel.io_store.get_io(s, run_id, "n")
        assert any(sp.node_id == "n" for sp in spans)  # timing span STILL written
        assert io is None  # but no node_io row (capture off)
    finally:
        await engine.dispose()


async def test_off_capture_leaks_no_byte_sizes(pg_url: str) -> None:
    # 'off' is the sovereignty hard stop: not even I/O-DERIVED metadata (byte counts) may land on
    # the span row (or ride the OTLP export) — that is what distinguishes it from 'metadata'.
    engine, sm = await _make(pg_url)
    try:
        run_id = await _seed_run(sm)
        tel = Telemetry(sessionmaker=sm, ceiling="full", topology="full")
        rt = tel.begin_run(run_id, executor_id="inproc", capture_level="off")
        async with rt.node_span(_FakeNode("n", "llm", "activity")) as scope:
            scope.set_io(inputs={"in": "x"}, outputs={"ok": "y"})
        await rt.finish()
        async with sm() as s:
            spans = await tel.trace_store.list_spans(s, run_id)
        node = next(sp for sp in spans if sp.node_id == "n")
        attrs = node.attributes or {}
        assert "theygent.bytes_in" not in attrs and "theygent.bytes_out" not in attrs
    finally:
        await engine.dispose()


async def test_pathological_payload_never_fails_the_span_close(pg_url: str) -> None:
    # A circular value defeats json serialization TWICE (sizing, then the truncation preview) —
    # the capture must degrade, never raise out of the span close into the run.
    engine, sm = await _make(pg_url)
    try:
        run_id = await _seed_run(sm)
        tel = Telemetry(sessionmaker=sm, ceiling="full", topology="full", max_bytes=8)
        rt = tel.begin_run(run_id, executor_id="inproc", capture_level="full")
        circular: dict[str, object] = {}
        circular["self"] = circular
        async with rt.node_span(_FakeNode("n", "tool", "activity")) as scope:
            scope.set_io(inputs={"in": circular}, outputs={"ok": "fine"})
        await rt.finish()  # reaching here at all is the assertion — no exception escaped
        async with sm() as s:
            spans = await tel.trace_store.list_spans(s, run_id)
        assert any(sp.node_id == "n" for sp in spans)  # the timing span still landed
    finally:
        await engine.dispose()


async def test_over_cap_payload_truncates(pg_url: str) -> None:
    engine, sm = await _make(pg_url)
    try:
        run_id = await _seed_run(sm)
        tel = Telemetry(sessionmaker=sm, ceiling="full", topology="full", max_bytes=64)
        rt = tel.begin_run(run_id, executor_id="inproc", capture_level="full")
        big = "x" * 5000
        async with rt.node_span(_FakeNode("n", "tool", "activity")) as scope:
            scope.set_io(inputs={"in": big}, outputs={"ok": "small"})
        await rt.finish()
        async with sm() as s:
            io = await tel.io_store.get_io(s, run_id, "n")
        assert io is not None and io.truncated is True
        assert io.bytes_in > 64  # the TRUE byte count, not the cap
        assert io.inputs is not None and io.inputs["in"]["_truncated"] is True
    finally:
        await engine.dispose()


async def test_reemit_is_idempotent_first_writer_wins(pg_url: str) -> None:
    # The resume property in miniature (§4): re-running the wrapper with the SAME run/node id from a
    # different "worker" must NOT overwrite the row written by the worker that first completed it.
    engine, sm = await _make(pg_url)
    try:
        run_id = await _seed_run(sm)
        tel = Telemetry(sessionmaker=sm, ceiling="full", topology="full")
        rt1 = tel.begin_run(run_id, executor_id="worker-1", capture_level="full")
        async with rt1.node_span(_FakeNode("n", "llm", "activity")) as scope:
            scope.set_io(inputs={"in": "a"}, outputs={"ok": "b"})
        # A "resume" on a different worker re-opens the SAME node id (deterministic span pk).
        rt2 = tel.begin_run(run_id, executor_id="worker-2", capture_level="full")
        async with rt2.node_span(_FakeNode("n", "llm", "activity")) as scope:
            scope.set_io(inputs={"in": "a"}, outputs={"ok": "b"})
        async with sm() as s:
            spans = await tel.trace_store.list_spans(s, run_id)
        node = next(sp for sp in spans if sp.node_id == "n")
        assert node.executor_id == "worker-1"  # first writer wins — history preserved
    finally:
        await engine.dispose()


async def test_capture_policy_precedence() -> None:
    # Hosted topology default is metadata; an agent may opt UP to full (under a full ceiling).
    assert (
        resolve_effective_capture(ceiling="full", topology_default="metadata", agent_policy="full")
        == "full"
    )
    # A metadata deployment CEILING caps even an agent requesting full.
    assert (
        resolve_effective_capture(ceiling="metadata", topology_default="full", agent_policy="full")
        == "metadata"
    )
    # No agent policy on hosted → the metadata sovereignty default.
    assert (
        resolve_effective_capture(ceiling="full", topology_default="metadata", agent_policy=None)
        == "metadata"
    )


class _FakeNode:
    """A stand-in IR node (id/type/kind) for the core tests — the wrapper only reads those three."""

    def __init__(self, node_id: str, type_: str, kind: str) -> None:
        self.id = node_id
        self.type = type_
        self.kind = kind
