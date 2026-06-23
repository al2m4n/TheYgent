"""``RunStore`` — Postgres-backed run persistence + thread memory (M4 §4/§5).

Replaces M3's in-memory ``RunRegistry``. Every method takes an ``AsyncSession`` handed in
by the caller, who owns the transaction boundary (§1.2): the read path uses a request
session; the run-execution path opens a transaction per logical operation (so the
post-stream pair-write is atomic on its own). Nothing here commits — the caller does.

Domain/persistence split (§1.3): callers see the Pydantic ``Run`` and plain message
tuples; ``RunRow``/``MessageRow``/``ThreadRow`` never leak out.

Thread memory is **mechanical, not smart** (§4): store the turns, replay them verbatim.
No summarization, no token-budget truncation, no vector retrieval — full replay only.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, func, insert, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from theygent_control_plane.mcp import McpServerConfig
from theygent_control_plane.models import (
    AgentRow,
    AgentVersionRow,
    McpServerRow,
    MessageRow,
    RunRow,
    ThreadRow,
)
from theygent_control_plane.run import (
    AgentDetail,
    AgentSummary,
    AgentVersion,
    Run,
    RunStatus,
    StoredVersion,
    ThreadDetail,
    ThreadMessage,
    ThreadSummary,
    new_ulid,
    now,
)


def _to_run(row: RunRow) -> Run:
    """Map a persistence row to the detached domain entity (§1.3)."""
    return Run(
        id=row.id,
        thread_id=row.thread_id,
        # DB columns are untyped str; the lifecycle is constrained at write time.
        status=cast(RunStatus, row.status),
        model=row.model,
        graph_id=row.graph_id,
        graph_version=row.graph_version,
        content_hash=row.content_hash,
        output=row.output,
        created_at=row.created_at,
        updated_at=row.updated_at,
        error=row.error,
    )


class RunStore:
    """Stateless persistence operations against a caller-provided session."""

    async def create_run(
        self,
        session: AsyncSession,
        *,
        model: str,
        thread_id: str | None,
        params: dict | None,
        graph_id: str | None = None,
        graph_version: str | None = None,
        content_hash: str | None = None,
    ) -> Run:
        # Graph fields default to None so the /runs path is unchanged; /graphs/runs passes them
        # (the IR's id/version/contentHash — M5 §4).
        run = Run(
            model=model,
            thread_id=thread_id,
            graph_id=graph_id,
            graph_version=graph_version,
            content_hash=content_hash,
        )
        session.add(
            RunRow(
                id=run.id,
                thread_id=thread_id,
                status=run.status,
                model=model,
                params=params or None,
                graph_id=graph_id,
                graph_version=graph_version,
                content_hash=content_hash,
                error=None,
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
        )
        await session.flush()
        return run

    async def get_run(self, session: AsyncSession, run_id: str) -> Run | None:
        row = await session.get(RunRow, run_id)
        return _to_run(row) if row is not None else None

    async def list_runs(
        self, session: AsyncSession, *, limit: int, before: str | None = None
    ) -> list[Run]:
        """Recent runs, newest first (M8 §1.1) — the cockpit home page.

        Keyset pagination on ``(created_at, id)`` DESC: ``before`` is a run id cursor; rows
        strictly older than that run are returned. A read-only list over already-persisted
        rows — it adds no contract, it surfaces one (M8 §2). An unknown ``before`` id is
        ignored (treated as no cursor), never an error.
        """
        stmt = select(RunRow).order_by(RunRow.created_at.desc(), RunRow.id.desc()).limit(limit)
        if before is not None:
            anchor = await session.get(RunRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(RunRow.created_at, RunRow.id) < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_run(row) for row in rows]

    async def list_threads(
        self, session: AsyncSession, *, limit: int, before: str | None = None
    ) -> list[ThreadSummary]:
        """Recent threads, newest-activity first (M8 §1.3).

        Each summary carries the message count, last-activity instant, and the first user
        message preview (always ``position == 0`` — turns are appended as user/assistant
        pairs, so the very first user turn is position 0). ``before`` is a thread id cursor
        on ``(created_at, id)`` DESC, mirroring ``list_runs``.
        """
        counts = (
            select(
                MessageRow.thread_id.label("thread_id"),
                func.count().label("message_count"),
                func.max(MessageRow.created_at).label("last_message_at"),
            )
            .group_by(MessageRow.thread_id)
            .subquery()
        )
        first_user = (
            select(MessageRow.thread_id.label("thread_id"), MessageRow.content.label("preview"))
            .where(MessageRow.position == 0)
            .subquery()
        )
        stmt = (
            select(
                ThreadRow.id,
                ThreadRow.created_at,
                ThreadRow.updated_at,
                func.coalesce(counts.c.message_count, 0).label("message_count"),
                func.coalesce(counts.c.last_message_at, ThreadRow.updated_at).label(
                    "last_activity"
                ),
                first_user.c.preview,
            )
            .outerjoin(counts, counts.c.thread_id == ThreadRow.id)
            .outerjoin(first_user, first_user.c.thread_id == ThreadRow.id)
            .order_by(ThreadRow.created_at.desc(), ThreadRow.id.desc())
            .limit(limit)
        )
        if before is not None:
            anchor = await session.get(ThreadRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(ThreadRow.created_at, ThreadRow.id) < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).all()
        return [
            ThreadSummary(
                id=row.id,
                created_at=row.created_at,
                last_activity=row.last_activity,
                message_count=int(row.message_count),
                preview=row.preview,
            )
            for row in rows
        ]

    async def get_thread(self, session: AsyncSession, thread_id: str) -> ThreadDetail | None:
        """A thread and its messages in ``position`` order (M8 §1.3 thread detail)."""
        thread = await session.get(ThreadRow, thread_id)
        if thread is None:
            return None
        rows = (
            await session.execute(
                select(
                    MessageRow.id,
                    MessageRow.run_id,
                    MessageRow.role,
                    MessageRow.content,
                    MessageRow.position,
                    MessageRow.created_at,
                )
                .where(MessageRow.thread_id == thread_id)
                .order_by(MessageRow.position)
            )
        ).all()
        return ThreadDetail(
            id=thread.id,
            created_at=thread.created_at,
            updated_at=thread.updated_at,
            messages=[
                ThreadMessage(
                    id=row.id,
                    run_id=row.run_id,
                    role=row.role,
                    content=row.content,
                    position=row.position,
                    created_at=row.created_at,
                )
                for row in rows
            ],
        )

    async def set_status(
        self,
        session: AsyncSession,
        run_id: str,
        status: RunStatus,
        *,
        error: str | None = None,
        output: str | None = None,
    ) -> Run:
        row = await session.get(RunRow, run_id)
        if row is None:  # pragma: no cover - the run is always created first
            raise KeyError(run_id)
        row.status = status
        row.error = error
        # M9 §2.2: persist the final output on completion. Only written when provided (an empty
        # string IS a real terminal output and is stored; None means "don't touch", so a `streaming`
        # or `failed` transition leaves it NULL).
        if output is not None:
            row.output = output
        row.updated_at = now()
        await session.flush()
        return _to_run(row)

    async def fail_if_active(self, session: AsyncSession, run_id: str, reason: str) -> bool:
        """Terminalize one run to ``failed`` ONLY if it's still non-terminal (``created``/
        ``streaming``). Used when a streaming response is cancelled (client disconnected) so the run
        never lingers as a zombie until the next restart's reconcile sweep. The status guard makes
        it race-safe: a run that already reached ``completed``/``failed`` is left untouched, so a
        late cancellation after a successful commit can't overwrite the real outcome. Returns
        whether it changed anything."""
        result = await session.execute(
            update(RunRow)
            .where(RunRow.id == run_id, RunRow.status.in_(("created", "streaming")))
            .values(status="failed", error=reason, updated_at=now())
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def reconcile_orphaned_runs(self, session: AsyncSession) -> int:
        """Sweep runs left non-terminal by a control-plane crash to a terminal ``failed`` state
        (M9 §2.1 / finding F5.2). The in-process M5 walker can't resume an in-flight run, but a
        zombie stuck at ``streaming``/``created`` forever — ``error`` null, ``updated_at`` frozen —
        is unacceptable: it lies about being alive. This is the cheap honest mitigation (not
        resume-after-crash — that's the durable-runtime fork, §4). A distinct reason string keeps
        an interrupted run distinguishable from a real inference failure. Returns the count swept.

        Run once at startup, before serving requests. Bulk UPDATE (no per-row domain mapping): the
        caller owns the transaction (§1.2), exactly like every other store method."""
        result = await session.execute(
            update(RunRow)
            .where(RunRow.status.in_(("created", "streaming")))
            .values(
                status="failed",
                error="interrupted: control-plane restarted while run was in-flight",
                updated_at=now(),
            )
        )
        return cast("CursorResult[Any]", result).rowcount or 0

    async def ensure_thread(self, session: AsyncSession, thread_id: str) -> None:
        """Idempotently create the thread row (existing or new — §4). ON CONFLICT DO
        NOTHING so a follow-up run in an existing thread is a no-op, not a PK violation."""
        ts = now()
        await session.execute(
            pg_insert(ThreadRow)
            .values(id=thread_id, created_at=ts, updated_at=ts, meta=None)
            .on_conflict_do_nothing(index_elements=[ThreadRow.id])
        )

    async def load_thread_messages(
        self, session: AsyncSession, thread_id: str
    ) -> list[dict[str, str]]:
        """Prior turns ordered by ``position`` (the ordering key, never a timestamp), as
        OpenAI-shaped ``{role, content}`` dicts ready to prepend to the new input (§4)."""
        rows = (
            await session.execute(
                select(MessageRow.role, MessageRow.content)
                .where(MessageRow.thread_id == thread_id)
                .order_by(MessageRow.position)
            )
        ).all()
        return [{"role": role, "content": content} for role, content in rows]

    async def append_turn(
        self,
        session: AsyncSession,
        *,
        thread_id: str,
        run_id: str,
        user_content: str,
        assistant_content: str,
    ) -> None:
        """Append the user+assistant pair at the next two positions.

        Called inside the caller's single success transaction (§4), alongside the run's
        ``completed`` update — so the pair and the run state land together or not at all.
        The thread row is locked first (FOR UPDATE) so concurrent runs in the same thread
        can't pick the same ``position`` (the control-plane scales horizontally — §8)."""
        await session.execute(
            select(ThreadRow.id).where(ThreadRow.id == thread_id).with_for_update()
        )
        next_pos = (
            await session.execute(
                select(func.coalesce(func.max(MessageRow.position), -1) + 1).where(
                    MessageRow.thread_id == thread_id
                )
            )
        ).scalar_one()
        ts = now()
        await session.execute(
            insert(MessageRow),
            [
                {
                    "id": new_ulid(),
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "role": "user",
                    "content": user_content,
                    "position": next_pos,
                    "created_at": ts,
                },
                {
                    "id": new_ulid(),
                    "thread_id": thread_id,
                    "run_id": run_id,
                    "role": "assistant",
                    "content": assistant_content,
                    "position": next_pos + 1,
                    "created_at": ts,
                },
            ],
        )


def _to_mcp_config(row: McpServerRow) -> McpServerConfig:
    """Map a persistence row to the manager's domain config (§1.3). ``transport`` defaults to the
    only M7 value (``stdio``) — the column round-trips it but the model pins the Literal."""
    return McpServerConfig(
        command=row.command,
        args=list(row.args or []),
        env=dict(row.env) if row.env else None,
        cwd=row.cwd,
    )


class McpStore:
    """Postgres persistence for MCP server registrations (M9 §2.3 / F6.1).

    Same M4 discipline as ``RunStore``: stateless ops over a caller-provided session, domain/ORM
    split (the manager's ``McpServerConfig`` is the domain shape; ``McpServerRow`` never leaks out).
    Persists the *registration* only — the live connection/process handle is the manager's, lazy
    and never stored. Distinct from the inference-plane model registry, which persists locally to
    the inference plane, never here (the plane boundary — theygent-stack.md §10)."""

    async def upsert_server(
        self, session: AsyncSession, name: str, config: McpServerConfig
    ) -> None:
        """Insert or replace a registration (PUT is idempotent — m7.md §3.2)."""
        ts = now()
        values = {
            "name": name,
            "transport": config.transport,
            "command": config.command,
            "args": config.args,
            "env": config.env,
            "cwd": config.cwd,
            "created_at": ts,
            "updated_at": ts,
        }
        await session.execute(
            pg_insert(McpServerRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[McpServerRow.name],
                set_={
                    "transport": config.transport,
                    "command": config.command,
                    "args": config.args,
                    "env": config.env,
                    "cwd": config.cwd,
                    "updated_at": ts,
                },
            )
        )

    async def delete_server(self, session: AsyncSession, name: str) -> None:
        await session.execute(delete(McpServerRow).where(McpServerRow.name == name))

    async def list_servers(self, session: AsyncSession) -> list[tuple[str, McpServerConfig]]:
        """Every persisted registration, for rehydration on startup (m7.md §3.2 — connections stay
        lazy; this restores the *registry*, not the live connections)."""
        rows = (await session.execute(select(McpServerRow))).scalars().all()
        return [(row.name, _to_mcp_config(row)) for row in rows]


class VersionConflict(Exception):
    """Publishing *different* content under an existing ``(agent_id,version)`` (M11 §1.2).
    Versions are immutable: a deployed/triggered (M12) version must never silently drift, so a
    re-publish under the same coordinate with a different ``content_hash`` is rejected — bump the
    version (§8.2). Re-publishing *identical* content is idempotent (no conflict)."""

    def __init__(self, agent_id: str, version: str, existing_hash: str, new_hash: str) -> None:
        super().__init__(
            f"version {version!r} of agent {agent_id!r} already exists with a different content "
            f"hash ({existing_hash} != {new_hash}); versions are immutable — bump the version"
        )
        self.agent_id = agent_id
        self.version = version


def _stored_version(row: AgentVersionRow) -> StoredVersion:
    return StoredVersion(
        agent_id=row.agent_id,
        version=row.version,
        content_hash=row.content_hash,
        seq=row.seq,
        created_at=row.created_at,
        ir=dict(row.ir),
        view=dict(row.view) if row.view is not None else None,
    )


class AgentStore:
    """Postgres persistence for the agent registry (M11) — the first big consumer of the M4
    conventions after run/thread/message. Same discipline (M4 §1.2/§1.3): stateless ops over a
    caller-provided session (the caller owns the transaction boundary), domain entities out
    (``AgentDetail``/``AgentSummary``/``AgentVersion``/``StoredVersion``), ORM rows never leak.

    The registry stores the canonical, view-stripped §8.2 IR document — it invents no "agent format"
    (M11 §0/§7). The ``content_hash`` it stores is the *walker's* hash for the same IR (computed by
    the one ``theygent_ir.content_hash`` function — §1.1), so a graph the walker ran and an agent
    the registry stored can never disagree. Versions are immutable (§1.2): the ``(agent,version)``
    UNIQUE index is the guard, and ``add_version`` rejects a same-coordinate, different-content
    publish loudly (``VersionConflict``)."""

    async def get_agent(self, session: AsyncSession, agent_id: str) -> AgentRow | None:
        return await session.get(AgentRow, agent_id)

    async def create_agent(self, session: AsyncSession, *, agent_id: str, name: str) -> None:
        """Create the stable agent identity row (§2). The agent ``id`` is the IR document's own §8.2
        ``id`` (§1.1) — the IR carries its identity; the registry persists it under that key. Caller
        has already checked the id is free (→ 409 in the endpoint); the PK is the safety net."""
        ts = now()
        session.add(AgentRow(id=agent_id, name=name, created_at=ts, updated_at=ts))
        await session.flush()

    async def add_version(
        self,
        session: AsyncSession,
        *,
        agent_id: str,
        version: str,
        content_hash: str,
        ir: dict[str, Any],
        view: dict[str, Any] | None,
    ) -> tuple[AgentVersion, bool]:
        """Append an immutable version, returning ``(version_meta, created)``. ``created`` is False
        when the identical content already exists under this ``(agent_id, version)`` — a re-publish
        of the same bytes is idempotent (no conflict, no new row). Publishing *different* content
        under an existing coordinate raises :class:`VersionConflict` (§1.2 immutability).

        The agent row is locked FOR UPDATE first (like ``append_turn`` locks the thread row) so
        concurrent publishes can't pick the same ``seq`` or both insert the same version — the
        control-plane scales horizontally (§1.1). ``seq`` is ``max(seq) + 1`` per agent, the
        monotonic ordering key (M4 §3), starting at 1."""
        # Serialize against concurrent publishes to this agent (seq allocation + the existence
        # check must be atomic — the UNIQUE index is the loud last-resort guard).
        await session.execute(select(AgentRow.id).where(AgentRow.id == agent_id).with_for_update())

        existing = (
            await session.execute(
                select(AgentVersionRow).where(
                    AgentVersionRow.agent_id == agent_id, AgentVersionRow.version == version
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.content_hash == content_hash:
                # Idempotent re-publish of identical content — return the existing version.
                return (
                    AgentVersion(
                        version=existing.version,
                        content_hash=existing.content_hash,
                        seq=existing.seq,
                        created_at=existing.created_at,
                    ),
                    False,
                )
            raise VersionConflict(agent_id, version, existing.content_hash, content_hash)

        next_seq = (
            await session.execute(
                select(func.coalesce(func.max(AgentVersionRow.seq), 0) + 1).where(
                    AgentVersionRow.agent_id == agent_id
                )
            )
        ).scalar_one()
        ts = now()
        row = AgentVersionRow(
            id=new_ulid(),
            agent_id=agent_id,
            version=version,
            content_hash=content_hash,
            ir=ir,
            view=view,
            seq=next_seq,
            created_at=ts,
        )
        session.add(row)
        # Touch the agent's updated_at so the list view's newest-activity reflects a new version.
        agent = await session.get(AgentRow, agent_id)
        if agent is not None:  # pragma: no branch - caller ensured it exists
            agent.updated_at = ts
        await session.flush()
        return (
            AgentVersion(version=version, content_hash=content_hash, seq=next_seq, created_at=ts),
            True,
        )

    async def list_agents(
        self, session: AsyncSession, *, limit: int, before: str | None = None
    ) -> list[AgentSummary]:
        """Saved agents, newest first (M8 §2 list shape) — the cockpit Agents page. Each row carries
        the latest version coordinate (highest ``seq``) + a version count, composed in one query so
        the cockpit needs no second call (no aggregating endpoint — M11 §5). Keyset pagination on
        ``(created_at, id)`` DESC, mirroring ``list_runs``; an unknown ``before`` id is ignored."""
        agg = (
            select(
                AgentVersionRow.agent_id.label("agent_id"),
                func.count().label("version_count"),
                func.max(AgentVersionRow.seq).label("max_seq"),
            )
            .group_by(AgentVersionRow.agent_id)
            .subquery()
        )
        latest = aliased(AgentVersionRow)
        stmt = (
            select(
                AgentRow.id,
                AgentRow.name,
                AgentRow.created_at,
                AgentRow.updated_at,
                func.coalesce(agg.c.version_count, 0).label("version_count"),
                latest.version,
                latest.content_hash,
            )
            .outerjoin(agg, agg.c.agent_id == AgentRow.id)
            .outerjoin(latest, and_(latest.agent_id == AgentRow.id, latest.seq == agg.c.max_seq))
            .order_by(AgentRow.created_at.desc(), AgentRow.id.desc())
            .limit(limit)
        )
        if before is not None:
            anchor = await session.get(AgentRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(AgentRow.created_at, AgentRow.id) < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).all()
        return [
            AgentSummary(
                id=row.id,
                name=row.name,
                created_at=row.created_at,
                updated_at=row.updated_at,
                version_count=int(row.version_count),
                latest_version=row.version,
                latest_content_hash=row.content_hash,
            )
            for row in rows
        ]

    async def get_agent_detail(self, session: AsyncSession, agent_id: str) -> AgentDetail | None:
        """An agent and its versions, newest first by ``seq`` (M11 §3, GET /agents/{id})."""
        agent = await session.get(AgentRow, agent_id)
        if agent is None:
            return None
        rows = (
            (
                await session.execute(
                    select(AgentVersionRow)
                    .where(AgentVersionRow.agent_id == agent_id)
                    .order_by(AgentVersionRow.seq.desc())
                )
            )
            .scalars()
            .all()
        )
        return AgentDetail(
            id=agent.id,
            name=agent.name,
            created_at=agent.created_at,
            updated_at=agent.updated_at,
            versions=[
                AgentVersion(
                    version=r.version,
                    content_hash=r.content_hash,
                    seq=r.seq,
                    created_at=r.created_at,
                )
                for r in rows
            ],
        )

    async def get_version(
        self, session: AsyncSession, agent_id: str, version: str
    ) -> StoredVersion | None:
        """The stored IR (+ view) for one ``(agent_id, version)`` — GET /agents/{id}/versions/{v}
        and the pinned ``version`` invoke (M11 §3)."""
        row = (
            await session.execute(
                select(AgentVersionRow).where(
                    AgentVersionRow.agent_id == agent_id, AgentVersionRow.version == version
                )
            )
        ).scalar_one_or_none()
        return _stored_version(row) if row is not None else None

    async def get_version_by_hash(
        self, session: AsyncSession, agent_id: str, content_hash: str
    ) -> StoredVersion | None:
        """The stored IR for a content-addressed (pinned-by-hash) invoke (M11 §3). ``content_hash``
        is indexed but not unique — two versions can share identical content (same hash); the
        highest-``seq`` match is returned deterministically. Scoped to ``agent_id`` so a hash only
        resolves within its agent."""
        row = (
            await session.execute(
                select(AgentVersionRow)
                .where(
                    AgentVersionRow.agent_id == agent_id,
                    AgentVersionRow.content_hash == content_hash,
                )
                .order_by(AgentVersionRow.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _stored_version(row) if row is not None else None

    async def latest_version(self, session: AsyncSession, agent_id: str) -> StoredVersion | None:
        """The latest published version (highest ``seq`` — M4 §3 ordering), the default an
        unpinned invoke resolves to (M11 §3). ``None`` if the agent has no versions."""
        row = (
            await session.execute(
                select(AgentVersionRow)
                .where(AgentVersionRow.agent_id == agent_id)
                .order_by(AgentVersionRow.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _stored_version(row) if row is not None else None
