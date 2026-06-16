"""EngineManager — lazy-spawn, port tracking, idle teardown, eviction.

This is "our code" (theygent-stack.md §9): LiteLLM gives routing/fallback/cost/
cache; spawn-on-demand + evict-under-pressure is here. It depends only on the
EngineLauncher and EvictionPolicy seams — it never calls Popen and never bakes in
eviction logic. Reachable (`openai-compatible`) bindings are NOT managed here;
the gateway proxies them directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from theygent_ir import Capabilities, ManagedBinding

from theygent_inference.clock import Clock, RealClock
from theygent_inference.eviction import (
    CountAndPriorityPolicy,
    EvictionPolicy,
    NullProbe,
    PendingLoad,
    ResidentView,
    ResourceBudget,
    ResourceProbe,
)
from theygent_inference.launcher import EngineHandle, EngineLauncher
from theygent_inference.registry import Registry


class NotManagedError(RuntimeError):
    """A reachable binding was handed to the manager; it manages local engines only."""


class NoCapacityError(RuntimeError):
    """No room for a new engine and nothing was evictable (all in-flight)."""


@dataclass
class Upstream:
    """Where the gateway sends an OpenAI-compatible request."""

    api_base: str
    model: str
    api_key: str


@dataclass
class _Resident:
    logical_id: str
    handle: EngineHandle
    binding: ManagedBinding
    last_used: float
    priority: int
    keep_warm: bool
    idle_timeout: float
    inflight: int = 0
    draining: bool = False
    terminated: bool = False


class EngineManager:
    def __init__(
        self,
        registry: Registry,
        launcher: EngineLauncher,
        *,
        clock: Clock | None = None,
        probe: ResourceProbe | None = None,
        policy: EvictionPolicy | None = None,
        max_resident: int = 2,
    ) -> None:
        self._registry = registry
        self._launcher = launcher
        self._clock = clock or RealClock()
        self._probe = probe or NullProbe()
        self._policy = policy or CountAndPriorityPolicy()
        self._max_resident = max_resident
        self._resident: dict[str, _Resident] = {}
        self._lock = asyncio.Lock()
        self._spawn_count = 0

    # ── public lifecycle ────────────────────────────────────────────────

    async def warm(self, logical_id: str) -> None:
        async with self._lock:
            await self._ensure_up_locked(logical_id)

    async def evict(self, logical_id: str) -> None:
        """Free now if idle; if a request is in flight, drain then tear down.
        No-op if the engine is not resident (idempotent)."""
        async with self._lock:
            eng = self._resident.get(logical_id)
            if eng is None or eng.terminated:
                return
            if eng.inflight > 0:
                eng.draining = True
            else:
                await self._terminate_locked(logical_id)

    async def capabilities(self, logical_id: str) -> Capabilities:
        async with self._lock:
            eng = await self._ensure_up_locked(logical_id)
            handle = eng.handle
        # Probe outside the lock — it is an HTTP call to the engine, not a completion.
        return await handle.capabilities()

    @asynccontextmanager
    async def lease(self, logical_id: str) -> AsyncIterator[Upstream]:
        """Ensure the engine is up and hold a slot for one in-flight request.

        While leased the engine is non-evictable; on release a draining engine is
        torn down. The lease spans the whole (possibly streamed) response."""
        eng = await self._acquire(logical_id)
        try:
            yield self._upstream_for(eng)
        finally:
            await self._release(eng)

    async def reap(self) -> None:
        """Tear down idle, non-pinned engines. Driven by the app's background loop
        (or called directly in tests after advancing the clock)."""
        async with self._lock:
            now = self._clock.now()
            stale = [
                lid
                for lid, e in self._resident.items()
                if not e.terminated
                and e.inflight == 0
                and not e.keep_warm
                and (now - e.last_used) >= e.idle_timeout
            ]
            for lid in stale:
                await self._terminate_locked(lid)

    async def shutdown(self) -> None:
        async with self._lock:
            for lid in list(self._resident):
                await self._terminate_locked(lid)

    # ── introspection (management plane) ────────────────────────────────

    def state(self, logical_id: str) -> dict[str, Any]:
        eng = self._resident.get(logical_id)
        if eng is None or eng.terminated:
            return {"resident": False, "inflight": 0, "draining": False}
        return {
            "resident": True,
            "inflight": eng.inflight,
            "draining": eng.draining,
            "baseUrl": eng.handle.base_url,
        }

    @property
    def spawn_count(self) -> int:
        """Total successful launches — lets tests assert laziness and re-spawn."""
        return self._spawn_count

    @property
    def max_resident(self) -> int:
        return self._max_resident

    def resident_engines(self) -> list[dict[str, Any]]:
        """Currently-resident managed engines (the /admin/engines view). We track
        count + priority, not RAM/VRAM bytes (count-based arbitration in M1/M2)."""
        return [
            {
                "logicalId": lid,
                "engine": e.binding.binding,
                "model": e.binding.model,
                "baseUrl": e.handle.base_url,
                "inflight": e.inflight,
                "draining": e.draining,
                "priority": e.priority,
            }
            for lid, e in self._resident.items()
            if not e.terminated
        ]

    # ── internals (all callers hold self._lock unless noted) ────────────

    async def _acquire(self, logical_id: str) -> _Resident:
        async with self._lock:
            eng = await self._ensure_up_locked(logical_id)
            eng.inflight += 1
            eng.last_used = self._clock.now()
            return eng

    async def _release(self, eng: _Resident) -> None:
        async with self._lock:
            eng.inflight -= 1
            eng.last_used = self._clock.now()
            if eng.draining and eng.inflight == 0 and not eng.terminated:
                await self._terminate_locked(eng.logical_id)

    async def _ensure_up_locked(self, logical_id: str) -> _Resident:
        binding = self._registry.require(logical_id)
        if not isinstance(binding, ManagedBinding):
            raise NotManagedError(logical_id)
        eng = self._resident.get(logical_id)
        if eng is not None and not eng.terminated:
            eng.draining = False  # wanted again — cancel any pending drain
            eng.last_used = self._clock.now()
            return eng
        await self._make_room_locked(binding, logical_id)
        handle = await self._launcher.launch(binding)
        self._spawn_count += 1
        eng = _Resident(
            logical_id=logical_id,
            handle=handle,
            binding=binding,
            last_used=self._clock.now(),
            priority=binding.lifecycle.priority,
            keep_warm=binding.lifecycle.keep_warm,
            idle_timeout=binding.lifecycle.idle_timeout_sec,
        )
        self._resident[logical_id] = eng
        return eng

    async def _make_room_locked(self, binding: ManagedBinding, logical_id: str) -> None:
        evictable = [
            ResidentView(e.logical_id, e.priority, e.last_used)
            for e in self._resident.values()
            if not e.terminated and e.inflight == 0 and not e.draining
        ]
        budget = ResourceBudget(self._max_resident, self._probe.over_watermark())
        victims = self._policy.select_victims(
            evictable, PendingLoad(logical_id, binding.lifecycle.priority), budget
        )
        for vid in victims:
            await self._terminate_locked(vid)
        live = sum(1 for e in self._resident.values() if not e.terminated)
        if live >= self._max_resident:
            raise NoCapacityError(
                f"cannot load {logical_id!r}: {live} engine(s) resident, "
                f"max {self._max_resident}, none evictable (all in-flight)"
            )

    async def _terminate_locked(self, logical_id: str) -> None:
        eng = self._resident.pop(logical_id, None)
        if eng is None:
            return
        eng.terminated = True
        await eng.handle.terminate()

    def _upstream_for(self, eng: _Resident) -> Upstream:
        # llama-server serves the OpenAI surface under /v1; no auth locally.
        return Upstream(
            api_base=f"{eng.handle.base_url}/v1",
            model=eng.binding.model,
            api_key="sk-noauth",
        )
