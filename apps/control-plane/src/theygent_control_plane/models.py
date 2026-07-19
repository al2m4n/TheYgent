"""SQLAlchemy ORM rows — the persistence shape, kept SEPARATE from the domain.

These are *rows*, not the API/logic entities. The domain ``Run`` (``run.py``) is a clean
Pydantic model mapped to/from ``RunRow``: the wire contract must not be welded to
the schema, so each can evolve without breaking the other — the same seam-thinking as
IR-vs-React-Flow. A thin mapping layer (``store.py``) is the cost; decoupling is the payoff.

``Base.metadata`` here is Alembic's ``target_metadata``, but the schema is owned by the
hand-written migration — **never** ``create_all``, including in tests. Keep the
migration and these models in lock-step by hand.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# TIMESTAMPTZ everywhere: tz-aware instants so ordering/age is unambiguous across
# horizontally-scaled instances. Ordering itself keys off explicit columns (position /
# the FK graph), never a timestamp — clock skew across instances makes those unreliable.
_TZ = TIMESTAMP(timezone=True)


class ChatSessionRow(Base):
    # ``chat_session``, not ``session`` — the bare name collides with SQLAlchemy's
    # ``AsyncSession``/``session`` convention everywhere a store method takes both.
    __tablename__ = "chat_session"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)
    # Reserved name on Declarative (``Base.metadata``), so the attribute is ``meta`` while
    # the column stays ``metadata``. Opaque JSONB the client owns (kind/model/title …);
    # the control plane stores and returns it, never interprets it.
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
    # Who owns this conversation. A breadcrumb (plain string, no FK): sessions must outlive
    # the account that created them, and pre-auth rows are NULL (visible to every signed-in
    # user — they predate ownership). List/read endpoints filter on it; admins see all.
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)


class RunRow(Base):
    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # NULL = one-shot run with no session/memory. Sessions are opt-in.
    # ON DELETE not set here: deleting a session nulls this FK explicitly (the run
    # history outlives the conversation it happened in).
    session_id: Mapped[str | None] = mapped_column(ForeignKey("chat_session.id"), nullable=True)
    status: Mapped[str] = mapped_column(String)  # created|streaming|completed|failed
    model: Mapped[str] = mapped_column(String)  # logical id (never an engine name)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # NULL for a non-graph /runs run, populated for /graphs/runs.
    # graph_id+graph_version = the IR registry coordinate; content_hash = its content-addressed
    # identity (recorded, not yet gated on).
    graph_id: Mapped[str | None] = mapped_column(String, nullable=True)
    graph_version: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which trigger fired this run.
    # NULL = an interactive run (POST /runs, /graphs/runs, /agents/{id}/runs, /agents/{id}/invoke);
    # populated for a schedule-/webhook-fired run, giving an unattended run lineage for the run list
    # and debugging. A plain breadcrumb, NOT an enforced FK: a trigger can be DELETEd while its
    # historical runs stay intact and keep recording the id of what fired them — the lineage of what
    # *did* run must outlive the trigger definition, so a referential constraint is the wrong tool.
    trigger_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Who started this run. NULL = system-initiated (a schedule/webhook firing, or a pre-auth
    # row). A breadcrumb like ``trigger_id`` — run history must outlive the account, so no FK.
    user_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    # The run's final accumulated output, durable with or without a session. NULL for a run that
    # never reached a terminal output (e.g. a failed run). TEXT — outputs can be large; kept
    # on the run row (the domain/ORM split still holds via the `Run` entity).
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The node id a `waiting` run is paused at (a `human` node's DBOS.recv checkpoint).
    # NULL except while status == 'waiting'. Additive, like graph_id / trigger_id — the
    # bookkeeping POST /runs/{id}/resume reads to find the node (schema + delivery), not an FK.
    awaiting_node: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which runtime owns this run's lifecycle: 'durable' rows recover/resume via the workflow
    # engine after a crash, so the startup reconcile sweep must leave them alone; NULL (legacy)
    # and 'inproc' rows are the interpreter's and are swept honestly when a restart orphans them.
    runtime: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)
    # A real terminal-completion instant, so duration is exact (completed_at - created_at)
    # instead of the updated_at proxy, AND every run is a [created_at, completed_at] interval
    # from which system-wide concurrency is a sweep-line query. Stamped on a real-time terminal
    # transition (completed/failed); LEFT NULL for a reconcile-swept zombie — its true end time
    # is unknown; now() would be reconcile-time garbage.
    completed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)


class MessageRow(Base):
    __tablename__ = "message"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # session_id is denormalized (also reachable via run) for direct ordered session reads.
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_session.id"))
    # NULL = a client-appended turn (a direct-to-inference chat persisting its history);
    # populated when a run wrote the pair.
    run_id: Mapped[str | None] = mapped_column(ForeignKey("run.id"), nullable=True)
    role: Mapped[str] = mapped_column(String)  # user|assistant (system later)
    content: Mapped[str] = mapped_column(String)
    # Monotonic within the session; THE ordering key (not timestamps — see _TZ note).
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    # UNIQUE: a session's positions are dense and monotonic; uniqueness is correct by
    # design. It is also the safety net for concurrency — append_turn serializes writes
    # with SELECT … FOR UPDATE on the session row, but if that lock ever regressed, a
    # colliding position would fail loudly here instead of silently losing a turn (the
    # one race shared Postgres state introduced that a single-instance dict could not).
    __table_args__ = (Index("ix_message_session_position", "session_id", "position", unique=True),)


class AgentRow(Base):
    """A saved agent's stable identity — the ``id`` that is constant across every version.
    The *content* lives in ``AgentVersionRow``; this row is just the named identity so a
    new version of an agent never mints a new ``id``. ``owner_id``/``workspace_id`` are
    omitted: a future shared multi-user registry slots a scoping column in WITHOUT a
    reshape. Single-user
    localhost until then. ``id`` is the IR document's own ``id`` (the IR carries its identity;
    the registry persists it under that key, so a stored agent and the Run it produces agree)."""

    __tablename__ = "agent"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)  # human label; NOT the key (id is)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


class AgentVersionRow(Base):
    """One immutable, content-addressed version of an agent. A given ``(agent_id,version)``
    resolves to exactly one ``content_hash`` forever — the UNIQUE index is the immutability guard:
    publishing different content under an existing ``(id, version)`` is rejected.
    ``ir`` is the canonical, **view-stripped** document (the registry stores the IR, never an
    invented "agent format"); ``view`` (React-Flow layout) is stored alongside but **never
    hashed** — dragging a node must not mint a version — so ``content_hash`` == the walker's
    ``content_hash`` for the same IR.

    ``seq`` is the monotonic-per-agent ordering key: order versions by it, NEVER by ULID or
    timestamp — clock skew across horizontally-scaled instances makes those unreliable. ``id`` is a
    fresh ULID for the version row itself (distinct from ``agent_id``)."""

    __tablename__ = "agent_version"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"))
    version: Mapped[str] = mapped_column(String)  # semver
    content_hash: Mapped[str] = mapped_column(String)  # == the walker's hash
    ir: Mapped[dict] = mapped_column(JSONB)  # canonical, view-stripped IR document
    view: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # stored, never hashed
    seq: Mapped[int] = mapped_column(Integer)  # monotonic per agent; the ordering key
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        # The immutability guard: one content per (agent, version), forever. A colliding
        # publish fails loudly here even if the application-level check ever regressed.
        Index("ix_agent_version_agent_version", "agent_id", "version", unique=True),
        # The ordering key — versions newest-first by seq, never by time/ULID.
        Index("ix_agent_version_agent_seq", "agent_id", "seq"),
        # Content-addressed lookup: pin-by-hash deploys resolve a version by its hash.
        Index("ix_agent_version_content_hash", "content_hash"),
    )


class AgentDraftRow(Base):
    """A mutable, autosaved, possibly-INVALID agent graph — the editor's work-in-progress,
    the opposite of ``AgentVersionRow`` (immutable, content-addressed, validated).
    Rows are updated in place on every autosave; the ``ir`` is NEVER validated or hashed (a
    half-wired graph is the point of a draft), and ``view`` (layout) is split off exactly like
    ``agent_version.ir``/``view``. Publishing a draft goes through the ``/agents`` registry —
    this table never feeds a run.

    ``agent_id`` is the registry agent this draft edits (NULL for a never-published graph) — a
    plain breadcrumb, NOT an enforced FK, so deleting the agent never takes the draft with it
    (the ``run.graph_id``/``run.trigger_id`` precedent: an origin pointer must not couple
    lifecycles). ``owner_id`` is a reserved per-user scoping slot (NULL until identity lands
    — the ``agent_io_policy.updated_by`` pattern). ``name``/``node_count`` are derived list-view
    labels, re-derived from the ir on every write."""

    __tablename__ = "agent_draft"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``drf_<ulid>``
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)  # breadcrumb, no FK
    owner_id: Mapped[str | None] = mapped_column(String, nullable=True)  # reserved principal slot
    name: Mapped[str] = mapped_column(String)  # derived from the ir (name > id > "Untitled")
    node_count: Mapped[int] = mapped_column(Integer)  # derived from the ir
    ir: Mapped[dict] = mapped_column(JSONB)  # unvalidated, unhashed, view-stripped document
    view: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # layout, never validated
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        # Non-unique on purpose: the client avoids duplicate per-agent drafts; the DB doesn't
        # enforce it, so a multi-tab race tolerably yields two drafts instead of an error.
        Index("ix_agent_draft_agent", "agent_id"),
    )


class McpServerRow(Base):
    """A persisted MCP server registration — the *registration*, never the live process handle.
    The domain shape is the manager's ``McpServerConfig`` (mcp/client.py); this row is the
    persistence shape, mapped in ``store.py``. ``env`` (the user's secrets/paths) is stored so
    the registration round-trips a restart — distinct from logging it (sovereignty rules forbid
    logging values, which ``_mcp_view`` honours, not storing them here)."""

    __tablename__ = "mcp_server"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    transport: Mapped[str] = mapped_column(String)  # "stdio" | "http"
    command: Mapped[str | None] = mapped_column(String, nullable=True)  # stdio only
    args: Mapped[list] = mapped_column(JSONB)
    env: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cwd: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)  # http only
    headers: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # http only
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


class TriggerRow(Base):
    """A non-interactive entry point that fires a *saved, pinned* agent unattended.

    This row IS the hard-to-reverse seam: the *definition* of a trigger — what fires which saved
    agent, with what pin, on what condition — is frozen and persisted; the *dispatcher* that reads
    it and fires runs is a reversible in-process detail that swaps for the durable worker without
    reshaping this table. Schedules persist here and rehydrate by being re-read on every dispatcher
    tick, so losing schedules on restart is not a concern.

    A trigger always pins: exactly one of ``version`` / ``content_hash`` is set, so an unattended
    deploy runs an immutable artifact and can never silently change because a graph was edited.
    ``kind``-specific knobs live in ``config`` (JSONB): a cron expression (schedule), a webhook
    signing secret (webhook), an optional input template. ``last_fired_at`` is dispatcher
    bookkeeping — NOT part of the frozen contract — persisted so a restart neither double-fires a
    schedule within its window nor backfills a long downtime (the dispatcher resumes from it)."""

    __tablename__ = "trigger"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"))
    # The version pin: exactly one of version / content_hash is set — the app enforces it.
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str] = mapped_column(String)  # http | schedule | webhook
    # kind-specific: {"cron": …, "input": …} | {"secret": …, "input": …} | {"input": …}
    config: Mapped[dict] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean)
    # Dispatcher bookkeeping (NOT the frozen contract): the last instant a schedule fired, so the
    # next tick computes due-ness from it and a restart resumes cleanly. NULL = never fired.
    last_fired_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        # The dispatcher lists enabled schedules every tick; agent_id supports listing a trigger's
        # source agent. A small registry today, but the index keeps the per-tick scan honest.
        Index("ix_trigger_agent", "agent_id"),
    )


class SpanRow(Base):
    """One row of the run waterfall — a run-root span, a node span, or a phase span. This is what
    the in-UI timeline reads, NOT the DBOS journal (spans for the timeline, the journal for resume;
    both keyed by ``run_id`` but serving different masters). The domain shape is ``run.Span``; this
    row is the persistence shape, mapped in ``store.py``.

    Deviations from the standard conventions:
    * ``start_ns``/``end_ns`` are epoch nanoseconds (``BigInteger``), not TIMESTAMPTZ — OTel is
      ns-resolution and the waterfall needs clean integer arithmetic (``end-start`` = duration,
      ``next.start - prev.end`` = gap). ``created_at`` stays TIMESTAMPTZ.
    * ``id`` is a deterministic composite (``{run_id}:{node_id}[:{phase}][:#{branch}]``), not a ULID
      — the load-bearing half of the resume-idempotency rule (ON CONFLICT DO NOTHING on a replay
      re-write, first-writer-wins so a resumed run visibly hops workers,
      enabling worker attribution).
    * ``executor_id`` / ``worker_host`` — worker attribution: which durable worker handled the span
      (``local``/distributed id for DBOS, ``inproc`` for the interactive walker; ``host:pid``)."""

    __tablename__ = "span"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    trace_id: Mapped[str] = mapped_column(String)  # OTel trace id (hex)
    otel_span_id: Mapped[str] = mapped_column(String)  # OTel span id (hex)
    parent_span_id: Mapped[str | None] = mapped_column(String, nullable=True)  # NULL = run root
    node_id: Mapped[str | None] = mapped_column(String, nullable=True)
    node_type: Mapped[str | None] = mapped_column(String, nullable=True)
    kind: Mapped[str | None] = mapped_column(String, nullable=True)
    name: Mapped[str] = mapped_column(String)  # node id for node spans, phase name otherwise
    phase: Mapped[str | None] = mapped_column(String, nullable=True)
    branch_index: Mapped[int | None] = mapped_column(Integer, nullable=True)  # loop/map iteration
    status: Mapped[str] = mapped_column(String)  # ok|err|skipped|running
    start_ns: Mapped[int] = mapped_column(BigInteger)
    end_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # NULL while in-flight
    # GenAI-semconv scalars only; NO payloads (those live in node_io).
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    executor_id: Mapped[str | None] = mapped_column(String, nullable=True)  # worker attribution
    worker_host: Mapped[str | None] = mapped_column(String, nullable=True)
    seq: Mapped[int] = mapped_column(Integer)  # monotonic per run; the ordering key
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        Index("ix_span_run_seq", "run_id", "seq"),
        Index("ix_span_run_parent", "run_id", "parent_span_id"),
        Index("ix_span_trace", "trace_id"),
    )


class NodeIoRow(Base):
    """Per-node I/O context — lazy-loaded on click, **never exported** over OTLP.
    Port-keyed ``inputs``/``outputs`` so multi-input graphs render edge-by-edge. ``capture_level``
    records what was actually persisted (``full``/``metadata``/``off``) so ``/io`` reports honestly.
    ``UNIQUE(run_id, node_id)`` is the idempotency guard (a replay re-write is a no-op)."""

    __tablename__ = "node_io"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    node_id: Mapped[str] = mapped_column(String)
    span_id: Mapped[str | None] = mapped_column(String, nullable=True)  # soft join target
    inputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # { port: value } post-$in
    outputs: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # { out_port: value }
    bytes_in: Mapped[int] = mapped_column(Integer, server_default="0")
    bytes_out: Mapped[int] = mapped_column(Integer, server_default="0")
    truncated: Mapped[bool] = mapped_column(Boolean, server_default=false())
    capture_level: Mapped[str] = mapped_column(String)  # off | metadata | full (as captured)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        Index("ix_node_io_run_node", "run_id", "node_id", unique=True),
        Index("ix_node_io_run", "run_id"),
    )


class AgentIoPolicyRow(Base):
    """Per-agent I/O capture governance — keyed to the STABLE ``agent.id``, NOT ``agent_version``,
    so editing capture policy never changes the agent's ``contentHash`` (immutability invariant).
    Absent row → effective policy = the topology default. ``updated_by`` is a reserved principal
    slot (NULL in single-user; filled by a future identity layer). This is the ONLY
    governance table — no identity/role/grant tables (none exist yet)."""

    __tablename__ = "agent_io_policy"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"), primary_key=True)
    io_capture: Mapped[str] = mapped_column(String)  # off | metadata | full
    io_retention_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redact_rules: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_TZ)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)


# ── The bench store — saved benchmark results + suites/cases + param presets ──────────────────────
# A benchmark is only honest if pinned to *exactly what ran*. Two pin shapes share one ``bench_run``
# row: a MODEL run pins ``logical_id`` + ``model_ref`` + ``binding`` + a params digest (temperature
# 0.2 vs 0.9 are different benchmarks, so params are part of identity); an AGENT run pins
# ``agent_id`` + ``version`` + ``content_hash`` (the content-addressing discipline — params already
# live inside the hashed IR). **Metrics + digests by default; raw payloads are NOT journaled here**:
# in cloud topology this DB is hosted, so a captured prompt/output/audio would breach sovereignty.
# Capture is opt-in and stays local (``capture_ref`` — a reference, never a blob).


class BenchSuiteRow(Base):
    """A saved suite of golden cases pinned to one target. The pin shape mirrors ``bench_run``:
    a model suite sets ``logical_id``/``binding``, an agent suite sets ``agent_id`` +
    (``version``|``content_hash``). Cases live in ``bench_case`` (the run loops them).
    A suite definition (its golden cases) is an authored test spec — distinct from a captured run
    payload — so it is stored in full so the suite re-runs."""

    __tablename__ = "bench_suite"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    target_kind: Mapped[str] = mapped_column(String)  # model | agent
    modality: Mapped[str | None] = mapped_column(String, nullable=True)
    # model target pin
    logical_id: Mapped[str | None] = mapped_column(String, nullable=True)
    binding: Mapped[str | None] = mapped_column(String, nullable=True)
    # agent target pin (exactly one of version / content_hash — the app enforces)
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


class BenchCaseRow(Base):
    """One golden case in a suite: ``(input, optional expected, assertion)``. ``input`` /
    ``expected`` are the AUTHORED test definition (not a captured run payload), stored so the
    suite re-runs. ``assertion`` ∈ exact|contains|regex|json-path-equals|llm-judge; the config
    carries the kind-specific knob (the regex, json path, llm-judge rubric + model)."""

    __tablename__ = "bench_case"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    suite_id: Mapped[str] = mapped_column(ForeignKey("bench_suite.id"))
    input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # {value: …} authored input
    expected: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # optional expected output
    assertion: Mapped[str] = mapped_column(
        String
    )  # exact|contains|regex|json-path-equals|llm-judge
    assertion_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    seq: Mapped[int] = mapped_column(Integer)  # ordering within the suite
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (Index("ix_bench_case_suite_seq", "suite_id", "seq"),)


class BenchRunRow(Base):
    """One recorded benchmark result — metrics pinned to exactly what ran. Either a MODEL pin
    (``logical_id`` + ``model_ref`` + ``binding`` + ``params_digest``) or an AGENT pin
    (``agent_id`` + ``version`` + ``content_hash``). ``metrics`` (JSONB) is the honest numbers
    captured at the bench (ttftMs, tokensPerSec, totalMs, prompt/completion tokens, cost, plus
    modality extras — rtf for STT, ttfbMs for TTS). ``output_digest`` is a cheap identity for
    the compare diff WITHOUT the raw output; ``capture_ref`` is the opt-in LOCAL reference to
    raw I/O (never a blob in this hot table — pass references, not raw payloads)."""

    __tablename__ = "bench_run"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    target_kind: Mapped[str] = mapped_column(String)  # model | agent
    modality: Mapped[str] = mapped_column(String)  # chat|vision|embeddings|audio.*|agent
    # model pin
    logical_id: Mapped[str | None] = mapped_column(String, nullable=True)
    model_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # resolved engine model
    binding: Mapped[str | None] = mapped_column(String, nullable=True)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # literal params (config)
    params_digest: Mapped[str | None] = mapped_column(String, nullable=True)  # identity of params
    # agent pin
    agent_id: Mapped[str | None] = mapped_column(String, nullable=True)
    version: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # the numbers + content identity
    metrics: Mapped[dict] = mapped_column(JSONB)
    output_digest: Mapped[str | None] = mapped_column(String, nullable=True)
    # opt-in local capture (NEVER a raw blob in cloud topology — pass references, not payloads)
    capture_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # suite linkage — a suite/case run tags its rows so a regression is one query
    suite_id: Mapped[str | None] = mapped_column(String, nullable=True)
    case_id: Mapped[str | None] = mapped_column(String, nullable=True)
    assertion: Mapped[str | None] = mapped_column(String, nullable=True)
    assertion_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # lineage + label
    run_id: Mapped[str | None] = mapped_column(String, nullable=True)  # control-plane run (agents)
    label: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        Index("ix_bench_run_created", "created_at", "id"),
        Index("ix_bench_run_logical", "logical_id"),
        Index("ix_bench_run_agent", "agent_id"),
        Index("ix_bench_run_suite", "suite_id"),
        Index("ix_bench_run_case", "case_id"),
    )


# ── Tool/MCP auth seam — encrypted secrets + connection bindings ──────────────────────────────────
# Tool/MCP auth never lives inline in the IR. A node references a logical tool key in ``ir.tools``;
# that binding references a ``connection`` by id; the connection carries NON-SECRET config
# (url/headers/transport/scopes) in JSONB and a ``secret_ref`` pointing at the encrypted ``secret``
# row. The handler resolves connection → secret_ref → plaintext SERVER-SIDE, inside the step, at
# runtime — raw secrets never reach the IR, the canvas, a span, or the journal. Rotating the secret
# keeps every agent's ``contentHash`` stable (the connection id is hashed content; the secret is
# not). See ``secrets.py`` for at-rest encryption.


class SecretRow(Base):
    """An encrypted-at-rest secret. ``id`` is the ``secret_ref`` a connection points at;
    ``ciphertext`` is the Fernet token (AES-128-CBC + HMAC, base64 text) from ``SecretStore`` — the
    plaintext is NEVER stored, logged, or returned over the wire. The raw value is decrypted only
    server-side inside the step that calls the tool/MCP (sovereignty posture: raw values never leave
    the step). A real KMS/HSM/vault is a future upgrade; this table is the minimal honest
    indirection so the IR carries no secret and rotation never bumps a hash."""

    __tablename__ = "secret"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # the secret_ref
    ciphertext: Mapped[str] = mapped_column(Text)  # Fernet token (never plaintext)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


class ConnectionRow(Base):
    """A server-side tool/MCP auth + config binding. ``kind`` ∈ {http_auth, mcp_server}.
    ``config`` (JSONB) holds ONLY non-secret material — an http connection's base url / static
    headers / auth *scheme* / OAuth token endpoint, or an mcp_server's transport (stdio:
    command/args/env-keys; http: url/headers/scopes). The secret material (api key, bearer token,
    OAuth client secret, basic password) lives behind ``secret_ref`` in the ``secret`` table, NEVER
    in this row's JSONB. The IR references this row by ``id`` from the ``tools`` block; the row's id
    (a stable logical reference) is hashed content, the secret is not — so rotating a key keeps the
    agent's ``contentHash`` stable, and prod-vs-staging connections are correctly different hashes.
    On export/import the id is an environment binding the importer re-maps (like a model
    binding)."""

    __tablename__ = "connection"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``con_<ulid>``
    name: Mapped[str] = mapped_column(String)  # human label
    kind: Mapped[str] = mapped_column(String)  # http_auth | mcp_server
    config: Mapped[dict] = mapped_column(JSONB)  # non-secret config only (never the secret)
    secret_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # → secret.id
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        Index("ix_connection_kind", "kind"),
        Index("ix_connection_created", "created_at", "id"),
    )


class GateCounterRow(Base):
    """The trivial per-key counter backing the ``ratelimit`` gate — a lean seam, NOT a metering
    engine. ``id`` is a composite scope+key+window-id string (so different gates / keys / windows
    are independent rows); ``count`` is the hits in the current fixed window, reset on window rolls
    (``window_start``). Atomic upsert in ``GateBackend.hit`` keeps it correct under concurrency.
    ``quota`` does NOT use this table — it READS accumulated token usage off span attributes
    ('usage read, not re-metered'), building no new metering pipeline."""

    __tablename__ = "gate_counter"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # scope:key:window_id
    count: Mapped[int] = mapped_column(Integer)
    window_start: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


class BenchPresetRow(Base):
    """A named, modality-scoped, LITERAL param set — a sibling of bench results, not a third
    subsystem. ``params`` holds VALUES ONLY (no run/agent link): "apply preset" copies these
    literals into an agent's ``models[binding].params``, so the IR never stores a preset *reference*
    (a live reference would silently drift a deployed agent's behaviour — contentHash would then
    not reflect the actual params used). ``modality`` scopes it (a chat preset can't land on a TTS
    binding); ``logical_id`` is an optional tag for the model it was tuned against."""

    __tablename__ = "bench_preset"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    modality: Mapped[str] = mapped_column(String)  # chat|vision|embeddings|audio.*
    logical_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # optional tuned-against tag
    params: Mapped[dict] = mapped_column(JSONB)  # literal values only (never a reference)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (Index("ix_bench_preset_modality", "modality"),)


# ── Retrieval (RAG) rows ─────────────────────────────────────────────────────
#
# A retrieval *source* is a named, user-owned document collection an agent's ``rag`` node queries.
# Vectors live in the SAME Postgres (pgvector) so chunks stay transactional with their source —
# one backup, FK cascades, and hybrid (vector + full-text) search in one SQL statement. The
# embedding model is a LOGICAL inference-plane id pinned per source at creation: mixing embeddings
# from different models in one similarity space is meaningless, so switching models is an explicit
# re-ingest, never a silent drift. ``embedding_dim`` is discovered from the first embedding
# response (the control plane never imports an embedding library — the dim is whatever the
# inference plane returned) and every chunk carries it so per-dimension partial indexes and the
# mandatory dim predicate keep mixed-dimension rows out of one ANN comparison.


class RagSourceRow(Base):
    """One retrieval collection + the state of its ingest job. Ingestion (crawl/parse/chunk/embed)
    runs as an in-process background task; its live counters land in ``progress`` (JSONB) and its
    terminal outcome in ``status``/``error`` — the UI polls the row, mirroring how model downloads
    report. A source interrupted by a restart is swept to ``failed`` on startup (the same honesty
    net as orphaned runs — cheap mitigation, not resume)."""

    __tablename__ = "rag_source"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``rag_<ulid>``
    name: Mapped[str] = mapped_column(String)  # human label (the picker shows this)
    kind: Mapped[str] = mapped_column(String)  # upload | crawl
    # Non-secret ingest config. For ``crawl``: root_url, max_pages, render_js (opt into a headless
    # browser for JS-rendered sites), include/exclude path prefixes. For ``upload``: unused.
    config: Mapped[dict] = mapped_column(JSONB)
    embedding_model: Mapped[str] = mapped_column(String)  # logical id (never an engine name)
    # Discovered from the first embedding response; NULL until the first chunk embeds.
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String)  # empty|ingesting|ready|failed|cancelled
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # live ingest counters
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (Index("ix_rag_source_created", "created_at", "id"),)


class RagDocumentRow(Base):
    """One ingested document (a crawled page or an uploaded file). ``content_hash`` is the sha256
    of the *extracted text*, so a re-crawl skips unchanged pages (and their embedding cost) instead
    of re-embedding the whole site. ``uri`` is unique per source — re-ingesting a page replaces its
    chunks rather than duplicating them."""

    __tablename__ = "rag_document"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``rdoc_<ulid>``
    source_id: Mapped[str] = mapped_column(
        ForeignKey("rag_source.id", ondelete="CASCADE"), nullable=False
    )
    uri: Mapped[str] = mapped_column(Text)  # page url, or the uploaded filename
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # sha256 of the text
    status: Mapped[str] = mapped_column(String)  # pending|embedded|failed
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    chars: Mapped[int] = mapped_column(Integer)  # extracted text length (ingest evidence)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        Index("ix_rag_document_source", "source_id"),
        UniqueConstraint("source_id", "uri", name="uq_rag_document_source_uri"),
    )


class RagChunkRow(Base):
    """One embedded chunk — the retrieval unit. ``embedding`` is an UNTYPED pgvector
    column (no fixed dimension): sources pin different embedding models, so one typed column can't
    hold them all. Every similarity query filters on ``dim`` and casts
    ``embedding::vector(<dim>)`` — the exact expression the per-dimension partial HNSW index (built
    by the ingest service once a dimension first appears; runtime DDL, kept outside
    Alembic) is defined over, so the query planner can use it. ``tsv`` is the generated full-text
    column for the keyword leg of hybrid search. ``source_id`` is denormalized off the document so
    the hot query path (filter by source, order by distance) needs no join."""

    __tablename__ = "rag_chunk"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``rchk_<ulid>``
    document_id: Mapped[str] = mapped_column(
        ForeignKey("rag_document.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[str] = mapped_column(
        ForeignKey("rag_source.id", ondelete="CASCADE"), nullable=False
    )
    position: Mapped[int] = mapped_column(Integer)  # chunk order within the document
    text: Mapped[str] = mapped_column(Text)
    # The heading path ("Install > macOS") — prepended at embed time (context) + shown in results.
    heading: Mapped[str | None] = mapped_column(Text, nullable=True)
    dim: Mapped[int] = mapped_column(Integer)  # embedding dimension (the index/query predicate)
    embedding: Mapped[list[float]] = mapped_column(Vector())  # untyped — see class docstring
    tsv: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', text)", persisted=True),
    )
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        Index("ix_rag_chunk_source", "source_id"),
        Index("ix_rag_chunk_document", "document_id"),
        Index("ix_rag_chunk_tsv", "tsv", postgresql_using="gin"),
    )


class PlatformSettingRow(Base):
    """One STORED platform setting (migration ``0017_platform_setting``). The catalog of valid
    keys/types/defaults/env-precedence lives in code (``settings.py``); this row exists only when
    a user explicitly set the key, so "reset to default" is a row delete — never a sentinel. For a
    sensitive key, ``value`` is the ``{"secret_ref", "names"}`` envelope pointing at an encrypted
    ``secret`` row; plaintext never lands here. Keys no longer in the catalog are ignored at
    resolution and surfaced as orphaned (deletable, never a boot failure)."""

    __tablename__ = "platform_setting"

    key: Mapped[str] = mapped_column(Text, primary_key=True)  # dotted catalog key
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)  # any JSON value (never SQL NULL)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


# ── Identity & access rows ────────────────────────────────────────────────────────────────────────
# The identity layer that fills the three long-standing auth seams (``require_auth``,
# ``require_token``, ``governance.authorize``). Accounts are local (scrypt password) and/or
# federated (OIDC identities); every credential presented over the wire is an opaque bearer token
# stored HASHED (sha256) — the raw token exists only in the response that minted it. Roles are a
# closed three-value set enforced at the app layer (no native enum, per house convention).


class UserAccountRow(Base):
    """One account. ``user_account``, not ``user`` — the bare name is a reserved word in
    Postgres and would force quoting through every raw-SQL test helper. ``password_hash`` is a
    self-describing scrypt string (``auth.py``); NULL for an SSO-only account (no password
    login possible). ``role`` is the account's ceiling — API keys may narrow it, never widen."""

    __tablename__ = "user_account"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``usr_<ulid>``
    # Lowercased at create/login so lookups are case-insensitive without citext.
    username: Mapped[str] = mapped_column(String)
    display_name: Mapped[str] = mapped_column(String)
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    role: Mapped[str] = mapped_column(String)  # admin | editor | viewer
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)  # NULL = SSO-only
    # An avatar image URL from a sign-in provider's userinfo (the OIDC ``picture`` claim),
    # refreshed on each SSO sign-in. NULL for a local account (the UI shows initials instead).
    # A plain http(s) URL to the provider's CDN, never a secret.
    avatar_url: Mapped[str | None] = mapped_column(String, nullable=True)
    disabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)
    last_login_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)

    __table_args__ = (Index("uq_user_account_username", "username", unique=True),)


class AuthIdentityRow(Base):
    """One federated identity linked to an account — the (provider, subject) pair an OIDC
    login resolves to a user. Local password accounts have no identity row. CASCADE with the
    user: an identity without its account is meaningless."""

    __tablename__ = "auth_identity"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``idn_<ulid>``
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id", ondelete="CASCADE"))
    # Breadcrumb to auth_provider.id — an identity stays meaningful (and auditable) after its
    # provider config is deleted, so no FK.
    provider_id: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)  # the provider's stable ``sub`` claim
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        UniqueConstraint("provider_id", "subject", name="uq_auth_identity_provider_subject"),
        Index("ix_auth_identity_user", "user_id"),
    )


class AuthSessionRow(Base):
    """One interactive sign-in. ``token_hash`` is sha256 of the opaque ``tys_…`` bearer the
    browser holds — a DB leak exposes no usable credentials. Expiry is absolute
    (``expires_at``); ``last_seen_at`` is telemetry, throttled to one write per few minutes,
    never the expiry input. CASCADE with the user: deleting an account signs it out."""

    __tablename__ = "auth_session"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``ses_<ulid>``
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String)  # sha256 hex of the bearer (never the token)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    expires_at: Mapped[datetime] = mapped_column(_TZ)
    last_seen_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        Index("uq_auth_session_token_hash", "token_hash", unique=True),
        Index("ix_auth_session_user", "user_id"),
    )


class ApiKeyRow(Base):
    """One long-lived programmatic credential (``tyk_…`` bearer, stored hashed). ``role`` is
    the key's OWN role, capped at the owner's role when minted; the effective authority at use
    time is min(key.role, owner.role), so demoting a user instantly narrows every key they
    minted. ``revoked_at`` soft-deletes — the row stays as an audit breadcrumb."""

    __tablename__ = "api_key"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``key_<ulid>``
    user_id: Mapped[str] = mapped_column(ForeignKey("user_account.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String)  # human label ("staging frontend")
    role: Mapped[str] = mapped_column(String)  # admin | editor | viewer (≤ owner at mint)
    token_hash: Mapped[str] = mapped_column(String)  # sha256 hex of the bearer
    # First characters of the raw token ("tyk_ab12…") — the only recoverable fragment,
    # so the UI can identify a key without ever storing the token.
    token_prefix: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    last_used_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)

    __table_args__ = (
        Index("uq_api_key_token_hash", "token_hash", unique=True),
        Index("ix_api_key_user", "user_id"),
    )


class AuthProviderRow(Base):
    """One configured sign-in provider (OIDC, or plain OAuth2 via explicit endpoints).
    ``config`` holds NON-SECRET material only: ``issuer_url`` (discovery) or explicit
    ``authorization_endpoint``/``token_endpoint``/``userinfo_endpoint``, ``client_id``,
    ``scopes``, ``auto_provision``, ``default_role``, ``allowed_domains``. The client secret
    lives behind ``client_secret_ref`` in the encrypted ``secret`` table — same discipline as
    connections. ``slug`` is the stable URL fragment (``/auth/oidc/{slug}/…``)."""

    __tablename__ = "auth_provider"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # ``idp_<ulid>``
    name: Mapped[str] = mapped_column(String)  # button label ("Okta", "Google")
    slug: Mapped[str] = mapped_column(String)  # url-safe stable handle
    config: Mapped[dict] = mapped_column(JSONB)  # non-secret config only (never the secret)
    client_secret_ref: Mapped[str | None] = mapped_column(String, nullable=True)  # → secret.id
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (Index("uq_auth_provider_slug", "slug", unique=True),)
