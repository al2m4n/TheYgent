"""Shadow runs: a copy of a request sent, after the real exchange, to the target its decision named
but did not apply — to measure what the counterfactual only estimates. Claude Code never sees one.

A shadow never touches the real exchange. It starts once the exchange has ended (Claude Code holds
its whole answer), nothing awaits it, and its answer goes nowhere but the exchange's record, which
is delivered when the shadow ends — or without it, should the shadow be cut short.

Where a shadow goes:

* a local target (``shadow_local``, on by default): the inference plane, through the local route.
  It says whether the local model's answer would have been used, and costs no API spend;
* a cloud target (``shadow_cloud``, off by default): the Anthropic API, the original bytes with the
  target's model spliced in, as a routed request would be. That is real spend, so it runs only on
  an API key the gateway's operator holds — the organization's key in team mode, the user's own in
  personal mode; never a subscription seat (its plan allowance is not the gateway's to spend) and
  never a team developer's own key — and stops for the day once the day's shadows cost
  ``shadow_cloud_daily_usd``. The count is per gateway instance and kept in memory, so a restart
  starts the day again, and shadows already running may overshoot it. A shadow that meets
  rate-limit pressure (a 429 or 529, or under half of any limit remaining) pauses cloud shadows for
  ten minutes, so they never crowd out real traffic.

A few shadows run at once per kind; beyond that a shadow is skipped, never queued — a shadow is a
sample, not an obligation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

import anyio
import httpx
from theygent_ir.gateway import PolicyConfig, ShadowOutcome, ShadowRun, Target, Usage

from theygent_ai_gateway.costs import PriceBook
from theygent_ai_gateway.local import LocalRoute
from theygent_ai_gateway.observe import (
    Observation,
    ObserveHook,
    ResponseObserver,
    deliver,
)
from theygent_ai_gateway.routing import with_model

log = logging.getLogger(__name__)

#: A local model serves real traffic too; one shadow at a time.
LOCAL_IN_FLIGHT = 1
CLOUD_IN_FLIGHT = 2
#: A shadowed turn generates in full; past this it is given up.
CLOUD_TIMEOUT_SECONDS = 300.0
#: How long cloud shadows stop after meeting rate-limit pressure.
PAUSE_SECONDS = 600.0
#: Below this share of any rate limit remaining, cloud shadows pause.
HEADROOM = 0.5
#: A target that answers these before the response would have its routed request sent back to the
#: requested model: the route was unusable. Other failures (capacity, transport) say nothing about
#: the request.
_UNUSABLE = frozenset({400, 404, 413, 422})
_PRESSURE = frozenset({429, 529})
_LIMIT_PREFIX = "anthropic-ratelimit-"


@dataclass(frozen=True)
class ShadowJob:
    """What a shadow needs from its exchange. ``headers`` are the outbound headers, credential
    included: they live only as long as the shadow, and are never logged."""

    target: Target
    body: bytes
    path: str
    headers: list[tuple[bytes, bytes]]
    #: The request was sent in fast mode, and its copy is too.
    fast: bool = False


def _under_pressure(response: httpx.Response) -> bool:
    if response.status_code in _PRESSURE:
        return True
    headers = response.headers
    for name, value in headers.items():
        lowered = name.lower()
        if not (lowered.startswith(_LIMIT_PREFIX) and lowered.endswith("-limit")):
            continue
        remaining = headers.get(lowered[: -len("-limit")] + "-remaining")
        try:
            limit, left = float(value), float(remaining) if remaining is not None else None
        except ValueError:
            continue
        if left is not None and limit > 0 and left / limit < HEADROOM:
            return True
    return False


class Shadows:
    """Runs shadows in the background and delivers each exchange's observation when its shadow
    ends. ``upstream`` and ``local`` are set while the app runs."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] = lambda: datetime.now(UTC).date(),
    ) -> None:
        self.upstream: httpx.AsyncClient | None = None
        self.local: LocalRoute | None = None
        self._clock = clock
        self._today = today
        self._tasks: set[asyncio.Task[None]] = set()
        self._in_flight = {"local": 0, "anthropic": 0}
        self._day = today()
        self._spent = 0.0
        self._paused_until = 0.0

    @property
    def spent_today(self) -> float:
        if self._today() != self._day:
            self._day, self._spent = self._today(), 0.0
        return self._spent

    def _may_run(self, target: Target, policy: PolicyConfig, cloud_allowed: bool) -> bool:
        if target.kind == "local":
            return (
                policy.shadow_local
                and self.local is not None
                and (self._in_flight["local"] < LOCAL_IN_FLIGHT)
            )
        return (
            policy.shadow_cloud
            and cloud_allowed
            and self.upstream is not None
            and self._in_flight["anthropic"] < CLOUD_IN_FLIGHT
            and self._clock() >= self._paused_until
            and self.spent_today < policy.shadow_cloud_daily_usd
        )

    def start(
        self,
        job: ShadowJob,
        observation: Observation,
        hook: ObserveHook | None,
        *,
        policy: PolicyConfig,
        prices: PriceBook | None,
        cloud_allowed: bool,
    ) -> bool:
        """Run ``job`` in the background, then deliver ``observation`` with its result. ``False``
        when no shadow may run now: nothing was started, and the caller delivers."""
        if not self._may_run(job.target, policy, cloud_allowed):
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        kind = job.target.kind
        self._in_flight[kind] += 1
        task = loop.create_task(self._run(job, observation, hook, prices))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _run(
        self,
        job: ShadowJob,
        observation: Observation,
        hook: ObserveHook | None,
        prices: PriceBook | None,
    ) -> None:
        result: ShadowRun | None = None
        try:
            if job.target.kind == "local":
                result = await self._local(job)
            else:
                result = await self._cloud(job, prices)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("the shadow run failed: %s", type(exc).__name__)
        finally:
            self._in_flight[job.target.kind] -= 1
            deliver(hook, replace(observation, shadow=result) if result else observation)

    async def _local(self, job: ShadowJob) -> ShadowRun:
        local = self.local
        assert local is not None
        started = time.perf_counter()
        attempt = await local.attempt(job.target.model, job.body)
        return ShadowRun(
            target=job.target,
            outcome=attempt.outcome,
            usage=attempt.usage,
            cost_usd=0.0,
            total_ms=int((time.perf_counter() - started) * 1000),
        )

    async def _cloud(self, job: ShadowJob, prices: PriceBook | None) -> ShadowRun:
        upstream = self.upstream
        assert upstream is not None
        body = with_model(job.body, job.target.model)
        if body is None:
            return ShadowRun(target=job.target, outcome="untranslatable", cost_usd=0.0)
        started = time.perf_counter()
        outcome: ShadowOutcome = "failed"
        observer: ResponseObserver | None = None
        request = upstream.build_request("POST", job.path, headers=job.headers, content=body)
        try:
            async with asyncio.timeout(CLOUD_TIMEOUT_SECONDS):
                response = await upstream.send(request, stream=True)
                try:
                    if _under_pressure(response):
                        self._paused_until = self._clock() + PAUSE_SECONDS
                        log.warning("cloud shadows paused: the target is under rate-limit pressure")
                    observer = ResponseObserver(
                        response.headers.get("content-type"),
                        response.headers.get("content-encoding"),
                    )
                    async for chunk in response.aiter_raw():
                        observer.feed(chunk)
                finally:
                    with anyio.CancelScope(shield=True):
                        await response.aclose()
            if response.status_code in _UNUSABLE:
                outcome = "unusable"
            elif 200 <= response.status_code < 300:
                outcome = "answered"
        except (TimeoutError, httpx.HTTPError) as exc:
            log.warning("the shadow got no full answer: %s", type(exc).__name__)
        # What a broken or abandoned answer used is still spent.
        summary = observer.finish() if observer is not None else None
        usage = summary.usage if summary is not None else Usage()
        if outcome == "answered" and summary is not None and summary.error_type is not None:
            outcome = "failed"  # an error event inside a 2xx stream
        cost = prices.cost(job.target.model, usage, fast=job.fast) if prices is not None else None
        if cost is not None:
            self._spent = self.spent_today + cost
        return ShadowRun(
            target=job.target,
            outcome=outcome,
            usage=usage,
            cost_usd=cost,
            total_ms=int((time.perf_counter() - started) * 1000),
        )

    async def stop(self) -> None:
        """Cancel running shadows; each still delivers its exchange's record, without a shadow."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
