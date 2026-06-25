"""SQLAlchemy ORM rows — the persistence shape (M4 §3), kept SEPARATE from the domain.

These are *rows*, not the API/logic entities. The domain ``Run`` (``run.py``) is a clean
Pydantic model mapped to/from ``RunRow`` (§1.3): the wire contract must not be welded to
the schema, so each can evolve without breaking the other — the same seam-thinking as
IR-vs-React-Flow. A thin mapping layer (``store.py``) is the cost; decoupling is the payoff.

``Base.metadata`` here is Alembic's ``target_metadata``, but the schema is owned by the
hand-written migration (§0/§1.1) — **never** ``create_all``, including in tests. Keep the
migration and these models in lock-step by hand.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, false
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# TIMESTAMPTZ everywhere (§3): tz-aware instants so ordering/age is unambiguous across
# horizontally-scaled instances. Ordering itself keys off explicit columns (position /
# the FK graph), never a timestamp — clock skew across instances makes those unreliable.
_TZ = TIMESTAMP(timezone=True)


class ThreadRow(Base):
    __tablename__ = "thread"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)
    # Reserved name on Declarative (``Base.metadata``), so the attribute is ``meta`` while
    # the column stays ``metadata``. No semantics in M4 — reserved for later (§3).
    meta: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)


class RunRow(Base):
    __tablename__ = "run"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # NULL = one-shot run with no thread/memory — M3's behavior, preserved (§3). Threads
    # are opt-in. ON DELETE not set: M4 never deletes threads/runs.
    thread_id: Mapped[str | None] = mapped_column(ForeignKey("thread.id"), nullable=True)
    status: Mapped[str] = mapped_column(String)  # created|streaming|completed|failed
    model: Mapped[str] = mapped_column(String)  # logical id (never an engine name)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # M5 graph run (deliberate contract extension — m5.md §3.4): NULL for a non-graph /runs
    # run, populated for /graphs/runs. graph_id+graph_version = the IR registry coordinate
    # (§8.2); content_hash = its content-addressed identity (recorded, not yet gated on — §3.3).
    graph_id: Mapped[str | None] = mapped_column(String, nullable=True)
    graph_version: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # M12 §2 (deliberate contract extension, like M5 added graph_id): which trigger fired this run.
    # NULL = an interactive run (POST /runs, /graphs/runs, /agents/{id}/runs, /agents/{id}/invoke);
    # populated for a schedule-/webhook-fired run, giving an unattended run lineage for the run list
    # and debugging. A plain breadcrumb, NOT an enforced FK: a trigger can be DELETEd (§3) while its
    # historical runs stay intact and keep recording the id of what fired them — the lineage of what
    # *did* run must outlive the trigger definition, so a referential constraint is the wrong tool.
    trigger_id: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    # M9 §2.2 (F5.3): the run's final accumulated output, durable regardless of threading. NULL
    # for a run that never reached a terminal output (e.g. a failed run). TEXT — outputs can be
    # large; kept on the run row (M4 §1.3 domain/ORM split still holds via the `Run` entity).
    output: Mapped[str | None] = mapped_column(Text, nullable=True)
    # M14 §1.1: the node id a `waiting` run is paused at (a `human` node's DBOS.recv checkpoint).
    # NULL except while status == 'waiting'. Additive, like M5's graph_id / M12's trigger_id — the
    # bookkeeping POST /runs/{id}/resume reads to find the node (schema + delivery), not an FK.
    awaiting_node: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)
    # M12 §9 evidence gate (m13.md §1.2): a real terminal-completion instant, so duration is
    # exact (completed_at - created_at) instead of the updated_at proxy, AND every run is a
    # [created_at, completed_at] interval from which system-wide concurrency is a sweep-line query.
    # Stamped on a real-time terminal transition (completed/failed); LEFT NULL for a reconcile-swept
    # zombie (M9 §2.1 — its true end time is unknown; now() would be reconcile-time garbage).
    completed_at: Mapped[datetime | None] = mapped_column(_TZ, nullable=True)


class MessageRow(Base):
    __tablename__ = "message"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    # thread_id is denormalized (also reachable via run) for direct ordered thread reads.
    thread_id: Mapped[str] = mapped_column(ForeignKey("thread.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("run.id"))
    role: Mapped[str] = mapped_column(String)  # user|assistant (system later)
    content: Mapped[str] = mapped_column(String)
    # Monotonic within the thread; THE ordering key (not timestamps — see _TZ note).
    position: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    # UNIQUE: a thread's positions are dense and monotonic; uniqueness is correct by
    # design. It is also the safety net for concurrency — append_turn serializes writes
    # with SELECT … FOR UPDATE on the thread row, but if that lock ever regressed, a
    # colliding position would fail loudly here instead of silently losing a turn (the
    # one race shared Postgres state introduced that a single-instance dict could not).
    __table_args__ = (Index("ix_message_thread_position", "thread_id", "position", unique=True),)


class AgentRow(Base):
    """A saved agent's stable identity (M11 §2) — the §8.2 ``id`` that is constant across every
    version. The *content* lives in ``AgentVersionRow``; this row is just the named identity so a
    new version of an agent never mints a new ``id``. ``owner_id``/``workspace_id`` are deliberately
    omitted now (M11 §1.3): the Team-tier shared registry slots a scoping column in later WITHOUT a
    reshape — exactly as the Run was built Postgres-ready before threads existed. Single-user
    localhost until then. ``id`` is the IR document's own ``id`` (§8.2: the IR carries its identity;
    the registry persists it under that key, so a stored agent and the Run it produces agree)."""

    __tablename__ = "agent"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)  # human label; NOT the key (§2)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


class AgentVersionRow(Base):
    """One immutable, content-addressed version of an agent (M11 §2). A given ``(agent_id,version)``
    resolves to exactly one ``content_hash`` forever — the UNIQUE index is the immutability guard
    (§1.2): publishing different content under an existing ``(id, version)`` is rejected, the §8.2
    promise M12's deploys lean on. ``ir`` is the canonical, **view-stripped** §8.2 document (the
    registry stores the IR, never an invented "agent format" — §0/§7); ``view`` (React-Flow layout)
    is stored alongside but **never hashed** (§1.2 — dragging a node must not mint a version), so
    ``content_hash`` == the walker's ``content_hash`` for the same IR (§1.1).

    ``seq`` is the monotonic-per-agent ordering key (M4 §3): order versions by it, NEVER by ULID or
    timestamp — clock skew across horizontally-scaled instances makes those unreliable. ``id`` is a
    fresh ULID for the version row itself (distinct from ``agent_id``)."""

    __tablename__ = "agent_version"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"))
    version: Mapped[str] = mapped_column(String)  # semver (§8.2)
    content_hash: Mapped[str] = mapped_column(String)  # == the walker's hash (§1.1)
    ir: Mapped[dict] = mapped_column(JSONB)  # canonical, view-stripped IR document
    view: Mapped[dict | None] = mapped_column(JSONB, nullable=True)  # stored, never hashed (§1.2)
    seq: Mapped[int] = mapped_column(Integer)  # monotonic per agent; the ordering key (M4 §3)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        # The immutability guard (§1.2): one content per (agent, version), forever. A colliding
        # publish fails loudly here even if the application-level check ever regressed.
        Index("ix_agent_version_agent_version", "agent_id", "version", unique=True),
        # The ordering key (M4 §3) — versions newest-first by seq, never by time/ULID.
        Index("ix_agent_version_agent_seq", "agent_id", "seq"),
        # Content-addressed lookup: pin-by-hash deploys in M12 resolve a version by its hash.
        Index("ix_agent_version_content_hash", "content_hash"),
    )


class McpServerRow(Base):
    """A persisted MCP server registration (M9 §2.3 / F6.1) — the *registration*, never the live
    process handle. The domain shape is the manager's ``McpServerConfig`` (mcp/client.py); this row
    is the persistence shape, mapped in ``store.py`` (M4 §1.3). ``env`` (the user's secrets/paths)
    is stored so the registration round-trips a restart — distinct from logging it (the §10
    sovereignty rule forbids logging values, which ``_mcp_view`` honours, not storing them here)."""

    __tablename__ = "mcp_server"

    name: Mapped[str] = mapped_column(String, primary_key=True)
    transport: Mapped[str] = mapped_column(String)  # "stdio" (M7's only transport)
    command: Mapped[str] = mapped_column(String)
    args: Mapped[list] = mapped_column(JSONB)
    env: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    cwd: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)


class TriggerRow(Base):
    """A non-interactive entry point that fires a *saved, pinned* agent unattended (M12 §1.2/§2).

    This row IS the hard-to-reverse seam (§1.2): the *definition* of a trigger — what fires which
    saved agent, with what pin, on what condition — is frozen and persisted with M4 conventions; the
    *dispatcher* that reads it and fires runs is a reversible in-process detail that M13 swaps for
    the durable worker without reshaping this table. So losing schedules on restart is the F6.1
    class M9 closed for the MCP/model registries — schedules persist here and rehydrate by being
    re-read on every dispatcher tick (§3).

    A trigger always pins (§1.1): exactly one of ``version`` / ``content_hash`` is set, so an
    unattended deploy runs an immutable artifact and can never silently change because a graph was
    edited. ``kind``-specific knobs live in ``config`` (JSONB): a cron expression (schedule), a
    webhook signing secret (webhook), an optional input template. ``last_fired_at`` is dispatcher
    bookkeeping — NOT part of the frozen contract — persisted so a restart neither double-fires a
    schedule within its window nor backfills a long downtime (the dispatcher resumes from it)."""

    __tablename__ = "trigger"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"))
    # The version pin (§1.1): exactly one of version / content_hash is set — the app enforces it.
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
    """One row of the run waterfall (M17 §3) — a run-root span, a node span, or a phase span. This
    is what the in-UI timeline reads, NOT the DBOS journal (§1.2: spans for the timeline, the
    journal for resume; both keyed by ``run_id`` but serving different masters). The domain shape is
    ``run.Span``; this row is the persistence shape, mapped in ``store.py`` (M4 §1.3).

    Deliberate deviations from the M4 conventions, recorded in migration 0008:
    * ``start_ns``/``end_ns`` are epoch nanoseconds (``BigInteger``), not TIMESTAMPTZ (§1.4) — OTel
      is ns-resolution and the waterfall needs clean integer arithmetic (``end-start`` = duration,
      ``next.start - prev.end`` = gap). ``created_at`` stays TIMESTAMPTZ.
    * ``id`` is a deterministic composite (``{run_id}:{node_id}[:{phase}][:#{branch}]``), not a ULID
      — the load-bearing half of the §4 resume-idempotency rule (ON CONFLICT DO NOTHING on a replay
      re-write, first-writer-wins so a resumed run visibly hops workers, §1 worker attribution).
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
    # GenAI-semconv scalars only; NO payloads (those live in node_io — §1.3).
    attributes: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(String, nullable=True)
    executor_id: Mapped[str | None] = mapped_column(String, nullable=True)  # worker attribution
    worker_host: Mapped[str | None] = mapped_column(String, nullable=True)
    seq: Mapped[int] = mapped_column(Integer)  # monotonic per run; the ordering key (M4 §3)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (
        Index("ix_span_run_seq", "run_id", "seq"),
        Index("ix_span_run_parent", "run_id", "parent_span_id"),
        Index("ix_span_trace", "trace_id"),
    )


class NodeIoRow(Base):
    """Per-node I/O context (M17 §3) — lazy-loaded on click, **never exported** over OTLP (§1.3).
    Port-keyed ``inputs``/``outputs`` so multi-input (M10) renders edge-by-edge. ``capture_level``
    records what was actually persisted (``full``/``metadata``/``off``) so ``/io`` reports honestly.
    ``UNIQUE(run_id, node_id)`` is the §4 idempotency guard (a replay re-write is a no-op)."""

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
    """Per-agent I/O capture governance (M17 §1.8) — keyed to the STABLE ``agent.id``, NOT
    ``agent_version``, so editing capture policy never changes the agent's ``contentHash`` (M11
    immutability). Absent row → effective policy = the topology default. ``updated_by`` is the
    deferred principal slot (NULL in single-user; filled by the Governance/Identity milestone). This
    is the ONLY new governance table — no identity/role/grant tables (§8 the Do-NOT)."""

    __tablename__ = "agent_io_policy"

    agent_id: Mapped[str] = mapped_column(ForeignKey("agent.id"), primary_key=True)
    io_capture: Mapped[str] = mapped_column(String)  # off | metadata | full
    io_retention_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redact_rules: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(_TZ)
    updated_by: Mapped[str | None] = mapped_column(String, nullable=True)


# ── The bench store (M18 §1.6) — saved benchmark results + suites/cases + param presets ───────────
# A benchmark is only honest if pinned to *exactly what ran* (§1.6). Two pin shapes share one
# ``bench_run`` row: a MODEL run pins ``logical_id`` + ``model_ref`` + ``binding`` + a params digest
# (temperature 0.2 vs 0.9 are different benchmarks, so params are part of identity); an AGENT run
# pins ``agent_id`` + ``version`` + ``content_hash`` (the M11 content-addressing discipline — params
# already live inside the hashed IR). **Metrics + digests by default; raw payloads are NOT journaled
# here** (§1.6 / §10): in cloud topology this DB is hosted, so a captured prompt/output/audio would
# breach §10. Capture is opt-in and stays local (``capture_ref`` — a reference, never a blob).


class BenchSuiteRow(Base):
    """A saved suite of golden cases pinned to one target (M18 §2.5). The pin shape mirrors
    ``bench_run``: a model suite sets ``logical_id``/``binding``, an agent suite sets
    ``agent_id`` + (``version``|``content_hash``). Cases live in ``bench_case`` (the run loops them
    A suite definition (its golden cases) is an authored test spec — distinct from a captured run
    payload — so it is stored in full so the suite re-runs (the M12 trigger discipline)."""

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
    """One golden case in a suite (M18 §2.5): ``(input, optional expected, assertion)``. ``input`` /
    ``expected`` are the AUTHORED test definition (not a captured run payload — §1.6), stored so the
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
    seq: Mapped[int] = mapped_column(Integer)  # ordering within the suite (M4 §3)
    created_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (Index("ix_bench_case_suite_seq", "suite_id", "seq"),)


class BenchRunRow(Base):
    """One recorded benchmark result (M18 §1.6/§2.4) — metrics pinned to exactly what ran. Either a
    MODEL pin (``logical_id`` + ``model_ref`` + ``binding`` + ``params_digest``) or an AGENT pin
    (``agent_id`` + ``version`` + ``content_hash``). ``metrics`` (JSONB) is the honest numbers
    captured at the bench (ttftMs, tokensPerSec, totalMs, prompt/completion tokens, cost, plus
    modality extras — rtf for STT, ttfbMs for TTS). ``output_digest`` is a cheap identity for
    the compare diff WITHOUT the raw output; ``capture_ref`` is the opt-in LOCAL reference to
    raw I/O (never a blob in this hot table — §1.6 / the M14 pass-references rule)."""

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
    # opt-in local capture (NEVER a raw blob in cloud topology — §1.6 / §10)
    capture_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    # suite linkage (§2.5) — a suite/case run tags its rows so a regression is one query
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


class BenchPresetRow(Base):
    """A named, modality-scoped, LITERAL param set (M18 §1.7) — a sibling of bench results, not a
    third subsystem. ``params`` holds VALUES ONLY (no run/agent link): "apply preset" copies these
    literals into an agent's ``models[binding].params``, so the IR never stores a preset *reference*
    (a live reference would silently drift a deployed agent's behaviour — the contentHash-drift bug
    §1.7). ``modality`` scopes it (a chat preset can't land on a TTS binding); ``logical_id`` is
    an optional tag for the model it was tuned against."""

    __tablename__ = "bench_preset"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    modality: Mapped[str] = mapped_column(String)  # the §1.2 vocabulary
    logical_id: Mapped[str | None] = mapped_column(
        String, nullable=True
    )  # optional tuned-against tag
    params: Mapped[dict] = mapped_column(JSONB)  # literal values only (§1.7)
    created_at: Mapped[datetime] = mapped_column(_TZ)
    updated_at: Mapped[datetime] = mapped_column(_TZ)

    __table_args__ = (Index("ix_bench_preset_modality", "modality"),)
