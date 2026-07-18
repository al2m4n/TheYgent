"""The durable runtime — DBOS lifecycle, the durable queue, the ``fire()`` seam, and DBOS-native
dynamic schedules.

DBOS is a **process-global singleton** (one ``DBOS()`` + ``DBOS.launch()`` per process), so this
object is the single owner of that lifecycle. It installs the process-local resources the steps in
``compiler.py`` read, runs the DBOS system-schema migration (``dbos`` schema, kept out of Alembic),
launches DBOS, and exposes:

* ``fire(trigger, input)`` — the fire seam: enqueue ``theygent_run`` on the durable queue
  and await its terminal result (HTTP-invoke + webhook converge here unchanged).
* schedule CRUD — ``upsert_schedule`` / ``pause_schedule`` / ``delete_schedule`` over DBOS dynamic
  schedules, keyed ``trigger-<id>``; theygent's ``trigger`` table stays the source of truth.
* ``reconcile_schedules`` — boot reconciliation: create missing schedules for enabled
  schedule-triggers, drop orphans. Multi-instance-safe: DBOS schedules dedupe across instances.

Two topologies, one codebase: in the desktop sidecar DBOS launches **in-process** with the API; in
the server / air-gapped topology a separate ``/apps/worker`` process launches the same runtime
against the shared Postgres. The Queue + workflow registration are identical.
"""

from __future__ import annotations

import asyncio
import contextlib
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

# The one durable queue every fired run flows onto. Concurrency is bounded by Postgres and
# ample at theygent scale; ``None`` = unlimited (no concurrency cap is applied).
# Created at import so it is registered before ``DBOS.launch()``.
RUN_QUEUE = Queue("theygent")

_SCHEDULE_PREFIX = "trigger-"


class WorkflowGoneError(RuntimeError):
    """A resume was sent to a workflow id the durable engine has no record of — the run row says
    ``waiting`` but the wait can never be satisfied (durable state repointed/reset, or the workflow
    cancelled/dead-lettered). The API maps this to a clean 410 and terminalizes the run instead of
    leaving it stuck ``waiting`` forever. Defined here so the API layer never imports ``dbos``."""


def _schedule_name(trigger_id: str) -> str:
    return f"{_SCHEDULE_PREFIX}{trigger_id}"


def run_dbos_migrations(database_url: str) -> None:
    """Create/upgrade the DBOS system schema (``dbos``) on the same Postgres. The ``dbos
    migrate`` equivalent — ONE deploy-time step, not a running service. Idempotent; run before
    launch in the worker bootstrap and the testcontainers fixture (alongside ``alembic upgrade
    head``). Alembic's ``public`` and DBOS's ``dbos`` never touch each other."""
    _run_dbos_migrations(system_database_url(database_url), schema=DBOS_SCHEMA)


class DurableRuntime:
    """Owns the embedded DBOS instance for one process. Build it with the app's resources, then
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
        tool_auth: Any = None,
        gates: Any = None,
        artifacts: Any = None,
        rag: Any = None,
        executor_id: str | None = None,
    ) -> None:
        self._database_url = database_url
        self._fast_polling = fast_polling
        self._executor_id = executor_id
        self._store = store
        self._sessionmaker = sessionmaker
        self.bus = bus or DeltaBus()
        # Observability wrapper. In the desktop sidecar the control-plane passes its own
        # Telemetry (so durable + interactive spans share one live bus); the standalone worker
        # builds one with its OWN bus — including the opt-in OTLP sink, so an exporter endpoint
        # configured on the worker exports durable-run spans exactly as the API process would.
        # If absent, the durable walk simply emits no spans.
        self._owns_telemetry = telemetry is None
        if telemetry is None:
            from theygent_control_plane.observability import Telemetry
            from theygent_control_plane.observability.otlp import build_otlp_sink

            telemetry = Telemetry(sessionmaker=sessionmaker, otlp_sink=build_otlp_sink())
        self.telemetry = telemetry
        # Install the process-local resources the steps read (compiler-global).
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
                tool_auth=tool_auth,  # durable steps resolve connection auth too
                gates=gates,  # durable gate steps use the same backend
                artifacts=artifacts,  # durable audio steps use the same store
                rag=rag,  # durable rag steps retrieve through the same backend
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
        crash-resume mechanism."""
        if self._launched:
            return
        self.migrate()
        DBOS(
            config=build_config(
                self._database_url,
                fast_polling=self._fast_polling,
                executor_id=self._executor_id,
            )
        )
        DBOS.launch()
        self._launched = True
        logger.info("durable.launched")

    def shutdown(self) -> None:
        if self._launched:
            DBOS.destroy()
            self._launched = False
            logger.info("durable.shutdown")
        # Flush the OTLP exporter only when this runtime BUILT the telemetry (the standalone
        # worker); a shared, app-owned telemetry is flushed by the app's own teardown.
        if self._owns_telemetry and getattr(self.telemetry, "otlp", None) is not None:
            self.telemetry.otlp.shutdown()

    # ── the fire() seam ─────────────────────────────────────────────────────────

    async def enqueue_run(
        self,
        agent_ref: dict[str, Any],
        input_value: Any,
        *,
        session_id: str | None = None,
        trigger_id: str | None = None,
    ) -> Any:
        """Enqueue ``theygent_run`` on the durable queue, returning the DBOS workflow handle (whose
        id IS the run id). The control-plane awaits the handle (the result-or-handle contract)
        or returns the run id for polling."""
        return await RUN_QUEUE.enqueue_async(
            theygent_run, agent_ref, input_value, session_id, trigger_id
        )

    async def resume(self, run_id: str, input_value: Any, *, node_id: str | None = None) -> None:
        """Deliver the awaited input to a ``waiting`` ``human`` run — the durable side of
        ``POST /runs/{id}/resume``. Maps to ``DBOS.send`` to the workflow whose id IS the run id, on
        the topic the node recvs on; the checkpointed workflow resumes from the recv, even across a
        worker restart. DBOS buffers the send per topic, so delivering before the node reaches recv
        (a race) is fine. Wrapped in ``{"input": …}`` so the node can tell a delivered value from a
        timeout (``recv`` → ``None``).

        The topic is per-node (``human:<node_id>``) so a duplicate resume — a double-clicked
        Approve, a client retry — buffers a stray message on the ALREADY-CONSUMED node's topic,
        where it is inert, instead of silently satisfying the run's NEXT human gate with a stale
        payload. ``node_id`` defaults to the run row's recorded ``awaiting_node``.

        Raises :class:`WorkflowGoneError` when the durable engine has no record of the workflow —
        a wait that can never be satisfied must surface as a clean, typed failure, not a 500."""
        from dbos._error import DBOSNonExistentWorkflowError

        from theygent_control_plane.durable.compiler import human_topic

        if node_id is None:
            async with self._sessionmaker() as session:
                run = await self._store.get_run(session, run_id)
            node_id = run.awaiting_node if run is not None else None
        if node_id is None:
            # Every human node recvs on its own `human:<node_id>` topic; a send with no node
            # would buffer forever on the bare prefix nothing listens to. Loud beats inert.
            raise ValueError(
                f"run {run_id!r} records no awaiting node — the resume cannot be delivered"
            )
        try:
            await DBOS.send_async(run_id, {"input": input_value}, human_topic(node_id))
        except DBOSNonExistentWorkflowError as exc:
            raise WorkflowGoneError(str(exc)) from exc

    async def fire(self, trigger: Trigger, input_value: Any) -> dict[str, Any]:
        """Enqueue the pinned agent's ``theygent_run`` and await its terminal result. A run whose
        bound inference plane is unreachable ends ``failed`` cleanly INSIDE the workflow — never a
        hang."""
        agent_ref = {
            "agent_id": trigger.agent_id,
            "version": trigger.version,
            "content_hash": trigger.content_hash,
            # Stamp the enqueue instant so the workflow can emit the `queue.wait` phase span
            # (enqueue → worker pickup). A workflow ARG (captured once, stable across DBOS replay).
            "enqueued_ns": now_ns(),
        }
        handle = await self.enqueue_run(agent_ref, input_value, trigger_id=trigger.id)
        result = await handle.get_result()
        return result

    # ── DBOS dynamic schedules ────────────────────────────────────────────────────

    async def upsert_schedule(self, trigger: Trigger) -> None:
        """Ensure an ENABLED ``schedule`` trigger has a live DBOS schedule that matches the trigger
        row (create on first sight, resume if paused, recreate on a cron edit). theygent's
        ``trigger`` row stays the source of truth; this just mirrors it into a DBOS dynamic schedule
        named ``trigger-<id>`` firing ``theygent_scheduled_fire`` with the trigger id as
        ``context``."""
        name = _schedule_name(trigger.id)
        cron = (trigger.config or {}).get("cron")
        if not isinstance(cron, str):  # not a schedule trigger / no cron — nothing to mirror
            return
        existing = await DBOS.get_schedule_async(name)
        if existing is not None and existing.get("schedule") != cron:
            # The trigger's cron was edited while no durable runtime was live to sync it (e.g. in
            # dispatcher mode) — the stale DBOS schedule would fire on the OLD cadence forever.
            # Recreate so boot reconciliation repairs drift, not just existence.
            await DBOS.delete_schedule_async(name)
            existing = None
        if existing is None:
            try:
                await DBOS.create_schedule_async(
                    schedule_name=name,
                    workflow_fn=theygent_scheduled_fire,
                    schedule=cron,
                    context=trigger.id,
                )
            except Exception as exc:
                # Not an upsert underneath: a sibling process reconciling the same boot window can
                # win the insert. Losing that race is success (the schedule exists) — resume it
                # below rather than crashing this process's startup.
                if "already exists" not in str(exc):
                    raise
                logger.info("durable.schedule_create_lost_race", extra={"trigger_id": trigger.id})
                await asyncio.to_thread(DBOS.resume_schedule, name)
                return
            logger.info("durable.schedule_created", extra={"trigger_id": trigger.id, "cron": cron})
        else:
            # A synchronous Postgres write — keep it off the event loop.
            await asyncio.to_thread(DBOS.resume_schedule, name)

    async def pause_schedule(self, trigger_id: str) -> None:
        """Pause the DBOS schedule for a disabled trigger (it stops firing but is not forgotten —
        re-enabling resumes it). Safe if no schedule exists."""
        if await DBOS.get_schedule_async(_schedule_name(trigger_id)) is not None:
            with contextlib.suppress(Exception):  # deleted by a sibling between check and pause
                await asyncio.to_thread(DBOS.pause_schedule, _schedule_name(trigger_id))

    async def delete_schedule(self, trigger_id: str) -> None:
        """Drop the DBOS schedule for a deleted trigger. Safe if none exists (including one a
        sibling process deleted between the check and the delete)."""
        if await DBOS.get_schedule_async(_schedule_name(trigger_id)) is not None:
            with contextlib.suppress(Exception):
                await DBOS.delete_schedule_async(_schedule_name(trigger_id))
            logger.info("durable.schedule_deleted", extra={"trigger_id": trigger_id})

    async def reconcile_schedules(self, enabled_schedule_triggers: list[Trigger]) -> None:
        """Boot reconciliation: make DBOS schedules match the enabled schedule-triggers.
        Create/resume one per enabled trigger; delete any ``trigger-*`` DBOS schedule with no
        matching enabled trigger (a trigger deleted/disabled while this process was down). Replaces
        the earlier in-process rehydrate-on-boot and is multi-instance-safe (DBOS dedupes)."""
        desired = {t.id for t in enabled_schedule_triggers}
        for trigger in enabled_schedule_triggers:
            await self.upsert_schedule(trigger)
        for sched in await DBOS.list_schedules_async(schedule_name_prefix=_SCHEDULE_PREFIX):
            name = sched["schedule_name"]
            trigger_id = name[len(_SCHEDULE_PREFIX) :]
            if trigger_id not in desired:
                with contextlib.suppress(Exception):  # a sibling's reconcile may win the delete
                    await DBOS.delete_schedule_async(name)
                logger.info("durable.schedule_orphan_dropped", extra={"trigger_id": trigger_id})
