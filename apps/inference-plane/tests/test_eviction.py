"""Eviction policy + manager lifecycle — deterministic, no real engine, no sleeps.

These run the manager in a single event loop (pytest-asyncio auto mode) with a
no-op launcher, an injected ManualClock, and an injected probe — so idle eviction
and memory-pressure arbitration are pure, fast, and deterministic.
"""

from __future__ import annotations

import asyncio

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
    # Regression (live wedge): an idle evictable engine coexists with a stuck/draining engine the
    # policy is NOT handed (only evictable engines are passed). With ``resident_total`` = the TRUE
    # count (2) and max 2, the incoming load must evict the idle one. Pre-fix the policy saw only
    # ``len(resident)`` = 1, computed 1+1 <= 2, evicted nothing → the manager then wedged at
    # NoCapacity even though ``idle`` was perfectly evictable.
    policy = CountAndPriorityPolicy()
    resident = [ResidentView("idle", priority=1, last_used=1)]
    budget = ResourceBudget(max_resident=2, resident_total=2)
    assert policy.select_victims(resident, PendingLoad("new", 9), budget) == ["idle"]


def test_policy_no_total_falls_back_to_len_resident() -> None:
    # Backward-compat: callers that don't supply resident_total (default 0) keep the old behavior.
    policy = CountAndPriorityPolicy()
    resident = [ResidentView("a", priority=1, last_used=1)]
    assert policy.select_victims(resident, PendingLoad("x", 1), ResourceBudget(3)) == []


def test_policy_watermark_forces_one_even_with_room() -> None:
    policy = CountAndPriorityPolicy()
    resident = [
        ResidentView("a", priority=5, last_used=1),
        ResidentView("b", priority=1, last_used=1),
    ]
    budget = ResourceBudget(max_resident=5, over_watermark=True)
    assert policy.select_victims(resident, PendingLoad("x", 1), budget) == ["b"]


# ── manager lifecycle (async) ───────────────────────────────────────────


# Plan test #8.
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


# Plan test #9.
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
    # Regression for the live wedge found during the test session: a permanently-draining engine
    # (in-flight forever — e.g. a hung model subprocess) must NOT block loading a new model when a
    # DIFFERENT resident engine is idle/evictable. Pre-fix the policy under-counted the draining
    # engine's slot and the manager raised NoCapacityError; now it evicts the idle engine and loads.
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
