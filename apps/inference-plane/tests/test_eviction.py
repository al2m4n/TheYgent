"""Eviction policy + manager lifecycle — deterministic, no real engine, no sleeps.

These run the manager in a single event loop (pytest-asyncio auto mode) with a
no-op launcher, an injected ManualClock, and an injected probe — so idle eviction
and memory-pressure arbitration are pure, fast, and deterministic.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

import pytest
from _payloads import managed_payload
from theygent_inference_plane.clock import ManualClock
from theygent_inference_plane.eviction import (
    CountAndPriorityPolicy,
    PendingLoad,
    ResidentView,
    ResourceBudget,
)
from theygent_inference_plane.manager import EngineManager, NoCapacityError
from theygent_inference_plane.registry import Registry
from theygent_ir import Capabilities, ManagedBinding, parse_registration


class _NoopHandle:
    def __init__(self) -> None:
        self.terminated = False

    @property
    def base_url(self) -> str:
        return "http://127.0.0.1:0"

    async def health(self) -> bool:
        return True

    async def capabilities(self) -> Capabilities:
        return Capabilities()

    async def terminate(self) -> None:
        self.terminated = True


class _NoopLauncher:
    ready = True
    not_ready_reason = None

    def __init__(self, launch_delay: float = 0.0) -> None:
        self.handles: list[_NoopHandle] = []
        self._launch_delay = launch_delay

    async def launch(self, binding: ManagedBinding) -> _NoopHandle:
        # A delay widens the window between "no engine yet" and "engine registered",
        # so a concurrency bug (double-spawn) would actually manifest.
        if self._launch_delay:
            await asyncio.sleep(self._launch_delay)
        handle = _NoopHandle()
        self.handles.append(handle)
        return handle


class _FlagProbe:
    def __init__(self, over: bool = False) -> None:
        self.over = over

    def over_watermark(self) -> bool:
        return self.over


def _register(reg: Registry, logical_id: str, **kwargs) -> None:
    reg.put(logical_id, parse_registration(managed_payload(model=logical_id, **kwargs)))


# ── policy unit (pure, sync) ────────────────────────────────────────────


def test_policy_orders_lowest_priority_then_lru() -> None:
    policy = CountAndPriorityPolicy()
    resident = [
        ResidentView("a", priority=5, last_used=100),
        ResidentView("b", priority=1, last_used=50),
        ResidentView("c", priority=1, last_used=200),
    ]
    # max 3, one incoming -> free 1; lowest priority, then LRU => "b".
    assert policy.select_victims(resident, PendingLoad("x", 9), ResourceBudget(3)) == ["b"]


def test_policy_no_eviction_when_room_available() -> None:
    policy = CountAndPriorityPolicy()
    resident = [ResidentView("a", priority=1, last_used=1)]
    assert policy.select_victims(resident, PendingLoad("x", 1), ResourceBudget(3)) == []


def test_policy_counts_nonevictable_slots() -> None:
    # An idle evictable engine coexists with a stuck/draining engine the policy is NOT handed
    # (only evictable engines are passed). The policy must budget against ``resident_total`` =
    # the TRUE count (2), not ``len(resident)`` = 1 — otherwise it computes 1+1 <= 2, evicts
    # nothing, and the manager wedges at NoCapacity even though ``idle`` is perfectly evictable.
    policy = CountAndPriorityPolicy()
    resident = [ResidentView("idle", priority=1, last_used=1)]
    budget = ResourceBudget(max_resident=2, resident_total=2)
    assert policy.select_victims(resident, PendingLoad("new", 9), budget) == ["idle"]


def test_policy_no_total_falls_back_to_len_resident() -> None:
    # Callers that don't supply resident_total (default 0) fall back to len(resident).
    policy = CountAndPriorityPolicy()
    resident = [ResidentView("a", priority=1, last_used=1)]
    assert policy.select_victims(resident, PendingLoad("x", 1), ResourceBudget(3)) == []


def test_policy_no_incoming_budget_frees_exactly_the_excess() -> None:
    # Ceiling enforcement passes incoming_count=0 (nothing is being admitted): with 3
    # resident and a ceiling of 2 the policy must free exactly 1 — the admission-shaped
    # default (+1 incoming) would take one victim too many.
    policy = CountAndPriorityPolicy()
    resident = [
        ResidentView("a", priority=1, last_used=10),
        ResidentView("b", priority=1, last_used=20),
        ResidentView("c", priority=1, last_used=30),
    ]
    budget = ResourceBudget(max_resident=2, resident_total=3, incoming_count=0)
    assert policy.select_victims(resident, PendingLoad("", 0), budget) == ["a"]


def test_policy_watermark_forces_one_even_with_room() -> None:
    policy = CountAndPriorityPolicy()
    resident = [
        ResidentView("a", priority=5, last_used=1),
        ResidentView("b", priority=1, last_used=1),
    ]
    budget = ResourceBudget(max_resident=5, over_watermark=True)
    assert policy.select_victims(resident, PendingLoad("x", 1), budget) == ["b"]


# ── manager lifecycle (async) ───────────────────────────────────────────


async def test_idle_eviction() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "a", idle_timeout_sec=10)

    await mgr.warm("a")
    assert mgr.state("a")["resident"] is True

    clock.advance(11)
    await mgr.reap()
    assert mgr.state("a")["resident"] is False
    assert launcher.handles[0].terminated is True


async def test_keep_warm_survives_reap() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "pinned", idle_timeout_sec=10, keep_warm=True)

    await mgr.warm("pinned")
    clock.advance(1000)
    await mgr.reap()
    assert mgr.state("pinned")["resident"] is True


async def test_memory_pressure_evicts_lowest_priority() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    probe = _FlagProbe(over=False)
    mgr = EngineManager(reg, launcher, clock=clock, probe=probe, max_resident=3)
    _register(reg, "hi", priority=5)
    _register(reg, "lo", priority=1)

    await mgr.warm("hi")
    clock.advance(1)
    await mgr.warm("lo")

    probe.over = True  # memory pressure now
    _register(reg, "new", priority=9)
    await mgr.warm("new")

    assert mgr.state("lo")["resident"] is False  # lowest priority evicted
    assert mgr.state("hi")["resident"] is True
    assert mgr.state("new")["resident"] is True


async def test_inflight_engine_is_never_evicted_and_load_refused() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=1)
    _register(reg, "busy")
    _register(reg, "other")

    async with mgr.lease("busy"):
        with pytest.raises(NoCapacityError):
            await mgr.warm("other")
        assert mgr.state("busy")["resident"] is True


async def test_evict_drains_inflight_then_terminates() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "busy")

    async with mgr.lease("busy"):
        await mgr.evict("busy")  # in-flight -> mark draining, do not kill
        assert mgr.state("busy")["resident"] is True
        assert mgr.state("busy")["draining"] is True

    # lease released -> drained -> torn down
    assert mgr.state("busy")["resident"] is False
    assert launcher.handles[0].terminated is True


async def test_stuck_draining_engine_does_not_wedge_idle_eviction() -> None:
    # A permanently-draining engine (in-flight forever — e.g. a hung model subprocess) must NOT
    # block loading a new model when a DIFFERENT resident engine is idle/evictable: the policy
    # must count the draining engine's slot, evict the idle engine, and load — never raise
    # NoCapacityError.
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "idle")
    _register(reg, "stuck")
    _register(reg, "new")

    await mgr.warm("idle")  # slot 1: idle, evictable
    async with mgr.lease("stuck"):  # slot 2: stuck, in-flight (never releases)
        await mgr.evict("stuck")  # -> draining, but still in-flight so it stays resident
        assert mgr.state("stuck")["draining"] is True
        await mgr.warm("new")  # must evict 'idle' and load 'new', NOT raise NoCapacity
        assert mgr.state("new")["resident"] is True
        assert mgr.state("idle")["resident"] is False  # the idle engine was freed
        assert mgr.state("stuck")["resident"] is True  # the stuck engine kept its slot


async def test_evict_is_idempotent_when_not_resident() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "a")
    await mgr.evict("a")  # never loaded -> no-op, no error
    assert mgr.state("a")["resident"] is False


# ── runtime ceiling changes (enforce_ceiling) ───────────────────────────


async def test_enforce_ceiling_evicts_idle_excess_immediately() -> None:
    # Admission-time eviction alone would leave a lowered ceiling unenforced until the
    # next model load; enforce_ceiling applies it NOW, in policy order (priority, LRU).
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=3)
    _register(reg, "old", priority=1)
    _register(reg, "mid", priority=1)
    _register(reg, "vip", priority=9)
    await mgr.warm("old")
    clock.advance(1)
    await mgr.warm("mid")
    clock.advance(1)
    await mgr.warm("vip")

    mgr.set_max_resident(1)
    await mgr.enforce_ceiling()

    assert mgr.state("old")["resident"] is False  # lowest priority + least recently used
    assert mgr.state("mid")["resident"] is False
    assert mgr.state("vip")["resident"] is True
    assert sum(1 for h in launcher.handles if h.terminated) == 2


async def test_enforce_ceiling_noop_when_at_or_under() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "a")
    _register(reg, "b")
    await mgr.warm("a")
    await mgr.warm("b")

    await mgr.enforce_ceiling()  # at the ceiling — nothing to do
    mgr.set_max_resident(5)
    await mgr.enforce_ceiling()  # raised — nothing to do either

    assert mgr.state("a")["resident"] is True
    assert mgr.state("b")["resident"] is True
    assert not any(h.terminated for h in launcher.handles)


async def test_enforce_ceiling_drains_busy_engine_and_never_kills_inflight() -> None:
    # A busy engine over the ceiling is marked draining (the :evict-on-busy semantics):
    # it keeps serving its in-flight request and tears down only when it goes idle.
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "busy")
    _register(reg, "kept")

    async with mgr.lease("busy"):
        clock.advance(1)
        async with mgr.lease("kept"):
            pass  # "kept" is now idle and more recently used than "busy"

        mgr.set_max_resident(1)
        await mgr.enforce_ceiling()

        # The idle engine was the policy's victim; the busy one survives untouched.
        assert mgr.state("kept")["resident"] is False
        state = mgr.state("busy")
        assert state["resident"] is True
        assert state["draining"] is False
        assert state["inflight"] == 1

        # Lower again with ONLY the busy engine resident: still never killed — marked
        # draining instead, and it keeps serving.
        mgr.set_max_resident(0)
        await mgr.enforce_ceiling()
        state = mgr.state("busy")
        assert state["resident"] is True
        assert state["draining"] is True
        assert launcher.handles[0].terminated is False

    # Lease released → drained → torn down.
    assert mgr.state("busy")["resident"] is False
    assert launcher.handles[0].terminated is True


async def test_enforce_ceiling_counts_already_draining_slots_once() -> None:
    # An engine already draining will free its slot on release — enforce_ceiling must not
    # drain a second engine to cover the same excess.
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "draining")
    _register(reg, "steady")

    async with mgr.lease("draining"):
        await mgr.evict("draining")  # busy → draining
        async with mgr.lease("steady"):
            mgr.set_max_resident(1)
            await mgr.enforce_ceiling()
            # The draining engine already covers the excess; "steady" is left alone.
            assert mgr.state("steady")["draining"] is False
            assert mgr.state("steady")["resident"] is True

    assert mgr.state("draining")["resident"] is False  # drained on release
    assert mgr.state("steady")["resident"] is True


async def test_lease_during_drain_window_does_not_defeat_a_lowered_ceiling() -> None:
    # A ceiling lowered under three busy engines can only mark the excess draining. A
    # data-plane lease during that drain window clears the flag ("wanted again") — which
    # must not cancel enforcement forever: the release path re-asserts the ceiling when an
    # engine goes idle, so once the in-flight work completes the resident count converges
    # to the new maximum. No in-flight request is ever killed along the way.
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=3)
    for lid in ("a", "b", "c"):
        _register(reg, lid)

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(mgr.lease("a"))
        clock.advance(1)
        await stack.enter_async_context(mgr.lease("b"))
        clock.advance(1)
        await stack.enter_async_context(mgr.lease("c"))

        mgr.set_max_resident(2)
        await mgr.enforce_ceiling()
        # All three are busy: nothing is killed, the LRU one is marked draining.
        assert mgr.state("a")["draining"] is True
        assert not any(h.terminated for h in launcher.handles)

        # The drain-window lease: "a" is wanted again, its pending drain is cancelled.
        async with mgr.lease("a"):
            assert mgr.state("a")["draining"] is False
            assert mgr.state("a")["inflight"] == 2
        # The extra lease released, but "a" is still held busy by the outer lease — the
        # plane sits over the ceiling with no drain flag anywhere (the failure state the
        # release-path re-assertion exists for).
        assert len(mgr.resident_engines()) == 3
        assert not any(e["draining"] for e in mgr.resident_engines())

    # All work completed → the ceiling was re-asserted on release and the count converged.
    assert len(mgr.resident_engines()) == 2
    assert sum(1 for h in launcher.handles if h.terminated) == 1


# Concurrent cold-start singleflight: two simultaneous first-calls to a cold model
# must spawn exactly once. The slow launcher forces real interleaving.
async def test_concurrent_cold_start_spawns_once() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(launch_delay=0.05), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "a")

    async def call() -> None:
        async with mgr.lease("a"):
            pass

    await asyncio.gather(*(call() for _ in range(5)))

    assert mgr.spawn_count == 1
    assert len(launcher.handles) == 1
    assert mgr.state("a")["resident"] is True


# An engine that dies mid-stream still releases its lease — the in-flight slot
# never leaks, so the (dead) engine can be evicted and re-spawned cleanly.
async def test_error_midstream_releases_lease() -> None:
    reg, launcher, clock = Registry(), _NoopLauncher(), ManualClock()
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=1)
    _register(reg, "a")

    with pytest.raises(RuntimeError):
        async with mgr.lease("a"):
            raise RuntimeError("engine died mid-stream")

    state = mgr.state("a")
    assert state["resident"] is True
    assert state["inflight"] == 0  # slot released despite the error

    await mgr.evict("a")
    assert mgr.state("a")["resident"] is False


# A client disconnecting mid-stream cancels the response task, and the server keeps
# re-delivering that cancellation at every suspension until the task dies. If the release
# can be killed while waiting on a contended manager lock (a concurrent cold spawn holds
# it for seconds), the inflight decrement is lost and the engine is pinned non-evictable
# forever — eviction and reap only ever consider inflight == 0. The release is shielded,
# so it must complete even under repeated cancellation.
async def test_lease_release_survives_repeated_cancellation() -> None:
    reg, clock = Registry(), ManualClock()
    launcher = _NoopLauncher(launch_delay=0.2)  # a slow spawn holds the manager lock
    mgr = EngineManager(reg, launcher, clock=clock, max_resident=2)
    _register(reg, "a")
    _register(reg, "b")

    await mgr.warm("a")  # spawn "a" up front so its lease is instant
    parked = asyncio.Event()

    async def leased_forever() -> None:
        async with mgr.lease("a"):
            parked.set()
            await asyncio.Event().wait()  # parked mid-lease until cancelled

    task = asyncio.create_task(leased_forever())
    await parked.wait()
    assert mgr.state("a")["inflight"] == 1

    # Contend the lock with a cold spawn of "b", then cancel like the server does:
    # repeatedly, so every suspension of the dying task gets its own cancellation.
    warm_task = asyncio.create_task(mgr.warm("b"))
    await asyncio.sleep(0.01)  # let warm() take the manager lock
    for _ in range(10):
        task.cancel()
        await asyncio.sleep(0.01)
    await warm_task
    await asyncio.sleep(0.05)  # lock is free — the shielded release completes now

    assert task.done()
    assert mgr.state("a")["inflight"] == 0  # the slot was released, never leaked
