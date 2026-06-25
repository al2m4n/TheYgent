"""bench store — saved benchmark results + suites/cases + param presets (M18 §1.6/§1.7)

The Bench (M18) proves a model/agent works and *measures* it. A benchmark is only honest if pinned
to exactly what ran (§1.6), so the results store keys each ``bench_run`` to either a MODEL pin
(``logical_id`` + ``model_ref`` + ``binding`` + ``params_digest`` — temperature 0.2 vs 0.9 are
DIFFERENT benchmarks, so params are part of the identity) or an AGENT pin (``agent_id`` +
``version`` + ``content_hash`` — the M11 content-addressing discipline; params already live inside
the hashed IR). The two pin shapes share one row.

**Metrics + digests by default — raw payloads are NEVER journaled here** (§1.6 / §10): in the cloud
topology this Postgres is hosted, so a captured prompt / output / audio / image would breach §10.
``metrics`` (JSONB) holds the numbers; ``output_digest`` is a cheap content identity for the compare
diff without the raw output; ``capture_ref`` is the opt-in LOCAL reference to raw I/O (a reference,
never a blob in the hot table — the M14 "pass references, don't journal blobs" rule).

* ``bench_suite`` / ``bench_case`` — golden cases pinned to a target (§2.5). A suite's cases are an
  AUTHORED test spec (distinct from a captured run payload), stored in full so the suite re-runs.
* ``bench_run`` — one recorded result; ``suite_id``/``case_id`` tag suite runs so a regression
  across versions is one query.
* ``bench_preset`` — a named, modality-scoped, LITERAL param set (§1.7), a sibling of results (not a
  third subsystem). Values only — "apply preset" copies them into the IR; the IR never stores a
  preset *reference* (that would be a contentHash-drift bug).

Hand-written and fully reversible — the §6 round-trip test exercises upgrade head -> downgrade base
including all four ``bench_*`` tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_bench"
down_revision: str | None = "0008_observability"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "bench_suite",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),  # model | agent
        sa.Column("modality", sa.String(), nullable=True),
        # model target pin
        sa.Column("logical_id", sa.String(), nullable=True),
        sa.Column("binding", sa.String(), nullable=True),
        # agent target pin (exactly one of version / content_hash — app-enforced)
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )

    op.create_table(
        "bench_case",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("suite_id", sa.String(), sa.ForeignKey("bench_suite.id"), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=True),  # authored test input
        sa.Column("expected", postgresql.JSONB(), nullable=True),  # optional expected output
        sa.Column("assertion", sa.String(), nullable=False),  # exact|contains|regex|json-path|judge
        sa.Column("assertion_config", postgresql.JSONB(), nullable=True),
        sa.Column("seq", sa.Integer(), nullable=False),  # ordering within the suite (M4 §3)
        sa.Column("created_at", _TZ, nullable=False),
    )
    op.create_index("ix_bench_case_suite_seq", "bench_case", ["suite_id", "seq"])

    op.create_table(
        "bench_run",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("target_kind", sa.String(), nullable=False),  # model | agent
        sa.Column("modality", sa.String(), nullable=False),  # chat|vision|embeddings|audio.*|agent
        # model pin
        sa.Column("logical_id", sa.String(), nullable=True),
        sa.Column("model_ref", sa.String(), nullable=True),
        sa.Column("binding", sa.String(), nullable=True),
        sa.Column("params", postgresql.JSONB(), nullable=True),  # literal params (config)
        sa.Column("params_digest", sa.String(), nullable=True),  # identity of the params
        # agent pin
        sa.Column("agent_id", sa.String(), nullable=True),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        # the numbers + content identity (NO raw payloads — §1.6 / §10)
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("output_digest", sa.String(), nullable=True),
        sa.Column(
            "capture_ref", sa.String(), nullable=True
        ),  # opt-in LOCAL reference, never a blob
        # suite linkage (§2.5)
        sa.Column("suite_id", sa.String(), nullable=True),
        sa.Column("case_id", sa.String(), nullable=True),
        sa.Column("assertion", sa.String(), nullable=True),
        sa.Column("assertion_passed", sa.Boolean(), nullable=True),
        # lineage + label
        sa.Column("run_id", sa.String(), nullable=True),  # control-plane run (agent runs)
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("created_at", _TZ, nullable=False),
    )
    op.create_index("ix_bench_run_created", "bench_run", ["created_at", "id"])
    op.create_index("ix_bench_run_logical", "bench_run", ["logical_id"])
    op.create_index("ix_bench_run_agent", "bench_run", ["agent_id"])
    op.create_index("ix_bench_run_suite", "bench_run", ["suite_id"])
    op.create_index("ix_bench_run_case", "bench_run", ["case_id"])

    op.create_table(
        "bench_preset",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("modality", sa.String(), nullable=False),  # the §1.2 vocabulary
        sa.Column("logical_id", sa.String(), nullable=True),  # optional tuned-against tag
        sa.Column("params", postgresql.JSONB(), nullable=False),  # literal values only (§1.7)
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )
    op.create_index("ix_bench_preset_modality", "bench_preset", ["modality"])


def downgrade() -> None:
    # Reverse creation order for a clean round-trip.
    op.drop_index("ix_bench_preset_modality", table_name="bench_preset")
    op.drop_table("bench_preset")
    op.drop_index("ix_bench_run_case", table_name="bench_run")
    op.drop_index("ix_bench_run_suite", table_name="bench_run")
    op.drop_index("ix_bench_run_agent", table_name="bench_run")
    op.drop_index("ix_bench_run_logical", table_name="bench_run")
    op.drop_index("ix_bench_run_created", table_name="bench_run")
    op.drop_table("bench_run")
    op.drop_index("ix_bench_case_suite_seq", table_name="bench_case")
    op.drop_table("bench_case")
    op.drop_table("bench_suite")
