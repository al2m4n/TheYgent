"""EngineManager — lazy-spawn, port tracking, idle teardown, eviction.

This is "our code" (spawn-on-demand + evict-under-pressure layer): LiteLLM gives
routing/fallback/cost/cache; spawn-on-demand + evict-under-pressure is here. It depends only on the
EngineLauncher and EvictionPolicy seams — it never calls Popen and never bakes in
eviction logic. Reachable (`openai-compatible`) bindings are NOT managed here;
the gateway proxies them directly.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from theygent_ir import Capabilities, ManagedBinding

from theygent_inference_plane.clock import Clock, RealClock
from theygent_inference_plane.eviction import (
    CountAndPriorityPolicy,
    EvictionPolicy,
    NullProbe,
    PendingLoad,
    ResidentView,
    ResourceBudget,
    ResourceProbe,
)
from theygent_inference_plane.launcher import EngineHandle, EngineLauncher
from theygent_inference_plane.registry import Registry


class NotManagedError(RuntimeError):
    """A reachable binding was handed to the manager; it manages local engines only."""


class NoCapacityError(RuntimeError):
    """No room for a new engine and nothing was evictable (all in-flight)."""


class EngineFailedError(RuntimeError):
    """The model's engine failed and still has requests in flight; a replacement launches
    once they end. Retryable — the caller is told to retry, not that the model is broken."""


@dataclass
class Upstream:
    """Where the gateway sends an OpenAI-compatible request."""

    api_base: str
    model: str
    api_key: str
    # The resident engine is MLX chat (`mlx_lm.server`), which returns Llama's native
    # text tool call as `content` instead of structured `tool_calls`. The gateway applies
    # `tool_parse` normalization ONLY when this is set — never for llama.cpp/hosted (they
    # already emit structured calls; double-parsing is the hazard). The flag is engine
    # *behaviour*, set by the manager from the binding — it carries no engine NAME onto
    # `/v1/*` (the logical-id rule holds).
    needs_tool_parse: bool = False
    # A local engine renders its chat template itself, and templates that honour a reasoning
    # effort (gpt-oss) read it as the template variable ``reasoning_effort`` — the top-level
    # OpenAI field alone never reaches them. Set for managed chat engines; a reachable upstream
    # (often a hosted API that rejects unknown fields) gets the OpenAI field only.
    effort_in_template: bool = False


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
    #: The engine reported itself unusable (or its process died): never handed out again,
    #: torn down once idle, replaced by a fresh launch.
    failed: bool = False


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
        # The last engine failure per logical id, until a replacement launches.
        self._failures: dict[str, dict[str, Any]] = {}

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

    async def mark_failed(self, logical_id: str, reason: str) -> None:
        """Record that the resident engine for ``logical_id`` is unusable — its backend is in
        an error state that only a new process clears (a GPU error), though the process itself
        still runs and answers health checks. The engine is never leased again: torn down now
        if idle, else as soon as its in-flight requests end, and the next request launches a
        replacement. A no-op when nothing is resident."""
        async with self._lock:
            self._failures[logical_id] = {"reason": reason, "at": time.time()}
            eng = self._resident.get(logical_id)
            if eng is None or eng.terminated:
                return
            eng.failed = True
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
            # Shielded: a client disconnecting mid-stream cancels the response task, and
            # the server keeps re-delivering that cancellation at every suspension — an
            # unshielded release dies waiting on a contended manager lock BEFORE the
            # inflight decrement, pinning the engine non-evictable forever. The shield
            # lets the release task run to completion even after the caller is cancelled.
            await asyncio.shield(self._release(eng))

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

    def set_max_resident(self, value: int) -> None:
        """Update the resident ceiling. A *raised* ceiling simply admits more engines; a
        *lowered* one only bites on the next admission — call :meth:`enforce_ceiling` to
        apply it to already-resident engines now."""
        self._max_resident = value

    async def enforce_ceiling(self) -> None:
        """Bring the resident count back under the ceiling NOW.

        Eviction otherwise runs only at admission time (``_make_room_locked``), so a
        ceiling lowered at runtime would not free anything until the next model load.
        Under the manager lock: offer the evictable engines (``inflight == 0``, not
        draining) to the eviction policy with a **no-incoming** budget (nothing is being
        admitted — the policy's admission math reserves a +1 slot that must not apply
        here) and evict the selected excess immediately. If the count is still over
        because the remaining engines are busy, mark the excess draining — lowest
        priority first, then least-recently-used, mirroring the policy's victim order —
        so each tears down when its in-flight work completes (the same drain-on-idle
        semantics as ``:evict`` on a busy engine). An in-flight request is never killed.

        The same sweep re-runs from the release path whenever an engine's last in-flight
        request completes: a lease taken during the drain window cancels that engine's
        pending drain ("wanted again"), so without idle-time re-assertion a single
        well-timed request would leave the plane above a lowered ceiling forever under
        steady reuse.
        """
        async with self._lock:
            await self._enforce_ceiling_locked()

    async def _enforce_ceiling_locked(self) -> None:
        live = [e for e in self._resident.values() if not e.terminated]
        if len(live) <= self._max_resident:
            return
        evictable = [
            ResidentView(e.logical_id, e.priority, e.last_used)
            for e in live
            if e.inflight == 0 and not e.draining
        ]
        budget = ResourceBudget(
            self._max_resident,
            self._probe.over_watermark(),
            # Draining engines free their slots on release — credit them here so the
            # policy frees only the uncovered excess (evicting an idle engine to cover a
            # slot a drain is already about to free would overshoot below the ceiling).
            resident_total=len(live) - sum(1 for e in live if e.draining),
            incoming_count=0,
        )
        # No pending load exists; the placeholder pairs with incoming_count=0 above so
        # the policy frees exactly the excess (select_victims keeps its one signature).
        victims = self._policy.select_victims(evictable, PendingLoad("", 0), budget)
        for vid in victims:
            await self._terminate_locked(vid)
        remaining = [e for e in self._resident.values() if not e.terminated]
        # Already-draining engines free their slots on release; only the shortfall
        # beyond those needs marking.
        excess = len(remaining) - self._max_resident
        to_drain = excess - sum(1 for e in remaining if e.draining)
        if to_drain <= 0:
            return
        candidates = sorted(
            (e for e in remaining if not e.draining),
            key=lambda e: (e.priority, e.last_used),
        )
        for eng in candidates[:to_drain]:
            if eng.inflight > 0:
                eng.draining = True
            else:
                # Belt-and-suspenders: a policy that under-selected leaves an idle
                # engine here — free it now rather than parking it in a draining
                # state nothing would ever release.
                await self._terminate_locked(eng.logical_id)

    # ── introspection (management plane) ────────────────────────────────

    def state(self, logical_id: str) -> dict[str, Any]:
        eng = self._resident.get(logical_id)
        view: dict[str, Any]
        if eng is None or eng.terminated:
            view = {"resident": False, "inflight": 0, "draining": False}
        else:
            view = {
                "resident": True,
                "inflight": eng.inflight,
                "draining": eng.draining,
                "baseUrl": eng.handle.base_url,
            }
        if (failure := self._failures.get(logical_id)) is not None:
            view["lastFailure"] = {
                "reason": failure["reason"],
                "at": datetime.fromtimestamp(failure["at"], UTC).isoformat(),
            }
        return view

    @property
    def spawn_count(self) -> int:
        """Total successful launches — lets tests assert laziness and re-spawn."""
        return self._spawn_count

    @property
    def max_resident(self) -> int:
        return self._max_resident

    def resident_engines(self) -> list[dict[str, Any]]:
        """Currently-resident managed engines (the /admin/engines view). We track
        count + priority, not RAM/VRAM bytes (count-based arbitration; RAM accounting deferred)."""
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
            if eng.inflight == 0:
                # Re-assert the ceiling when an engine goes idle (a no-op at or under it).
                # A lease during the drain window cleared the engine's pending drain
                # ("wanted again"), and admission-time eviction only fires for
                # non-resident loads — without this sweep, a lowered ceiling would stay
                # un-enforced forever while the same engines are steadily reused.
                await self._enforce_ceiling_locked()

    async def _ensure_up_locked(self, logical_id: str) -> _Resident:
        binding = self._registry.require(logical_id)
        if not isinstance(binding, ManagedBinding):
            raise NotManagedError(logical_id)
        eng = self._resident.get(logical_id)
        if eng is not None and not eng.terminated:
            exit_code = eng.handle.exit_code
            if exit_code is not None and not eng.failed:
                # The process died under us (a crash, an OOM kill): its port answers nothing.
                eng.failed = True
                self._failures[logical_id] = {
                    "reason": f"the engine process exited (code {exit_code})",
                    "at": time.time(),
                }
            if not eng.failed:
                eng.draining = False  # wanted again — cancel any pending drain
                eng.last_used = self._clock.now()
                return eng
            if eng.inflight > 0:
                eng.draining = True
                raise EngineFailedError(
                    f"the engine for {logical_id!r} failed and is shutting down; retry shortly"
                )
            await self._terminate_locked(logical_id)
        await self._make_room_locked(binding, logical_id)
        handle = await self._launcher.launch(binding)
        self._spawn_count += 1
        self._failures.pop(logical_id, None)
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
        # Pass the TRUE resident count (incl. in-flight/draining engines the policy can't evict) so
        # the policy frees enough evictable slots. Without this, one permanently-draining engine
        # (inflight never → 0, e.g. a hung model subprocess) makes the policy under-count and refuse
        # to evict an otherwise-idle engine, wedging the whole plane at NoCapacity.
        live = sum(1 for e in self._resident.values() if not e.terminated)
        budget = ResourceBudget(
            self._max_resident, self._probe.over_watermark(), resident_total=live
        )
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
        # MLX chat (mlx_lm.server) does not emit structured tool_calls — flag it so the gateway
        # normalizes Llama's text tool call into the OpenAI shape (tool_parse). llama.cpp/vLLM and
        # reachable upstreams emit structured calls already, so they keep the flag False.
        needs_tool_parse = eng.binding.binding == "mlx" and eng.binding.modality == "chat"
        return Upstream(
            api_base=f"{eng.handle.base_url}/v1",
            model=eng.binding.model,
            api_key="sk-noauth",
            needs_tool_parse=needs_tool_parse,
            effort_in_template=eng.binding.modality in ("chat", "vision"),
        )
