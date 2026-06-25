"""The durable runtime — DBOS lifecycle, the durable queue, the re-pointed ``fire()`` seam, and the
DBOS-native dynamic schedules that replace M12's in-process dispatcher (M13 §4/§5, decisions D2/D3).

DBOS is a **process-global singleton** (one ``DBOS()`` + ``DBOS.launch()`` per process, D2), so this
object is the single owner of that lifecycle. It installs the process-local resources the steps in
``compiler.py`` read, runs the DBOS system-schema migration (``dbos`` schema, kept out of Alembic —
D5), launches DBOS, and exposes:

* ``fire(trigger, input)`` — the re-pointed M12 seam: enqueue ``theygent_run`` on the durable queue
  and await its terminal result (HTTP-invoke + webhook converge here unchanged).
* schedule CRUD — ``upsert_schedule`` / ``pause_schedule`` / ``delete_schedule`` over DBOS dynamic
  schedules, keyed ``trigger-<id>``; theygent's ``trigger`` table stays the source of truth.
* ``reconcile_schedules`` — boot reconciliation: create missing schedules for enabled
  schedule-triggers, drop orphans. This replaces M12's rehydrate-on-boot and lifts the
  single-dispatcher constraint (DBOS schedules dedupe across instances).

Two topologies, one codebase: in the desktop sidecar DBOS launches **in-process** with the API; in
the server / air-gapped topology a separate ``/apps/worker`` process launches the same runtime
against the shared Postgres (m13-dbos.md §5). The Queue + workflow registration are identical.
"""

from __future__ import annotations

import logging
from typing import Any

from dbos import DBOS, Queue
from dbos import run_dbos_database_migrations as _run_dbos_migrations
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from theygent_control_plane.durable import compiler
from theygent_control_plane.durable.bus import DeltaBus
from theygent_control_plane.durable.compiler import (
    DurableResources,
    theygent_run,
    theygent_scheduled_fire,
)
from theygent_control_plane.durable.config import (
    DBOS_SCHEMA,
    build_config,
    system_database_url,
)
from theygent_control_plane.mcp import McpManager
from theygent_control_plane.observability import now_ns
from theygent_control_plane.run import Trigger
from theygent_control_plane.store import AgentStore, RunStore, TriggerStore

logger = logging.getLogger("theygent.control_plane.durable")

# The one durable queue every fired run flows onto (M13 §5). Concurrency is bounded by Postgres and
# ample at theygent scale; ``None`` = unlimited (the thin-M13 default — a concurrency cap is the
# deferred tuning, §5). Created at import so it is registered before ``DBOS.launch()``.
RUN_QUEUE = Queue("theygent")

_SCHEDULE_PREFIX = "trigger-"


def _schedule_name(trigger_id: str) -> str:
    return f"{_SCHEDULE_PREFIX}{trigger_id}"


def run_dbos_migrations(database_url: str) -> None:
    """Create/upgrade the DBOS system schema (``dbos``) on the same Postgres (D5). The ``dbos
    migrate`` equivalent — ONE deploy-time step, not a running service. Idempotent; run before
    launch in the worker bootstrap and the testcontainers fixture (alongside ``alembic upgrade
    head``). Alembic's ``public`` and DBOS's ``dbos`` never touch each other."""
    _run_dbos_migrations(system_database_url(database_url), schema=DBOS_SCHEMA)


class DurableRuntime:
    """Owns the embedded DBOS instance for one process (D2). Build it with the app's resources, then
    ``launch()``; ``shutdown()`` tears DBOS down. ``fire`` and the schedule helpers are the surface
    the control-plane (or the worker) drives."""

    def __init__(
        self,
        *,
        database_url: str,
        gateway: Any,
        mcp: McpManager,
        store: RunStore,
        agents: AgentStore,
        triggers: TriggerStore,
        sessionmaker: async_sessionmaker[AsyncSession],
        bus: DeltaBus | None = None,
        fast_polling: bool = False,
        telemetry: Any = None,
    ) -> None:
        self._database_url = database_url
        self._fast_polling = fast_polling
        self.bus = bus or DeltaBus()
        # M17: the observability wrapper. In the desktop sidecar the control-plane passes its own
        # Telemetry (so durable + interactive spans share one live bus); the standalone worker
        # builds
        # one with its OWN bus. If absent, the durable walk simply emits no spans.
        if telemetry is None:
            from theygent_control_plane.observability import Telemetry

            telemetry = Telemetry(sessionmaker=sessionmaker)
        self.telemetry = telemetry
        # Install the process-local resources the steps read (compiler-global, D2/D6).
        compiler.set_resources(
            DurableResources(
                gateway=gateway,
                mcp=mcp,
                store=store,
                agents=agents,
                triggers=triggers,
                sessionmaker=sessionmaker,
                bus=self.bus,
                telemetry=telemetry,
            )
        )
        self._launched = False

    # ── lifecycle ────────────────────────────────────────────────────────────────

    def migrate(self) -> None:
        run_dbos_migrations(self._database_url)

    def launch(self) -> None:
        """Construct + launch the embedded DBOS instance (idempotent within this object). Runs the
        DBOS system migration first so a fresh deployment/test needs no separate ``dbos migrate``
        step. ``DBOS.launch()`` also recovers any workflows this executor left pending — the
        crash-resume mechanism (§7)."""
        if self._launched:
            return
        self.migrate()
        DBOS(config=build_config(self._database_url, fast_polling=self._fast_polling))
        DBOS.launch()
        self._launched = True
        logger.info("durable.launched")

    def shutdown(self) -> None:
        if self._launched:
            DBOS.destroy()
            self._launched = False
            logger.info("durable.shutdown")

    # ── the re-pointed fire() seam (M13 §4) ──────────────────────────────────────

    async def enqueue_run(
        self,
        agent_ref: dict[str, Any],
        input_value: Any,
        *,
        thread_id: str | None = None,
        trigger_id: str | None = None,
    ) -> Any:
        """Enqueue ``theygent_run`` on the durable queue, returning the DBOS workflow handle (whose
        id IS the run id). The control-plane awaits the handle (the M12 result-or-handle contract)
        or returns the run id for polling."""
        return await RUN_QUEUE.enqueue_async(
            theygent_run, agent_ref, input_value, thread_id, trigger_id
        )

    async def resume(self, run_id: str, input_value: Any) -> None:
        """Deliver the awaited input to a ``waiting`` ``human`` run (M14 §1.1) — the durable side of
        ``POST /runs/{id}/resume``. Maps to ``DBOS.send`` to the workflow whose id IS the run id, on
        the ``human`` topic the node recvs on; the checkpointed workflow resumes from the recv,
        even across a worker restart. DBOS buffers the send per topic, so delivering before the node
        reaches recv (a race) is fine. Wrapped in ``{"input": …}`` so the node can tell a delivered
        value from a timeout (``recv`` → ``None``)."""
        from theygent_control_plane.durable.compiler import HUMAN_TOPIC

        await DBOS.send_async(run_id, {"input": input_value}, HUMAN_TOPIC)

    async def fire(self, trigger: Trigger, input_value: Any) -> dict[str, Any]:
        """The durable replacement for M12's ``fire`` closure (m13-dbos.md §4): enqueue the pinned
        agent's ``theygent_run`` and await its terminal result. A run whose bound inference plane is
        unreachable ends ``failed`` cleanly INSIDE the workflow (§1.4) — never a hang."""
        agent_ref = {
            "agent_id": trigger.agent_id,
            "version": trigger.version,
            "content_hash": trigger.content_hash,
            # M17 §2: stamp the enqueue instant so the workflow can emit the `queue.wait` phase span
            # (enqueue → worker pickup). A workflow ARG (captured once, stable across DBOS replay).
            "enqueued_ns": now_ns(),
        }
        handle = await self.enqueue_run(agent_ref, input_value, trigger_id=trigger.id)
        result = await handle.get_result()
        return result

    # ── DBOS dynamic schedules (M13 §4) ──────────────────────────────────────────

    async def upsert_schedule(self, trigger: Trigger) -> None:
        """Ensure an ENABLED ``schedule`` trigger has a live DBOS schedule (create on first sight,
        resume if paused). theygent's ``trigger`` row stays the source of truth; this just mirrors
        it into a DBOS dynamic schedule named ``trigger-<id>`` firing ``theygent_scheduled_fire``
        with the trigger id as ``context``."""
        name = _schedule_name(trigger.id)
        cron = (trigger.config or {}).get("cron")
        if not isinstance(cron, str):  # not a schedule trigger / no cron — nothing to mirror
            return
        existing = await DBOS.get_schedule_async(name)
        if existing is None:
            await DBOS.create_schedule_async(
                schedule_name=name,
                workflow_fn=theygent_scheduled_fire,
                schedule=cron,
                context=trigger.id,
            )
            logger.info("durable.schedule_created", extra={"trigger_id": trigger.id, "cron": cron})
        else:
            DBOS.resume_schedule(name)

    async def pause_schedule(self, trigger_id: str) -> None:
        """Pause the DBOS schedule for a disabled trigger (it stops firing but is not forgotten —
        re-enabling resumes it). Safe if no schedule exists."""
        if await DBOS.get_schedule_async(_schedule_name(trigger_id)) is not None:
            DBOS.pause_schedule(_schedule_name(trigger_id))

    async def delete_schedule(self, trigger_id: str) -> None:
        """Drop the DBOS schedule for a deleted trigger. Safe if none exists."""
        if await DBOS.get_schedule_async(_schedule_name(trigger_id)) is not None:
            await DBOS.delete_schedule_async(_schedule_name(trigger_id))
            logger.info("durable.schedule_deleted", extra={"trigger_id": trigger_id})

    async def reconcile_schedules(self, enabled_schedule_triggers: list[Trigger]) -> None:
        """Boot reconciliation (M13 §4): make DBOS schedules match the enabled schedule-triggers.
        Create/resume one per enabled trigger; delete any ``trigger-*`` DBOS schedule with no
        matching enabled trigger (a trigger deleted/disabled while this process was down). Replaces
        M12's in-process rehydrate-on-boot and is multi-instance-safe (DBOS dedupes)."""
        desired = {t.id for t in enabled_schedule_triggers}
        for trigger in enabled_schedule_triggers:
            await self.upsert_schedule(trigger)
        for sched in await DBOS.list_schedules_async(schedule_name_prefix=_SCHEDULE_PREFIX):
            name = sched["schedule_name"]
            trigger_id = name[len(_SCHEDULE_PREFIX) :]
            if trigger_id not in desired:
                await DBOS.delete_schedule_async(name)
                logger.info("durable.schedule_orphan_dropped", extra={"trigger_id": trigger_id})
