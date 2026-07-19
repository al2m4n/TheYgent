"""``RunStore`` — Postgres-backed run persistence + session memory.

Every method takes an ``AsyncSession`` handed in
by the caller, who owns the transaction boundary: the read path uses a request
session; the run-execution path opens a transaction per logical operation (so the
post-stream pair-write is atomic on its own). Nothing here commits — the caller does.

Domain/persistence split: callers see the Pydantic ``Run`` and plain message
tuples; ``RunRow``/``MessageRow``/``ChatSessionRow`` never leak out.

Session memory is **mechanical, not smart**: store the turns, replay them verbatim.
No summarization, no token-budget truncation, no vector retrieval — full replay only.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, and_, delete, func, insert, or_, select, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from theygent_control_plane.mcp import McpServerConfig
from theygent_control_plane.models import (
    AgentDraftRow,
    AgentIoPolicyRow,
    AgentRow,
    AgentVersionRow,
    BenchCaseRow,
    BenchPresetRow,
    BenchRunRow,
    BenchSuiteRow,
    ChatSessionRow,
    ConnectionRow,
    McpServerRow,
    MessageRow,
    RunRow,
    TriggerRow,
)
from theygent_control_plane.run import (
    AgentDetail,
    AgentDraft,
    AgentSummary,
    AgentVersion,
    BenchCase,
    BenchPreset,
    BenchRun,
    BenchSuite,
    Connection,
    ConnectionKind,
    Run,
    RunStatus,
    SessionDetail,
    SessionMessage,
    SessionSummary,
    StoredVersion,
    Trigger,
    TriggerKind,
    new_ulid,
    now,
)


def params_digest(params: dict[str, Any] | None) -> str:
    """A deterministic identity for a param set — two bench runs differing only in
    ``temperature`` get different digests, so they are distinct results. Canonical JSON (sorted
    no whitespace), the same discipline as the IR ``content_hash`` fixpoint."""
    canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def output_digest(output: str | None) -> str | None:
    """A cheap content identity for the compare diff WITHOUT storing the raw output.
    ``None`` output → ``None`` digest (an empty/absent result is not a content)."""
    if output is None:
        return None
    return "sha256:" + hashlib.sha256(output.encode()).hexdigest()


def _to_run(row: RunRow) -> Run:
    """Map a persistence row to the detached domain entity."""
    return Run(
        id=row.id,
        session_id=row.session_id,
        # DB columns are untyped str; the lifecycle is constrained at write time.
        status=cast(RunStatus, row.status),
        model=row.model,
        graph_id=row.graph_id,
        graph_version=row.graph_version,
        content_hash=row.content_hash,
        trigger_id=row.trigger_id,
        user_id=row.user_id,
        output=row.output,
        awaiting_node=row.awaiting_node,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        error=row.error,
    )


# Terminal statuses get a real-time completion timestamp stamped (evidence gate for duration/cost).
# The startup reconcile sweep does NOT use this path — a zombie's true end time is
# unknown.
_TERMINAL_STATUSES = ("completed", "failed")


class RunStore:
    """Stateless persistence operations against a caller-provided session."""

    async def create_run(
        self,
        session: AsyncSession,
        *,
        model: str,
        session_id: str | None,
        params: dict | None,
        graph_id: str | None = None,
        graph_version: str | None = None,
        content_hash: str | None = None,
        trigger_id: str | None = None,
        user_id: str | None = None,
    ) -> Run:
        # Graph fields default to None so the /runs path is unchanged; /graphs/runs passes them
        # (the IR's id/version/contentHash). trigger_id defaults to None so every
        # interactive path is unchanged; a schedule-/webhook-fired run passes it. user_id is
        # the starting account (None = system-initiated) — a breadcrumb, like trigger_id.
        run = Run(
            model=model,
            session_id=session_id,
            graph_id=graph_id,
            graph_version=graph_version,
            content_hash=content_hash,
            trigger_id=trigger_id,
            user_id=user_id,
        )
        session.add(
            RunRow(
                id=run.id,
                session_id=session_id,
                status=run.status,
                model=model,
                params=params or None,
                graph_id=graph_id,
                graph_version=graph_version,
                content_hash=content_hash,
                trigger_id=trigger_id,
                user_id=user_id,
                error=None,
                runtime="inproc",
                created_at=run.created_at,
                updated_at=run.updated_at,
            )
        )
        await session.flush()
        return run

    async def ensure_run(
        self,
        session: AsyncSession,
        *,
        run_id: str,
        model: str,
        graph_id: str | None = None,
        graph_version: str | None = None,
        content_hash: str | None = None,
        trigger_id: str | None = None,
        user_id: str | None = None,
    ) -> Run:
        """Idempotently create a run row with a CALLER-CHOSEN id. The durable workflow uses
        its own ``DBOS.workflow_id`` as the run id so a resumed run reuses the same row and
        ``GET /runs/{id}`` correlates across a crash/resume. A DBOS step may re-execute if the
        process dies after the row commits but before the step result is journaled (at-least-once),
        so this is ON CONFLICT DO NOTHING — a re-exec is a no-op, never a duplicate-PK crash. The
        session-memory path is unused on the durable ``fire()`` route (session_id is None), so this
        creates a session-less run, exactly like an interactive graph run minus the new-ULID id.
        ``user_id`` is the initiating account (None = a schedule/webhook firing) — the ownership
        breadcrumb the run read/resume gates check, so a durable human-in-the-loop run is only
        resumable by its owner."""
        ts = now()
        await session.execute(
            pg_insert(RunRow)
            .values(
                id=run_id,
                session_id=None,
                status="created",
                model=model,
                params=None,
                graph_id=graph_id,
                graph_version=graph_version,
                content_hash=content_hash,
                trigger_id=trigger_id,
                user_id=user_id,
                error=None,
                # A durable run recovers and resumes across a crash — the startup reconcile
                # sweep must not terminalize it (its fate belongs to the workflow engine).
                runtime="durable",
                created_at=ts,
                updated_at=ts,
            )
            .on_conflict_do_nothing(index_elements=[RunRow.id])
        )
        row = await session.get(RunRow, run_id)
        assert row is not None
        return _to_run(row)

    async def get_run(self, session: AsyncSession, run_id: str) -> Run | None:
        row = await session.get(RunRow, run_id)
        return _to_run(row) if row is not None else None

    async def list_runs(
        self,
        session: AsyncSession,
        *,
        limit: int,
        before: str | None = None,
        for_user: str | None = None,
    ) -> list[Run]:
        """Recent runs, newest first — the cockpit home page.

        Keyset pagination on ``(created_at, id)`` DESC: ``before`` is a run id cursor; rows
        strictly older than that run are returned. A read-only list over already-persisted
        rows. An unknown ``before`` id is ignored (treated as no cursor), never an error.
        ``for_user`` scopes the list to that account's runs plus ownerless (system/pre-auth)
        rows — the non-admin view, mirroring ``list_chat_sessions``; ``None`` returns everything
        (the admin view). Run output is conversation content, so the same ownership boundary
        the /sessions surface enforces applies here.
        """
        stmt = select(RunRow).order_by(RunRow.created_at.desc(), RunRow.id.desc()).limit(limit)
        if for_user is not None:
            stmt = stmt.where(or_(RunRow.user_id.is_(None), RunRow.user_id == for_user))
        if before is not None:
            anchor = await session.get(RunRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(RunRow.created_at, RunRow.id) < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_run(row) for row in rows]

    async def count_runs(self, session: AsyncSession) -> int:
        """Total run count — the exact figure the dashboard overview shows (the list endpoint only
        returns a page window). A single ``COUNT(*)``; read-only."""
        return int((await session.scalar(select(func.count()).select_from(RunRow))) or 0)

    async def count_chat_sessions(self, session: AsyncSession) -> int:
        """Total chat-session count, for the dashboard overview (see :meth:`count_runs`)."""
        return int((await session.scalar(select(func.count()).select_from(ChatSessionRow))) or 0)

    @staticmethod
    def _session_summary_stmt() -> Any:
        """The base summary select (aggregates joined onto the session row) shared by the
        list view and the single-row summary an upsert returns."""
        counts = (
            select(
                MessageRow.session_id.label("session_id"),
                func.count().label("message_count"),
                func.max(MessageRow.created_at).label("last_message_at"),
            )
            .group_by(MessageRow.session_id)
            .subquery()
        )
        first_user = (
            select(MessageRow.session_id.label("session_id"), MessageRow.content.label("preview"))
            .where(MessageRow.position == 0)
            .subquery()
        )
        return (
            select(
                ChatSessionRow.id,
                ChatSessionRow.created_at,
                ChatSessionRow.updated_at,
                ChatSessionRow.meta.label("meta"),
                ChatSessionRow.user_id,
                func.coalesce(counts.c.message_count, 0).label("message_count"),
                func.coalesce(counts.c.last_message_at, ChatSessionRow.updated_at).label(
                    "last_activity"
                ),
                first_user.c.preview,
            )
            .outerjoin(counts, counts.c.session_id == ChatSessionRow.id)
            .outerjoin(first_user, first_user.c.session_id == ChatSessionRow.id)
        )

    @staticmethod
    def _to_session_summary(row: Any) -> SessionSummary:
        return SessionSummary(
            id=row.id,
            created_at=row.created_at,
            last_activity=row.last_activity,
            message_count=int(row.message_count),
            preview=row.preview,
            metadata=row.meta,
            user_id=row.user_id,
        )

    async def list_chat_sessions(
        self,
        session: AsyncSession,
        *,
        limit: int,
        before: str | None = None,
        for_user: str | None = None,
    ) -> list[SessionSummary]:
        """Recent sessions, newest-activity first.

        Each summary carries the message count, last-activity instant, the first user
        message preview (always ``position == 0`` — turns are appended as user/assistant
        pairs, so the very first user turn is position 0), and the client-owned metadata.
        ``before`` is a session id cursor on ``(created_at, id)`` DESC, mirroring ``list_runs``.
        ``for_user`` scopes the list to that account's sessions plus ownerless (pre-auth)
        rows — the non-admin view; ``None`` returns everything (the admin view).
        """
        stmt = (
            self._session_summary_stmt()
            .order_by(ChatSessionRow.created_at.desc(), ChatSessionRow.id.desc())
            .limit(limit)
        )
        if for_user is not None:
            stmt = stmt.where(
                or_(ChatSessionRow.user_id.is_(None), ChatSessionRow.user_id == for_user)
            )
        if before is not None:
            anchor = await session.get(ChatSessionRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(ChatSessionRow.created_at, ChatSessionRow.id)
                    < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).all()
        return [self._to_session_summary(row) for row in rows]

    async def get_chat_session(
        self, session: AsyncSession, session_id: str
    ) -> SessionDetail | None:
        """A session and its messages in ``position`` order."""
        chat_session = await session.get(ChatSessionRow, session_id)
        if chat_session is None:
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
                .where(MessageRow.session_id == session_id)
                .order_by(MessageRow.position)
            )
        ).all()
        return SessionDetail(
            id=chat_session.id,
            created_at=chat_session.created_at,
            updated_at=chat_session.updated_at,
            metadata=chat_session.meta,
            user_id=chat_session.user_id,
            messages=[
                SessionMessage(
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
        # Any non-waiting transition clears the waiting-node breadcrumb — a run is only
        # paused at a human node WHILE waiting; leaving the wait (resuming or terminalizing) clears
        # it so a completed/failed run never carries a stale awaiting_node.
        row.awaiting_node = None
        # Persist the final output on completion. Only written when provided (an empty
        # string IS a real terminal output and is stored; None means "don't touch", so a `streaming`
        # or `failed` transition leaves it NULL).
        if output is not None:
            row.output = output
        ts = now()
        row.updated_at = ts
        # Stamp the real completion instant on a real-time terminal transition so duration
        # (completed_at - created_at) and run-interval concurrency are exact. A non-terminal
        # transition (`streaming`) leaves it NULL; the reconcile sweep uses its own bulk path
        # and never reaches here, so a swept zombie stays NULL (honest unknown end time).
        if status in _TERMINAL_STATUSES:
            row.completed_at = ts
        await session.flush()
        return _to_run(row)

    async def mark_waiting(self, session: AsyncSession, run_id: str, node_id: str) -> Run:
        """Pause a run at a ``human`` node: set status ``waiting`` and record which node
        it is paused at, so ``POST /runs/{id}/resume`` can find the node (its schema + the delivery
        target). Idempotent (a recovered durable step re-marking the same wait is a no-op write). A
        ``waiting`` run is excluded from the reconcile sweep, so it survives a restart while
        paused — the whole point of the durable wait. Does NOT stamp ``completed_at`` (not done)."""
        row = await session.get(RunRow, run_id)
        if row is None:  # pragma: no cover - the run is always created first
            raise KeyError(run_id)
        row.status = "waiting"
        row.awaiting_node = node_id
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
        ts = now()
        result = await session.execute(
            update(RunRow)
            .where(RunRow.id == run_id, RunRow.status.in_(("created", "streaming")))
            # A client-disconnect terminalization is a real-time terminal transition, so stamp
            # completed_at — unlike the startup reconcile sweep, this IS the run's end.
            .values(status="failed", error=reason, updated_at=ts, completed_at=ts)
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def reconcile_orphaned_runs(self, session: AsyncSession) -> int:
        """Sweep runs left non-terminal by a control-plane crash to a terminal ``failed`` state.
        The in-process walker can't resume an in-flight run, but a zombie stuck at
        ``streaming``/``created`` forever — ``error`` null, ``updated_at`` frozen — is
        unacceptable: it lies about being alive. This is the cheap honest mitigation (not
        resume-after-crash — that's the durable-runtime fork). A distinct reason string keeps
        an interrupted run distinguishable from a real inference failure. Returns the count swept.

        Run once at startup, before serving requests. Bulk UPDATE (no per-row domain mapping): the
        caller owns the transaction, exactly like every other store method.

        Does NOT set ``completed_at``: a zombie's real end time is unknown
        (it died with the prior process), so ``now()`` here would be reconcile-time garbage that
        skews the duration/cost evidence. A ``failed`` run with NULL ``completed_at`` reads honestly
        as "crashed, end time unknown", distinct from a run that failed in real time."""
        # The sweep is a WHITELIST of in-flight statuses, so ``waiting`` is excluded for free
        # AND explicitly: a run paused at a ``human`` node is durably checkpointed and may wait for
        # days across restarts — reconciling it to ``failed`` would defeat the durable wait.
        # ``waiting`` is intentionally NOT in this set.
        #
        # Durable runs are excluded the same way: their workflow engine recovers and resumes them
        # after a crash (that is the point of durability), and in the split API/worker topology a
        # restart of THIS process says nothing about a run a healthy sibling is executing. Sweeping
        # one would report a false terminal ``failed`` that the resumed workflow later silently
        # overwrites. Only the in-process interpreter's runs (``inproc`` / legacy NULL) are zombies.
        result = await session.execute(
            update(RunRow)
            .where(
                RunRow.status.in_(("created", "streaming")),
                or_(RunRow.runtime.is_(None), RunRow.runtime != "durable"),
            )
            .values(
                status="failed",
                error="interrupted: control-plane restarted while run was in-flight",
                updated_at=now(),
            )
        )
        return cast("CursorResult[Any]", result).rowcount or 0

    async def ensure_chat_session(
        self, session: AsyncSession, session_id: str, *, user_id: str | None = None
    ) -> None:
        """Idempotently create the session row (existing or new). ON CONFLICT DO
        NOTHING so a follow-up run in an existing session is a no-op, not a PK violation —
        which also means an existing session KEEPS its owner (``user_id`` lands on insert
        only, never reassigns)."""
        ts = now()
        await session.execute(
            pg_insert(ChatSessionRow)
            .values(id=session_id, created_at=ts, updated_at=ts, meta=None, user_id=user_id)
            .on_conflict_do_nothing(index_elements=[ChatSessionRow.id])
        )

    async def upsert_chat_session(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        metadata: dict[str, Any] | None,
        user_id: str | None = None,
    ) -> SessionSummary:
        """The client-write ensure: create the session row, or — when it already exists and
        ``metadata`` was provided — replace its metadata WHOLE (never a deep merge; the value is
        opaque and client-owned). ``metadata=None`` on an existing row changes nothing, so the
        call is an idempotent ensure. ``user_id`` sets the owner ON CREATE ONLY — the conflict
        branch never reassigns ownership. Returns the summary row either way (the caller owns
        the transaction, like every other store method)."""
        ts = now()
        stmt = pg_insert(ChatSessionRow).values(
            id=session_id, created_at=ts, updated_at=ts, meta=metadata, user_id=user_id
        )
        if metadata is not None:
            stmt = stmt.on_conflict_do_update(
                index_elements=[ChatSessionRow.id],
                set_={"metadata": metadata, "updated_at": ts},
            )
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=[ChatSessionRow.id])
        await session.execute(stmt)
        row = (
            await session.execute(
                self._session_summary_stmt().where(ChatSessionRow.id == session_id)
            )
        ).one()
        return self._to_session_summary(row)

    async def delete_chat_session(self, session: AsyncSession, session_id: str) -> bool:
        """Delete a session and its messages, DETACHING (not deleting) its runs — run history
        outlives the conversation it happened in, so ``run.session_id`` is nulled and the run
        rows stay. All three writes ride in the caller's one transaction. Returns whether the
        session row existed."""
        await session.execute(
            update(RunRow).where(RunRow.session_id == session_id).values(session_id=None)
        )
        await session.execute(delete(MessageRow).where(MessageRow.session_id == session_id))
        result = await session.execute(
            delete(ChatSessionRow).where(ChatSessionRow.id == session_id)
        )
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def load_session_messages(
        self, session: AsyncSession, session_id: str
    ) -> list[dict[str, str]]:
        """Prior turns ordered by ``position`` (the ordering key, never a timestamp), as
        OpenAI-shaped ``{role, content}`` dicts ready to prepend to the new input."""
        rows = (
            await session.execute(
                select(MessageRow.role, MessageRow.content)
                .where(MessageRow.session_id == session_id)
                .order_by(MessageRow.position)
            )
        ).all()
        return [{"role": role, "content": content} for role, content in rows]

    async def append_turn(
        self,
        session: AsyncSession,
        *,
        session_id: str,
        run_id: str | None,
        user_content: str,
        assistant_content: str,
    ) -> list[SessionMessage]:
        """Append the user+assistant pair at the next two positions.

        Called inside the caller's single transaction — on the run path alongside the run's
        ``completed`` update (so the pair and the run state land together or not at all), and by
        the client-write turns endpoint with ``run_id=None`` (persisted chat history with no run
        behind it). The session row is locked first (FOR UPDATE) so concurrent writers to the
        same session can't pick the same ``position`` (the control-plane scales horizontally).
        Returns the two appended messages, id/position stamped for the wire — or an EMPTY list
        when the session row no longer exists (see below)."""
        locked = (
            await session.execute(
                select(ChatSessionRow.id).where(ChatSessionRow.id == session_id).with_for_update()
            )
        ).scalar_one_or_none()
        if locked is None:
            # The session was deleted out from under the writer (DELETE /sessions racing an
            # in-flight run in that session). Inserting the pair would hit the
            # message→chat_session FK and blow up the CALLER'S whole transaction — for a run,
            # that transaction also carries the `completed` + output write, so the run's real
            # outcome would be destroyed by a session deletion. The honest posture: the run
            # still completes and persists its output; it simply has no session left to write
            # into. Append nothing, loudly return the empty list (the turns endpoint maps it
            # to 404; the run paths ignore the return value).
            return []
        # The session's updated_at means "last write"; without this it stays frozen at creation
        # even as turns land (the row is already locked above, so the touch is race-free).
        await session.execute(
            update(ChatSessionRow).where(ChatSessionRow.id == session_id).values(updated_at=now())
        )
        next_pos = (
            await session.execute(
                select(func.coalesce(func.max(MessageRow.position), -1) + 1).where(
                    MessageRow.session_id == session_id
                )
            )
        ).scalar_one()
        ts = now()
        pair = [
            {
                "id": new_ulid(),
                "session_id": session_id,
                "run_id": run_id,
                "role": "user",
                "content": user_content,
                "position": next_pos,
                "created_at": ts,
            },
            {
                "id": new_ulid(),
                "session_id": session_id,
                "run_id": run_id,
                "role": "assistant",
                "content": assistant_content,
                "position": next_pos + 1,
                "created_at": ts,
            },
        ]
        await session.execute(insert(MessageRow), pair)
        return [
            SessionMessage(
                id=cast(str, m["id"]),
                run_id=run_id,
                role=cast(str, m["role"]),
                content=cast(str, m["content"]),
                position=cast(int, m["position"]),
                created_at=ts,
            )
            for m in pair
        ]


def _to_mcp_config(row: McpServerRow) -> McpServerConfig:
    """Map a persistence row to the manager's domain config — both transports round-trip
    (an http registration rehydrates with its url/headers, never as a broken stdio one)."""
    transport = "http" if row.transport == "http" else "stdio"
    return McpServerConfig(
        transport=transport,
        command=row.command,
        args=list(row.args or []),
        env=dict(row.env) if row.env else None,
        cwd=row.cwd,
        url=row.url,
        headers=dict(row.headers) if row.headers else None,
    )


class McpStore:
    """Postgres persistence for MCP server registrations.

    Stateless ops over a caller-provided session, domain/ORM split (the manager's
    ``McpServerConfig`` is the domain shape; ``McpServerRow`` never leaks out).
    Persists the *registration* only — the live connection/process handle is the manager's, lazy
    and never stored. Distinct from the inference-plane model registry, which persists locally to
    the inference plane, never here (the plane boundary: only the control-plane's own registrations
    go to Postgres)."""

    async def upsert_server(
        self, session: AsyncSession, name: str, config: McpServerConfig
    ) -> None:
        """Insert or replace a registration (PUT is idempotent)."""
        ts = now()
        values = {
            "name": name,
            "transport": config.transport,
            "command": config.command,
            "args": config.args,
            "env": config.env,
            "cwd": config.cwd,
            "url": config.url,
            "headers": config.headers,
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
                    "url": config.url,
                    "headers": config.headers,
                    "updated_at": ts,
                },
            )
        )

    async def delete_server(self, session: AsyncSession, name: str) -> None:
        await session.execute(delete(McpServerRow).where(McpServerRow.name == name))

    async def list_servers(self, session: AsyncSession) -> list[tuple[str, McpServerConfig]]:
        """Every persisted registration, for rehydration on startup (connections stay
        lazy; this restores the *registry*, not the live connections)."""
        rows = (await session.execute(select(McpServerRow))).scalars().all()
        return [(row.name, _to_mcp_config(row)) for row in rows]


class VersionConflict(Exception):
    """Publishing *different* content under an existing ``(agent_id,version)``.
    Versions are immutable: a deployed/triggered version must never silently drift, so a
    re-publish under the same coordinate with a different ``content_hash`` is rejected — bump the
    version. Re-publishing *identical* content is idempotent (no conflict)."""

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
    """Postgres persistence for the agent registry. Stateless ops over a caller-provided session
    (the caller owns the transaction boundary), domain entities out
    (``AgentDetail``/``AgentSummary``/``AgentVersion``/``StoredVersion``), ORM rows never leak.

    The registry stores the canonical, view-stripped IR document — it invents no "agent format".
    The ``content_hash`` it stores is the *walker's* hash for the same IR (computed by
    the one ``theygent_ir.content_hash`` function), so a graph the walker ran and an agent
    the registry stored can never disagree. Versions are immutable: the ``(agent,version)``
    UNIQUE index is the guard, and ``add_version`` rejects a same-coordinate, different-content
    publish loudly (``VersionConflict``)."""

    async def get_agent(self, session: AsyncSession, agent_id: str) -> AgentRow | None:
        return await session.get(AgentRow, agent_id)

    async def create_agent(self, session: AsyncSession, *, agent_id: str, name: str) -> None:
        """Create the stable agent identity row. The agent ``id`` is the IR document's own
        ``id`` — the IR carries its identity; the registry persists it under that key. Caller
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
        created_at: datetime | None = None,
    ) -> tuple[AgentVersion, bool]:
        """Append an immutable version, returning ``(version_meta, created)``. ``created`` is False
        when the identical content already exists under this ``(agent_id, version)`` — a re-publish
        of the same bytes is idempotent (no conflict, no new row). Publishing *different* content
        under an existing coordinate raises :class:`VersionConflict` (immutability guard).

        The agent row is locked FOR UPDATE first (like ``append_turn`` locks the session row) so
        concurrent publishes can't pick the same ``seq`` or both insert the same version — the
        control-plane scales horizontally. ``seq`` is ``max(seq) + 1`` per agent, the
        monotonic ordering key, starting at 1.

        ``created_at`` is for the bundle-import path only: an imported version keeps its source
        install's instant instead of the restore instant (``seq``, not time, orders versions, so
        a preserved timestamp changes nothing else). Publishes leave it None (= now)."""
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
        ts = created_at or now()
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
        """Saved agents, newest first — the cockpit Agents page. Each row carries
        the latest version coordinate (highest ``seq``) + a version count, composed in one query so
        the cockpit needs no second call. Keyset pagination on
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

    async def count_agents(self, session: AsyncSession) -> int:
        """Total saved-agent count, for the dashboard overview (the list endpoint only returns a
        page window). A single ``COUNT(*)``; read-only."""
        return int((await session.scalar(select(func.count()).select_from(AgentRow))) or 0)

    async def get_agent_detail(self, session: AsyncSession, agent_id: str) -> AgentDetail | None:
        """An agent and its versions, newest first by ``seq`` (GET /agents/{id})."""
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
        and the pinned ``version`` invoke."""
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
        """The stored IR for a content-addressed (pinned-by-hash) invoke. ``content_hash``
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
        """The latest published version (highest ``seq`` — the monotonic ordering key), the default
        an unpinned invoke resolves to. ``None`` if the agent has no versions."""
        row = (
            await session.execute(
                select(AgentVersionRow)
                .where(AgentVersionRow.agent_id == agent_id)
                .order_by(AgentVersionRow.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return _stored_version(row) if row is not None else None

    async def delete_agent(self, session: AsyncSession, agent_id: str) -> list[str] | None:
        """Delete an agent and everything hard-FK'd to it — triggers, its io-policy row, every
        version — then the identity row, all in the caller's ONE transaction (children first;
        none of the FKs carry ON DELETE). Returns the deleted triggers' ids so the route can drop
        their mirrored DBOS schedules (the DELETE /triggers discipline), or ``None`` when the
        agent does not exist.

        Deliberately NOT touched: drafts (``agent_id`` is a breadcrumb with no FK — the 0016
        contract) and runs (``graph_id``/``content_hash`` are lineage by value) — history and
        work-in-progress outlive the registry entry."""
        agent = await session.get(AgentRow, agent_id)
        if agent is None:
            return None
        trigger_ids = list(
            (
                await session.execute(select(TriggerRow.id).where(TriggerRow.agent_id == agent_id))
            ).scalars()
        )
        await session.execute(delete(TriggerRow).where(TriggerRow.agent_id == agent_id))
        await session.execute(delete(AgentIoPolicyRow).where(AgentIoPolicyRow.agent_id == agent_id))
        await session.execute(delete(AgentVersionRow).where(AgentVersionRow.agent_id == agent_id))
        await session.delete(agent)
        await session.flush()
        return trigger_ids


def _to_draft(row: AgentDraftRow) -> AgentDraft:
    return AgentDraft(
        id=row.id,
        agent_id=row.agent_id,
        owner_id=row.owner_id,
        name=row.name,
        node_count=row.node_count,
        created_at=row.created_at,
        updated_at=row.updated_at,
        ir=dict(row.ir or {}),
        view=row.view,
    )


def _derive_draft_meta(ir: dict[str, Any]) -> tuple[str, int]:
    """Derive the list-view labels from a (view-stripped) draft ir: ``name`` falls back
    ``name`` > ``id`` > ``"Untitled"``; ``node_count`` counts ``nodes`` when it is a list, else 0.
    Best-effort labels over an UNVALIDATED document — never a validation of it. Re-derived on
    every write so the row tracks the document."""
    label = ir.get("name") or ir.get("id")
    nodes = ir.get("nodes")
    return (str(label) if label else "Untitled", len(nodes) if isinstance(nodes, list) else 0)


class DraftStore:
    """Postgres persistence for editor drafts — mutable, autosaved, possibly-invalid agent
    graphs, OUTSIDE the registry's immutability invariants (contrast
    ``AgentStore``: no hashing, no version rows, updates in place). Stateless ops over a
    caller-provided session (the caller owns the transaction boundary), domain ``AgentDraft``
    out, ``AgentDraftRow`` never leaks. The store takes the already view-stripped ``ir`` +
    ``view`` (the route owns the wire-side split, like ``_canonical_ir_and_view`` for the
    registry) and derives ``name``/``node_count`` itself so every write path re-derives them
    identically. ``owner_id`` is always written NULL until identity lands."""

    async def create_draft(
        self,
        session: AsyncSession,
        *,
        ir: dict[str, Any],
        view: dict[str, Any] | None,
        agent_id: str | None = None,
    ) -> AgentDraft:
        name, node_count = _derive_draft_meta(ir)
        draft = AgentDraft(agent_id=agent_id, name=name, node_count=node_count, ir=ir, view=view)
        session.add(
            AgentDraftRow(
                id=draft.id,
                agent_id=agent_id,
                owner_id=None,  # the deferred per-user scoping slot — NULL until identity lands
                name=name,
                node_count=node_count,
                ir=ir,
                view=view,
                created_at=draft.created_at,
                updated_at=draft.updated_at,
            )
        )
        await session.flush()
        return draft

    async def get_draft(self, session: AsyncSession, draft_id: str) -> AgentDraft | None:
        row = await session.get(AgentDraftRow, draft_id)
        return _to_draft(row) if row is not None else None

    async def update_draft(
        self,
        session: AsyncSession,
        draft_id: str,
        *,
        ir: dict[str, Any],
        view: dict[str, Any] | None,
    ) -> AgentDraft | None:
        """Replace the draft's document (autosave), re-deriving ``name``/``node_count`` and
        bumping ``updated_at``. ``agent_id`` is immutable after create (the origin breadcrumb
        never re-points). ``None`` on a miss."""
        row = await session.get(AgentDraftRow, draft_id)
        if row is None:
            return None
        row.ir = ir
        row.view = view
        row.name, row.node_count = _derive_draft_meta(ir)
        row.updated_at = now()
        await session.flush()
        return _to_draft(row)

    async def list_drafts(
        self,
        session: AsyncSession,
        *,
        limit: int,
        before: str | None = None,
        agent_id: str | None = None,
    ) -> list[AgentDraft]:
        """Drafts, most recently EDITED first — a draft is mutated in place on every autosave,
        so recency means ``updated_at``, not creation (ordering by ``created_at`` would freeze a
        draft's position at birth and let a truncated page hide the row the user just touched).
        Keyset pagination on ``(updated_at, id)`` DESC; the anchor comparison uses the same key
        as the ORDER BY. The key is mutable, so a draft edited mid-pagination can be skipped or
        repeated across pages — tolerable for this surface, which fetches one recent window. An
        unknown ``before`` id is ignored. ``agent_id`` narrows to the drafts editing one
        registry agent (exact match)."""
        stmt = (
            select(AgentDraftRow)
            .order_by(AgentDraftRow.updated_at.desc(), AgentDraftRow.id.desc())
            .limit(limit)
        )
        if agent_id is not None:
            stmt = stmt.where(AgentDraftRow.agent_id == agent_id)
        if before is not None:
            anchor = await session.get(AgentDraftRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(AgentDraftRow.updated_at, AgentDraftRow.id)
                    < (anchor.updated_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_draft(row) for row in rows]

    async def delete_draft(self, session: AsyncSession, draft_id: str) -> bool:
        result = await session.execute(delete(AgentDraftRow).where(AgentDraftRow.id == draft_id))
        return bool(cast("CursorResult[Any]", result).rowcount)


def _to_trigger(row: TriggerRow) -> Trigger:
    return Trigger(
        id=row.id,
        agent_id=row.agent_id,
        version=row.version,
        content_hash=row.content_hash,
        kind=cast(TriggerKind, row.kind),
        config=dict(row.config or {}),
        enabled=row.enabled,
        last_fired_at=row.last_fired_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class TriggerStore:
    """Postgres persistence for the trigger registry — the deploy primitive's durable seam.

    Stateless ops over a caller-provided session, domain ``Trigger`` out, ``TriggerRow`` never
    leaks. Persisting the *definition* (not just the dispatcher state) means a schedule is never
    lost on restart. The dispatcher rehydrates by simply re-reading these rows each tick, so a
    fresh control-plane instance picks up every persisted schedule with no in-memory restore."""

    async def create(
        self,
        session: AsyncSession,
        *,
        agent_id: str,
        kind: TriggerKind,
        version: str | None,
        content_hash: str | None,
        config: dict[str, Any],
        enabled: bool,
        trigger_id: str | None = None,
        created_at: datetime | None = None,
    ) -> Trigger:
        # ``trigger_id``/``created_at`` are for the bundle-import path only: a restored trigger
        # keeps its source install's identity (so a re-import skips it) and instant. The API
        # create path leaves them None (fresh ULID, now).
        overrides: dict[str, Any] = {}
        if trigger_id is not None:
            overrides["id"] = trigger_id
        if created_at is not None:
            overrides["created_at"] = created_at
            overrides["updated_at"] = created_at
        trigger = Trigger(
            agent_id=agent_id,
            kind=kind,
            version=version,
            content_hash=content_hash,
            config=config,
            enabled=enabled,
            **overrides,
        )
        session.add(
            TriggerRow(
                id=trigger.id,
                agent_id=agent_id,
                version=version,
                content_hash=content_hash,
                kind=kind,
                config=config,
                enabled=enabled,
                last_fired_at=None,
                created_at=trigger.created_at,
                updated_at=trigger.updated_at,
            )
        )
        await session.flush()
        return trigger

    async def get(self, session: AsyncSession, trigger_id: str) -> Trigger | None:
        row = await session.get(TriggerRow, trigger_id)
        return _to_trigger(row) if row is not None else None

    async def list_triggers(
        self, session: AsyncSession, *, limit: int, before: str | None = None
    ) -> list[Trigger]:
        """Triggers, newest first. Keyset pagination on ``(created_at, id)``
        DESC, mirroring ``list_runs``/``list_agents``; an unknown ``before`` id is ignored."""
        stmt = (
            select(TriggerRow)
            .order_by(TriggerRow.created_at.desc(), TriggerRow.id.desc())
            .limit(limit)
        )
        if before is not None:
            anchor = await session.get(TriggerRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(TriggerRow.created_at, TriggerRow.id) < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_trigger(row) for row in rows]

    async def update(
        self,
        session: AsyncSession,
        trigger_id: str,
        *,
        enabled: bool | None = None,
        config: dict[str, Any] | None = None,
    ) -> Trigger | None:
        """Enable/disable and/or edit config (PATCH). The pin and kind are immutable here:
        editing them would change *which immutable artifact* an unattended deploy runs, so a re-pin
        is a new trigger, not a mutation (the immutability discipline, applied to triggers)."""
        row = await session.get(TriggerRow, trigger_id)
        if row is None:
            return None
        if enabled is not None:
            row.enabled = enabled
        if config is not None:
            row.config = config
        row.updated_at = now()
        await session.flush()
        return _to_trigger(row)

    async def delete(self, session: AsyncSession, trigger_id: str) -> bool:
        result = await session.execute(delete(TriggerRow).where(TriggerRow.id == trigger_id))
        return bool(cast("CursorResult[Any]", result).rowcount)

    async def list_enabled_schedules(self, session: AsyncSession) -> list[Trigger]:
        """Every enabled ``schedule`` trigger — what the dispatcher scans each tick. Read
        fresh per tick (no in-memory cache), so a new instance after a restart sees them all and a
        disabled trigger drops out immediately."""
        rows = (
            (
                await session.execute(
                    select(TriggerRow).where(
                        TriggerRow.kind == "schedule", TriggerRow.enabled.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        return [_to_trigger(row) for row in rows]

    async def mark_fired(self, session: AsyncSession, trigger_id: str, fired_at: Any) -> None:
        """Stamp ``last_fired_at`` after a schedule fires. The dispatcher computes the next
        due instant from this, so persisting it makes a restart resume cleanly — neither
        double-firing within a window nor backfilling a long downtime."""
        await session.execute(
            update(TriggerRow)
            .where(TriggerRow.id == trigger_id)
            .values(last_fired_at=fired_at, updated_at=now())
        )


# ── Connection store — the tool/MCP auth seam ────────────────────────────────────────────────────


def _to_connection(row: ConnectionRow) -> Connection:
    return Connection(
        id=row.id,
        name=row.name,
        kind=cast(ConnectionKind, row.kind),
        config=dict(row.config or {}),
        secret_ref=row.secret_ref,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ConnectionStore:
    """Postgres persistence for the connection registry — the tool/MCP auth seam.

    Stateless ops over a caller-provided session, domain ``Connection`` out, ``ConnectionRow``
    never leaks. The row stores NON-SECRET config + a ``secret_ref``; the secret material itself
    lives in the ``secret`` table (``SecretStore``), written/rotated/deleted by the route
    alongside the connection in the SAME transaction. This store knows nothing about encryption —
    it only persists the ref (the seam: connection = config + a pointer, never the secret)."""

    async def create(
        self,
        session: AsyncSession,
        *,
        name: str,
        kind: ConnectionKind,
        config: dict[str, Any],
        secret_ref: str | None,
        enabled: bool = True,
    ) -> Connection:
        conn = Connection(
            name=name, kind=kind, config=config, secret_ref=secret_ref, enabled=enabled
        )
        session.add(
            ConnectionRow(
                id=conn.id,
                name=name,
                kind=kind,
                config=config,
                secret_ref=secret_ref,
                enabled=enabled,
                created_at=conn.created_at,
                updated_at=conn.updated_at,
            )
        )
        await session.flush()
        return conn

    async def get(self, session: AsyncSession, connection_id: str) -> Connection | None:
        row = await session.get(ConnectionRow, connection_id)
        return _to_connection(row) if row is not None else None

    async def list_connections(
        self, session: AsyncSession, *, limit: int, before: str | None = None
    ) -> list[Connection]:
        """Connections, newest first. Keyset pagination on ``(created_at, id)``
        DESC, mirroring ``list_triggers``/``list_agents``; an unknown ``before`` id is ignored."""
        stmt = (
            select(ConnectionRow)
            .order_by(ConnectionRow.created_at.desc(), ConnectionRow.id.desc())
            .limit(limit)
        )
        if before is not None:
            anchor = await session.get(ConnectionRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(ConnectionRow.created_at, ConnectionRow.id)
                    < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_connection(row) for row in rows]

    async def update(
        self,
        session: AsyncSession,
        connection_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        secret_ref: str | None = None,
        set_secret_ref: bool = False,
        enabled: bool | None = None,
    ) -> Connection | None:
        """Edit non-secret config / name / enabled, and optionally re-point ``secret_ref`` (a secret
        rotation keeps the SAME ref, so ``set_secret_ref`` is only used when clearing/attaching a
        secret, not on rotation — rotating a secret keeps the same ref so every agent's
        ``content_hash`` is stable). ``kind`` is immutable (a different kind is a different
        connection)."""
        row = await session.get(ConnectionRow, connection_id)
        if row is None:
            return None
        if name is not None:
            row.name = name
        if config is not None:
            row.config = config
        if set_secret_ref:
            row.secret_ref = secret_ref
        if enabled is not None:
            row.enabled = enabled
        row.updated_at = now()
        await session.flush()
        return _to_connection(row)

    async def delete(self, session: AsyncSession, connection_id: str) -> Connection | None:
        """Delete the connection, returning the deleted domain object so the caller can also delete
        its secret (the route owns the secret lifecycle — connection + secret in one transaction).
        ``None`` if it did not exist."""
        row = await session.get(ConnectionRow, connection_id)
        if row is None:
            return None
        conn = _to_connection(row)
        await session.delete(row)
        await session.flush()
        return conn


# ── Bench store ──────────────────────────────────────────────────────────────────────────────────


def _to_bench_case(row: BenchCaseRow) -> BenchCase:
    return BenchCase(
        id=row.id,
        input=(row.input or {}).get("value") if isinstance(row.input, dict) else row.input,
        expected=(row.expected or {}).get("value") if isinstance(row.expected, dict) else None,
        assertion=cast("Any", row.assertion),
        assertion_config=row.assertion_config or {},
        seq=row.seq,
        created_at=row.created_at,
    )


def _to_bench_suite(row: BenchSuiteRow, cases: list[BenchCaseRow]) -> BenchSuite:
    return BenchSuite(
        id=row.id,
        name=row.name,
        target_kind=cast("Any", row.target_kind),
        modality=row.modality,
        logical_id=row.logical_id,
        binding=row.binding,
        agent_id=row.agent_id,
        version=row.version,
        content_hash=row.content_hash,
        cases=[_to_bench_case(c) for c in cases],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _to_bench_run(row: BenchRunRow) -> BenchRun:
    return BenchRun(
        id=row.id,
        target_kind=cast("Any", row.target_kind),
        modality=row.modality,
        logical_id=row.logical_id,
        model_ref=row.model_ref,
        binding=row.binding,
        params=row.params,
        params_digest=row.params_digest,
        agent_id=row.agent_id,
        version=row.version,
        content_hash=row.content_hash,
        metrics=row.metrics or {},
        output_digest=row.output_digest,
        capture_ref=row.capture_ref,
        suite_id=row.suite_id,
        case_id=row.case_id,
        assertion=row.assertion,
        assertion_passed=row.assertion_passed,
        run_id=row.run_id,
        label=row.label,
        created_at=row.created_at,
    )


def _to_bench_preset(row: BenchPresetRow) -> BenchPreset:
    return BenchPreset(
        id=row.id,
        name=row.name,
        modality=row.modality,
        logical_id=row.logical_id,
        params=row.params or {},
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class BenchStore:
    """Postgres persistence for the bench store. Stateless ops over a caller-provided session,
    domain shapes out, ORM rows never leak.
    Metrics + digests only by default; raw payloads are NEVER journaled here — capture
    is opt-in and a LOCAL reference (``capture_ref``), never a blob in the hot table."""

    # ── suites + cases ───────────────────────────────────────────────────
    async def create_suite(self, session: AsyncSession, suite: BenchSuite) -> BenchSuite:
        session.add(
            BenchSuiteRow(
                id=suite.id,
                name=suite.name,
                target_kind=suite.target_kind,
                modality=suite.modality,
                logical_id=suite.logical_id,
                binding=suite.binding,
                agent_id=suite.agent_id,
                version=suite.version,
                content_hash=suite.content_hash,
                created_at=suite.created_at,
                updated_at=suite.updated_at,
            )
        )
        # Flush the parent before the cases so the bench_case FK to bench_suite is satisfied.
        await session.flush()
        for i, case in enumerate(suite.cases):
            # input/expected wrapped in {value: …} so a scalar, list, or object all round-trip in a
            # JSONB column without a separate type tag.
            session.add(
                BenchCaseRow(
                    id=case.id,
                    suite_id=suite.id,
                    input={"value": case.input},
                    expected=None if case.expected is None else {"value": case.expected},
                    assertion=case.assertion,
                    assertion_config=case.assertion_config or None,
                    seq=i,
                    created_at=case.created_at,
                )
            )
        await session.flush()
        return suite

    async def get_suite(self, session: AsyncSession, suite_id: str) -> BenchSuite | None:
        row = await session.get(BenchSuiteRow, suite_id)
        if row is None:
            return None
        cases = (
            (
                await session.execute(
                    select(BenchCaseRow)
                    .where(BenchCaseRow.suite_id == suite_id)
                    .order_by(BenchCaseRow.seq.asc())
                )
            )
            .scalars()
            .all()
        )
        return _to_bench_suite(row, list(cases))

    async def list_suites(
        self, session: AsyncSession, *, limit: int, before: str | None = None
    ) -> list[BenchSuite]:
        """Suites newest-first (keyset pagination), without their cases (the list view shows
        coordinates; GET one suite to load its cases)."""
        stmt = (
            select(BenchSuiteRow)
            .order_by(BenchSuiteRow.created_at.desc(), BenchSuiteRow.id.desc())
            .limit(limit)
        )
        if before is not None:
            anchor = await session.get(BenchSuiteRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(BenchSuiteRow.created_at, BenchSuiteRow.id)
                    < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_bench_suite(row, []) for row in rows]

    # ── results ──────────────────────────────────────────────────────────
    async def record_run(self, session: AsyncSession, run: BenchRun) -> BenchRun:
        """Record one result. The caller has already set ``params_digest`` / ``output_digest`` (via
        the module helpers) and decided whether ``capture_ref`` is set (opt-in, local)."""
        session.add(
            BenchRunRow(
                id=run.id,
                target_kind=run.target_kind,
                modality=run.modality,
                logical_id=run.logical_id,
                model_ref=run.model_ref,
                binding=run.binding,
                params=run.params,
                params_digest=run.params_digest,
                agent_id=run.agent_id,
                version=run.version,
                content_hash=run.content_hash,
                metrics=run.metrics,
                output_digest=run.output_digest,
                capture_ref=run.capture_ref,
                suite_id=run.suite_id,
                case_id=run.case_id,
                assertion=run.assertion,
                assertion_passed=run.assertion_passed,
                run_id=run.run_id,
                label=run.label,
                created_at=run.created_at,
            )
        )
        await session.flush()
        return run

    async def get_run(self, session: AsyncSession, bench_run_id: str) -> BenchRun | None:
        row = await session.get(BenchRunRow, bench_run_id)
        return _to_bench_run(row) if row is not None else None

    async def list_runs(
        self,
        session: AsyncSession,
        *,
        limit: int,
        before: str | None = None,
        logical_id: str | None = None,
        agent_id: str | None = None,
        suite_id: str | None = None,
        case_id: str | None = None,
    ) -> list[BenchRun]:
        """Results newest-first, optionally filtered by target/suite/case so a regression
        across versions or a per-case history is one query."""
        stmt = (
            select(BenchRunRow)
            .order_by(BenchRunRow.created_at.desc(), BenchRunRow.id.desc())
            .limit(limit)
        )
        if logical_id is not None:
            stmt = stmt.where(BenchRunRow.logical_id == logical_id)
        if agent_id is not None:
            stmt = stmt.where(BenchRunRow.agent_id == agent_id)
        if suite_id is not None:
            stmt = stmt.where(BenchRunRow.suite_id == suite_id)
        if case_id is not None:
            stmt = stmt.where(BenchRunRow.case_id == case_id)
        if before is not None:
            anchor = await session.get(BenchRunRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(BenchRunRow.created_at, BenchRunRow.id) < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_bench_run(row) for row in rows]

    # ── presets ──────────────────────────────────────────────────────────
    async def create_preset(self, session: AsyncSession, preset: BenchPreset) -> BenchPreset:
        session.add(
            BenchPresetRow(
                id=preset.id,
                name=preset.name,
                modality=preset.modality,
                logical_id=preset.logical_id,
                params=preset.params,
                created_at=preset.created_at,
                updated_at=preset.updated_at,
            )
        )
        await session.flush()
        return preset

    async def list_presets(
        self,
        session: AsyncSession,
        *,
        limit: int,
        before: str | None = None,
        modality: str | None = None,
        logical_id: str | None = None,
    ) -> list[BenchPreset]:
        stmt = (
            select(BenchPresetRow)
            .order_by(BenchPresetRow.created_at.desc(), BenchPresetRow.id.desc())
            .limit(limit)
        )
        if modality is not None:
            stmt = stmt.where(BenchPresetRow.modality == modality)
        if logical_id is not None:
            stmt = stmt.where(BenchPresetRow.logical_id == logical_id)
        if before is not None:
            anchor = await session.get(BenchPresetRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(BenchPresetRow.created_at, BenchPresetRow.id)
                    < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt)).scalars().all()
        return [_to_bench_preset(row) for row in rows]

    async def get_preset(self, session: AsyncSession, preset_id: str) -> BenchPreset | None:
        row = await session.get(BenchPresetRow, preset_id)
        return _to_bench_preset(row) if row is not None else None

    async def delete_preset(self, session: AsyncSession, preset_id: str) -> bool:
        result = await session.execute(delete(BenchPresetRow).where(BenchPresetRow.id == preset_id))
        return bool(cast("CursorResult[Any]", result).rowcount)
