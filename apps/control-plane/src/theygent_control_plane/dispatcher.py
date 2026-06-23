"""The in-process schedule dispatcher (M12 §1.2/§3) — the *reversible* half of the trigger layer.

M12's one rule: freeze the trigger *contract*, keep the trigger *mechanism* swappable. The contract
(``Trigger`` + the ``trigger`` table) is the hard-to-reverse seam; THIS file is the throwaway. The
stack doc (§8) says schedules ultimately belong in the durable worker (process type 2), which isn't
built — so the scheduler is a thin asyncio loop over the persisted trigger registry, fronting the
single ``fire(trigger, input)`` seam. M13 re-points firing at the worker without reshaping the
trigger definition, the API, or the persisted rows.

Two deliberate properties:

* **Rehydration is free (§3).** The dispatcher holds no in-memory schedule state — every tick
  re-reads enabled schedules from Postgres. So a fresh control-plane instance after a restart picks
  up every persisted schedule automatically (the F6.1 lesson M9 closed for the MCP registry, here
  for free because the read is per-tick).
* **No backfill, no retry (§0/§4).** ``last_fired_at`` is persisted so the next due instant is
  computed from it: a restart neither double-fires within a window nor replays a long downtime. A
  fired run that fails is recorded honestly (the Run row + a log) and the schedule simply advances —
  M12 builds no retry/backoff engine (that is the M13 durable-runtime fork).

**KNOWN CONSTRAINT — single dispatcher instance only.** The control-plane otherwise scales
horizontally on shared Postgres (stack §8), but this dispatcher does **not**: two instances each run
this loop, both read the same due schedule before either stamps ``last_fired_at``, and both fire →
a **double-fire**. There is no cross-instance claim/lock (an advisory lock or a conditional
"claim the tick" UPDATE would be the cheap fix, but that is M13's concern — its runtime ships
**native durable schedules** that resolve this, m13.md §6). For M12, run the dispatcher on **exactly
one** instance. *Within* one instance there is no double-fire: ``_run_forever`` awaits the entire
``tick`` (every fire AND its ``mark_fired``) before sleeping, so ticks are strictly serial and a
re-tick at the same instant sees the advanced ``last_fired_at`` and is no longer due.

``tick(now)`` is callable directly (the fast suite drives it deterministically); ``run_forever``
is the production background loop, started in the FastAPI lifespan and cancelled at shutdown.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from croniter import croniter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theygent_control_plane.run import Trigger, now
from theygent_control_plane.store import TriggerStore

logger = logging.getLogger("theygent.control_plane")

# The firing seam (§1.2): given a trigger and its resolved input, run the pinned saved agent through
# the existing M5 walker path and return the run outcome. Owned by the app (it needs the gateway,
# the run store, the agent registry); the dispatcher only *calls* it, so M13 can re-point the same
# seam at the durable worker without touching this loop.
FireFn = Callable[[Trigger, Any], Awaitable[Any]]


def cron_is_valid(expr: str) -> bool:
    """Whether ``expr`` is a parseable cron expression (validated at trigger-create time so a bad
    cron is a 400 up front, never a silent never-firing schedule)."""
    return bool(croniter.is_valid(expr))


def is_due(
    expr: str, *, last_fired_at: datetime | None, created_at: datetime, at: datetime
) -> bool:
    """Whether a schedule is due to fire at instant ``at``. Due-ness is computed from the last fire
    (or, if never fired, the trigger's creation) so a schedule fires once per cron window and a long
    downtime does not backfill: the first cron tick strictly after the base must be ``<= at``."""
    base = last_fired_at or created_at
    nxt = croniter(expr, base).get_next(datetime)
    return nxt <= at


class ScheduleDispatcher:
    """Reads enabled schedules from the registry and fires the due ones via the ``fire`` seam."""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        store: TriggerStore,
        fire: FireFn,
        interval_s: float = 5.0,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._store = store
        self._fire = fire
        self._interval_s = interval_s
        self._task: asyncio.Task[None] | None = None

    async def tick(self, at: datetime | None = None) -> list[str]:
        """One scan: fire every enabled schedule due at ``at`` (default: now), returning the ids
        fired. ``last_fired_at`` is stamped AFTER the attempt regardless of the run's outcome — a
        failed fire is recorded honestly and the schedule advances; M12 does not retry (§4). Read
        and bookkeeping use short, separate transactions so one slow/failed fire can't wedge the
        scan or hold a row lock across the agent run."""
        at = at or now()
        async with self._sessionmaker() as session:
            schedules = await self._store.list_enabled_schedules(session)
        fired: list[str] = []
        for trigger in schedules:
            expr = (trigger.config or {}).get("cron")
            if not isinstance(expr, str) or not cron_is_valid(expr):
                # A schedule with no/invalid cron can't be due — skip loudly-once, don't crash the
                # loop (create-time validation should prevent this; the guard keeps the scan safe).
                logger.warning(
                    "trigger.schedule_bad_cron", extra={"trigger_id": trigger.id, "cron": expr}
                )
                continue
            if not is_due(
                expr,
                last_fired_at=trigger.last_fired_at,
                created_at=trigger.created_at,
                at=at,
            ):
                continue
            await self._fire_one(trigger, at)
            fired.append(trigger.id)
        return fired

    async def _fire_one(self, trigger: Trigger, at: datetime) -> None:
        input_value = (trigger.config or {}).get("input")
        try:
            result = await self._fire(trigger, input_value)
            status = result.get("status") if isinstance(result, dict) else "failed"
            logger.info(
                "trigger.fired",
                extra={"trigger_id": trigger.id, "agent_id": trigger.agent_id, "status": status},
            )
        except Exception:  # a fired run must never wedge the dispatcher loop (§4): log and advance.
            logger.exception("trigger.fire_failed", extra={"trigger_id": trigger.id})
        finally:
            # Stamp the fire attempt even on failure so we don't re-fire the same window (no retry).
            async with self._sessionmaker() as session, session.begin():
                await self._store.mark_fired(session, trigger.id, at)

    def start(self) -> None:
        """Launch the background loop (production). The fast suite drives ``tick`` directly and does
        not call this, so scheduled fires stay deterministic in tests."""
        if self._task is None:
            self._task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # never let one bad scan kill the loop.
                logger.exception("trigger.tick_failed")
            await asyncio.sleep(self._interval_s)
