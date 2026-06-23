"""The ``Run`` domain entity (M3 §3.4) — the API/logic shape, free of storage coupling.

M3 modelled ``Run`` as a clean Pydantic entity that maps 1:1 to a future table; M4 makes
that table real. ``Run`` stays the **domain** shape and is *not* the ORM row (§1.3): it is
mapped to/from ``RunRow`` in ``store.py``. The M3 wire contract is unchanged — ``thread_id``
is the one additive field (optional; ``None`` = a one-shot run, exactly M3's behavior).

The in-memory ``RunRegistry`` is gone: persistence now lives in the Postgres-backed
``RunStore`` (``store.py``), so runs survive a restart and are shared state across
horizontally-scaled control-plane instances (§5/§8).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from ulid import ULID

# M14 §1.1: ``waiting`` is the additive durable-wait status — a run paused at a ``human`` node,
# checkpointed on ``DBOS.recv``. It is NOT ``failed`` and is deliberately EXCLUDED from M9's
# reconciliation sweep (which touches only ``created``/``streaming``), so a run can wait
# indefinitely across a worker restart without being reconciled to ``failed`` (m14.md §1.1 / §4).
RunStatus = Literal["created", "streaming", "waiting", "completed", "failed"]


def now() -> datetime:
    return datetime.now(UTC)


def new_ulid() -> str:
    return str(ULID())


class Run(BaseModel):
    """A single request, from creation to a terminal status.

    ``model`` is the **logical** model id forwarded to inference (never an engine name —
    §3.2). ``thread_id`` is ``None`` for a one-shot run. The status lifecycle is
    ``created`` -> ``streaming`` -> ``completed`` | ``failed``.

    M5 adds three nullable graph fields — a *deliberate* contract extension (m5.md §3.4 /
    §8 step 4), the second after M4's ``thread_id``. They are ``None`` for a non-graph
    ``/runs`` run and populated for a ``/graphs/runs`` run: ``graph_id`` + ``graph_version``
    are the IR's registry coordinate (§8.2), ``content_hash`` its content-addressed identity.
    Recorded now (not gated on yet — §3.3) so the field is correct when the registry consumes it.
    """

    id: str = Field(default_factory=new_ulid)
    thread_id: str | None = None
    status: RunStatus = "created"
    model: str
    graph_id: str | None = None
    graph_version: str | None = None
    content_hash: str | None = None
    # M12 §2: which trigger fired this run (None = interactive). A deliberate, additive contract
    # extension — the third after M4's thread_id and M5's graph fields — giving an unattended run
    # lineage back to the schedule/webhook that fired it.
    trigger_id: str | None = None
    # M9 §2.2: the run's final output, persisted on success so GET /runs/{id} can return it for an
    # un-threaded run too (not only the live SSE stream). None until a terminal output is reached.
    output: str | None = None
    # M14 §1.1: when a run is ``waiting`` at a ``human`` node, the id of that node — the "run
    # bookkeeping" the waiting status needs. ``POST /runs/{id}/resume`` reads it to validate the
    # awaited input against the node's declared schema and to deliver it. NULL when not waiting.
    awaiting_node: str | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)
    # M12 §9 evidence gate: the real terminal-completion instant (None until a real-time terminal
    # transition; stays None for a reconcile-swept zombie). duration = completed_at - created_at;
    # run intervals give system-wide concurrency (m13.md §1.2).
    completed_at: datetime | None = None
    error: str | None = None


# ── Read models for the cockpit list views (M8 §1.1/§1.3) ────────────────────
# Read-only projections the list endpoints return; like ``Run`` they are domain shapes the
# store maps rows onto (§1.3), never ORM rows. They add no write path — the cockpit only
# reads existing persisted state (M8 §2).


class ThreadSummary(BaseModel):
    """One row of the threads list — aggregates over a thread's messages."""

    id: str
    created_at: datetime
    last_activity: datetime
    message_count: int
    # First user message (thread message at ``position == 0``); ``None`` for an empty thread.
    preview: str | None = None


class ThreadMessage(BaseModel):
    id: str
    run_id: str
    role: str  # user | assistant
    content: str
    position: int
    created_at: datetime


class ThreadDetail(BaseModel):
    """A thread with its messages in ``position`` order (thread detail view)."""

    id: str
    created_at: datetime
    updated_at: datetime
    messages: list[ThreadMessage] = Field(default_factory=list)


# ── Agent registry domain entities (M11 §2/§3) ───────────────────────────────
# Like ``Run``, these are domain shapes the store maps rows onto (§1.3) — never ORM rows. The
# registry stores the canonical §8.2 IR document; it invents no "agent format" (M11 §0/§7). The
# IR is the source of truth for ``id`` (the §8.2 agent id) and ``version`` (semver), so a stored
# agent and the Run it produces agree byte-for-byte on ``graph_id``/``graph_version``/``hash``
# (§1.1). ``AgentVersion`` is the lean per-version metadata (no IR payload — it lists fast);
# ``StoredVersion`` carries the full IR + view, returned for a single version and used to resolve
# an invoke-by-reference run.


class AgentVersion(BaseModel):
    """One immutable version's metadata — no IR payload (the list/detail views show coordinates,
    not the document). ``content_hash`` is the §8.2 content-addressed key; ``seq`` is the
    monotonic-per-agent ordering (M4 §3), newest first in the listings."""

    version: str
    content_hash: str
    seq: int
    created_at: datetime


class AgentSummary(BaseModel):
    """One row of the agents list (M8 §2 list shape) — newest agent first. Carries the latest
    version coordinate + a count so the cockpit row reads "name, latest version, hash, count"
    without a second call (client-side composition; no aggregating endpoint — M11 §5)."""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    latest_version: str | None = None
    latest_content_hash: str | None = None
    version_count: int = 0


class AgentDetail(BaseModel):
    """An agent and its versions, newest first (GET /agents/{id} — M11 §3)."""

    id: str
    name: str
    created_at: datetime
    updated_at: datetime
    versions: list[AgentVersion] = Field(default_factory=list)


class StoredVersion(BaseModel):
    """A resolved version with its full IR (+ view) — returned by GET /agents/{id}/versions/{ver}
    and the value an invoke-by-reference run resolves to before walking it (M11 §3/§5). The ``ir``
    the canonical, view-stripped §8.2 document; ``view`` is the stored-but-never-hashed layout."""

    agent_id: str
    version: str
    content_hash: str
    seq: int
    created_at: datetime
    ir: dict[str, Any]
    view: dict[str, Any] | None = None


# ── Trigger domain entity (M12 §1.2/§2) — the deploy primitive ───────────────────
# The frozen trigger *contract*: what fires which saved, pinned agent, on what condition. Like
# ``Run``/``Agent*`` it is a domain shape the store maps ``TriggerRow`` onto (§1.3), never the ORM
# row. The dispatcher that reads it and fires runs is a reversible in-process detail (M13 swaps it
# for the durable worker) — this shape is the hard-to-reverse seam it must not reshape.

TriggerKind = Literal["http", "schedule", "webhook"]


class Trigger(BaseModel):
    """A persisted, non-interactive entry point for a saved agent (M12 §1.2). A trigger always pins
    (§1.1): exactly one of ``version`` / ``content_hash`` — an unattended deploy runs an immutable
    artifact. ``kind`` is the firing mechanism; ``config`` carries the kind-specific knobs (a cron
    expression, a webhook signing secret, an optional input template). ``last_fired_at`` is
    dispatcher bookkeeping, not part of the frozen contract."""

    id: str = Field(default_factory=new_ulid)
    agent_id: str
    version: str | None = None
    content_hash: str | None = None
    kind: TriggerKind
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    last_fired_at: datetime | None = None
    created_at: datetime = Field(default_factory=now)
    updated_at: datetime = Field(default_factory=now)

    def public_dump(self) -> dict[str, Any]:
        """The wire shape, with the webhook ``secret`` redacted (§1.3 / §10 sovereignty: a signing
        secret is the user's credential — list its presence, never echo its value, mirroring how
        ``_mcp_view`` lists ``env`` keys without values)."""
        data = self.model_dump(mode="json")
        config = dict(data.get("config") or {})
        if "secret" in config:
            config["secret"] = "***"
        data["config"] = config
        return data
