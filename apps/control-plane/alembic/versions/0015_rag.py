"""rag_source / rag_document / rag_chunk — retrieval storage on pgvector

Retrieval sources live in the SAME Postgres as everything else (the pgvector extension), so
chunks stay transactional with their source rows — FK cascades on delete, one backup story, and
hybrid (vector + full-text) search in a single SQL statement. No second storage engine.

The load-bearing shape decisions:

* ``rag_source.embedding_model`` is a LOGICAL inference-plane id pinned at creation. Vectors from
  different embedding models share no similarity space, so the model is per-source and switching
  it is an explicit re-ingest — never a silent drift. ``embedding_dim`` is discovered from the
  first embedding response (the control plane never imports an embedding library).
* ``rag_chunk.embedding`` is an UNTYPED ``vector`` column: pgvector ANN indexes need a fixed
  dimension but sources pin different models. Queries therefore always filter ``dim = <n>`` and
  cast ``embedding::vector(<n>)`` — the exact expression the per-dimension partial HNSW index
  (created by the ingest service when a dimension first appears; runtime DDL, kept out
  of this chain because the dimensions are data, not schema) is defined over.
* ``rag_chunk.tsv`` is a GENERATED tsvector over the chunk text — the keyword leg of hybrid
  search, fused with vector similarity via reciprocal rank fusion at query time.

Hand-written and reversible. ``downgrade`` drops the tables but not the ``vector``
extension — an extension is database-scoped, other consumers may exist, and leaving it installed
is harmless.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0015_rag"
down_revision: str | None = "0014_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TZ = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "rag_source",
        sa.Column("id", sa.String(), primary_key=True),  # rag_<ulid>
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),  # upload | crawl
        sa.Column("config", postgresql.JSONB(), nullable=False),  # non-secret ingest config
        sa.Column("embedding_model", sa.String(), nullable=False),  # logical id, never an engine
        sa.Column("embedding_dim", sa.Integer(), nullable=True),  # discovered on first embed
        sa.Column("status", sa.String(), nullable=False),  # empty|ingesting|ready|failed|cancelled
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("progress", postgresql.JSONB(), nullable=True),  # live ingest counters
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
    )
    op.create_index("ix_rag_source_created", "rag_source", ["created_at", "id"])

    op.create_table(
        "rag_document",
        sa.Column("id", sa.String(), primary_key=True),  # rdoc_<ulid>
        sa.Column(
            "source_id",
            sa.String(),
            sa.ForeignKey("rag_source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("uri", sa.Text(), nullable=False),  # page url, or the uploaded filename
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),  # sha256 of the extracted text
        sa.Column("status", sa.String(), nullable=False),  # pending|embedded|failed
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("chars", sa.Integer(), nullable=False),
        sa.Column("created_at", _TZ, nullable=False),
        sa.Column("updated_at", _TZ, nullable=False),
        sa.UniqueConstraint("source_id", "uri", name="uq_rag_document_source_uri"),
    )
    op.create_index("ix_rag_document_source", "rag_document", ["source_id"])

    op.create_table(
        "rag_chunk",
        sa.Column("id", sa.String(), primary_key=True),  # rchk_<ulid>
        sa.Column(
            "document_id",
            sa.String(),
            sa.ForeignKey("rag_document.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Denormalized off the document so the hot query (filter by source, order by
        # distance) needs no join.
        sa.Column(
            "source_id",
            sa.String(),
            sa.ForeignKey("rag_source.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=True),  # heading path ("Install > macOS")
        sa.Column("dim", sa.Integer(), nullable=False),  # the index/query predicate
        sa.Column("embedding", Vector(), nullable=False),  # untyped — dim varies per source
        sa.Column(
            "tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('english', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("created_at", _TZ, nullable=False),
    )
    op.create_index("ix_rag_chunk_source", "rag_chunk", ["source_id"])
    op.create_index("ix_rag_chunk_document", "rag_chunk", ["document_id"])
    op.create_index("ix_rag_chunk_tsv", "rag_chunk", ["tsv"], postgresql_using="gin")


def downgrade() -> None:
    # Reverse creation order for a clean round-trip. Per-dimension partial HNSW indexes the
    # ingest service may have created (ix_rag_chunk_hnsw_<dim>) drop with the table.
    op.drop_index("ix_rag_chunk_tsv", table_name="rag_chunk")
    op.drop_index("ix_rag_chunk_document", table_name="rag_chunk")
    op.drop_index("ix_rag_chunk_source", table_name="rag_chunk")
    op.drop_table("rag_chunk")
    op.drop_index("ix_rag_document_source", table_name="rag_document")
    op.drop_table("rag_document")
    op.drop_index("ix_rag_source_created", table_name="rag_source")
    op.drop_table("rag_source")
    # The vector extension stays installed (see module docstring).
