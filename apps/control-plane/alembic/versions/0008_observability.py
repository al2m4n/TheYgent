"""observability: span + node_io + agent_io_policy (M17 §3)

Revision ID: 0008_observability
Revises: 0007_human_waiting
Create Date: 2026-06-24

M17 turns the M8 per-node "log timeline" stub into a real timing **waterfall** with click-through
per-node I/O — without coupling the UI to any external trace backend (the one rule, §0). Three new
``public`` tables, all plain M4-convention rows (no new heavy deps — §10):

* ``span`` — the timeline rows; one per emitted span (a run-root span, a node span, or a phase
  span). This is what the in-UI waterfall reads — NOT the DBOS journal (§1.2: spans for the
  timeline, journal for resume, both keyed by ``run_id`` but serving different masters, so swapping
  the durable runtime later does not move the waterfall). **Deliberate deviations recorded here:**
  - ``start_ns``/``end_ns`` are **epoch nanoseconds (BIGINT)**, not TIMESTAMPTZ (§1.4) — OTel is
    ns-resolution and the waterfall needs clean integer arithmetic (``end-start`` = duration,
    ``next.start - prev.end`` = gap). ``created_at`` stays TIMESTAMPTZ (a different concern).
  - ``id`` is a **deterministic composite** (``{run_id}:{node_id}[:{phase}][:#{branch}]``), not a
    random ULID. This is the load-bearing half of the §4 resume-idempotency rule: a resumed durable
    run re-executes the workflow body, so the wrapper re-opens every span — a deterministic id +
    ON CONFLICT DO NOTHING makes the redundant re-write a no-op AND preserves the row written by the
    worker that *actually completed* the step (first-writer-wins), so a crash-resumed run visibly
    hops workers in the waterfall (§1's worker-attribution ask) instead of overwriting history.
  - ``executor_id`` / ``worker_host`` — **worker attribution** (which durable worker handled this
    span). ``executor_id`` is the DBOS executor id (``local`` single-worker, a distinct id per
    distributed worker); ``inproc`` for the interactive walker. ``worker_host`` is ``host:pid``.
    This is what makes "see which worker handled what" answerable and the resume demo legible.
* ``node_io`` — per-node I/O context, lazy-loaded on click, **NEVER exported** (§1.3). Port-keyed
  ``inputs``/``outputs`` (multi-input M10 → one entry per port). ``UNIQUE(run_id, node_id)`` is the
  §4 idempotency guard. ``capture_level`` records what was actually persisted so ``/io`` can report
  ``full`` / ``metadata`` (sizes only) / ``off`` honestly.
* ``agent_io_policy`` — per-agent capture governance (§1.8), keyed to the **stable** ``agent.id``,
  NOT ``agent_version`` — so editing capture policy never changes ``contentHash`` (M11
  immutability). Absent row → effective policy = the topology default. ``updated_by`` is the
  deferred principal slot (NULL in single-user; filled by the Governance/Identity milestone, §1.9).
  This adds NO identity/role/grant tables — only this one policy table (§8 the Do-NOT).

Hand-written and fully reversible (the §6 round-trip test exercises upgrade head → downgrade base,
now including these three tables). ``dbos`` ordering is unchanged; Alembic owns ``public``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_observability"
down_revision: str | None = "0007_human_waiting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "span",
        # Deterministic composite id (§1.2 idempotency): {run_id}:{node_id}[:{phase}][:#{branch}].
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_id", sa.String(), sa.ForeignKey("run.id"), nullable=False),
        # OTel ids (hex) — for joining to an external backend if OTLP export is configured (§1.3).
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("otel_span_id", sa.String(), nullable=False),
        sa.Column("parent_span_id", sa.String(), nullable=True),  # NULL = the run-root span
        sa.Column("node_id", sa.String(), nullable=True),  # IR node id; == name for node spans
        sa.Column("node_type", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=True),  # activity|orchestration|boundary
        sa.Column("name", sa.String(), nullable=False),  # node id, or phase name (queue.wait, …)
        sa.Column("phase", sa.String(), nullable=True),  # NULL for node/root spans
        sa.Column("branch_index", sa.Integer(), nullable=True),  # loop/map per-iteration span
        sa.Column("status", sa.String(), nullable=False),  # ok|err|skipped|running
        # Epoch nanoseconds (§1.4) — clean integer arithmetic for durations + gaps.
        sa.Column("start_ns", sa.BigInteger(), nullable=False),
        sa.Column("end_ns", sa.BigInteger(), nullable=True),  # NULL while in-flight
        # GenAI-semconv SCALARS only (model, token counts, finish_reason, ttft_ms, …). NO payloads.
        sa.Column("attributes", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        # Worker attribution (§1): which durable worker/executor handled this span.
        sa.Column("executor_id", sa.String(), nullable=True),
        sa.Column("worker_host", sa.String(), nullable=True),
        # Monotonic per run — the ordering key (M4 §3 — not ULID/time).
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
    )
    op.create_index("ix_span_run_seq", "span", ["run_id", "seq"])
    op.create_index("ix_span_run_parent", "span", ["run_id", "parent_span_id"])
    op.create_index("ix_span_trace", "span", ["trace_id"])

    op.create_table(
        "node_io",
        sa.Column("id", sa.String(), primary_key=True),  # ULID — written once per (run, node)
        sa.Column("run_id", sa.String(), sa.ForeignKey("run.id"), nullable=False),
        sa.Column("node_id", sa.String(), nullable=False),
        sa.Column("span_id", sa.String(), nullable=True),  # the node's span id (soft join target)
        sa.Column("inputs", postgresql.JSONB(), nullable=True),  # { port: value } — post-$in
        sa.Column("outputs", postgresql.JSONB(), nullable=True),  # { out_port: value }
        sa.Column("bytes_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("capture_level", sa.String(), nullable=False),  # off|metadata|full (as captured)
        sa.Column("created_at", _TZ, nullable=False),
    )
    # The §4 idempotency guard: one I/O row per (run, node), upsert/DO-NOTHING on a replay re-write.
    op.create_index("ix_node_io_run_node", "node_io", ["run_id", "node_id"], unique=True)
    op.create_index("ix_node_io_run", "node_io", ["run_id"])

    op.create_table(
        "agent_io_policy",
        # Keyed to the STABLE agent id (§1.8) — NOT agent_version, so editing it never changes
        # contentHash. Absent row → effective policy = the topology default.
        sa.Column("agent_id", sa.String(), sa.ForeignKey("agent.id"), primary_key=True),
        sa.Column("io_capture", sa.String(), nullable=False),  # off | metadata | full
        sa.Column("io_retention_seconds", sa.Integer(), nullable=True),  # optional TTL; NULL=keep
        sa.Column("redact_rules", postgresql.JSONB(), nullable=True),  # field/path patterns
        sa.Column("updated_at", _TZ, nullable=False),
        sa.Column("updated_by", sa.String(), nullable=True),  # principal slot (NULL single-user)
    )


def downgrade() -> None:
    # Reverse dependency/creation order for a clean round-trip.
    op.drop_table("agent_io_policy")
    op.drop_index("ix_node_io_run", table_name="node_io")
    op.drop_index("ix_node_io_run_node", table_name="node_io")
    op.drop_table("node_io")
    op.drop_index("ix_span_trace", table_name="span")
    op.drop_index("ix_span_run_parent", table_name="span")
    op.drop_index("ix_span_run_seq", table_name="span")
    op.drop_table("span")
