"""RAG domain entities + the Postgres store (rows ↔ domain, hybrid search).

Same discipline as every other store: methods take a caller-provided ``AsyncSession``, flush,
and never commit — the caller owns the transaction. Domain models are the wire/logic shape;
the ORM rows live in ``models.py``.

Search is hybrid by construction: the vector leg (cosine over pgvector) catches paraphrase,
the full-text leg (tsvector) catches exact terms/identifiers vector similarity fumbles, and
reciprocal rank fusion combines them without score calibration. One SQL statement, no second
search engine. The query always filters ``dim`` and casts ``embedding::vector(<dim>)`` — the
exact expression of the per-dimension partial HNSW index, so it is used when present and the
scan is merely exact (correct, slower) when not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel
from sqlalchemy import delete, func, select, text, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from sqlalchemy.engine import CursorResult

from theygent_control_plane.models import RagChunkRow, RagDocumentRow, RagSourceRow
from theygent_control_plane.rag.chunking import Chunk
from theygent_control_plane.run import new_ulid, now

RagSourceKind = Literal["upload", "crawl"]
RagSourceStatus = Literal["empty", "ingesting", "ready", "failed", "cancelled"]

#: Reciprocal-rank-fusion constant (the standard 60) and the per-leg candidate pool.
_RRF_K = 60
_POOL = 50


def new_rag_source_id() -> str:
    """``rag_<ulid>`` — the stable reference a ``rag`` node's config carries. Hashed content,
    like a connection id: re-ingesting the source never bumps an agent's contentHash."""
    return f"rag_{new_ulid()}"


class RagSource(BaseModel):
    id: str
    name: str
    kind: RagSourceKind
    config: dict[str, Any]
    embedding_model: str
    embedding_dim: int | None = None
    status: RagSourceStatus
    error: str | None = None
    progress: dict[str, Any] | None = None
    documents: int = 0
    chunks: int = 0
    created_at: Any = None
    updated_at: Any = None

    def public_dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RagDocument(BaseModel):
    id: str
    source_id: str
    uri: str
    title: str | None = None
    status: str
    error: str | None = None
    chars: int = 0
    chunks: int = 0
    created_at: Any = None
    updated_at: Any = None

    def public_dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RagMatch(BaseModel):
    """One retrieval hit. ``score`` is the fused RRF rank score (ordering);  ``similarity`` is
    the raw cosine similarity when the vector leg matched (``None`` for a keyword-only hit)."""

    chunk_id: str
    document_id: str
    uri: str
    title: str | None = None
    heading: str | None = None
    position: int
    text: str
    score: float
    similarity: float | None = None

    def public_dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def _source_domain(row: RagSourceRow, *, documents: int = 0, chunks: int = 0) -> RagSource:
    return RagSource(
        id=row.id,
        name=row.name,
        kind=cast("RagSourceKind", row.kind),
        config=row.config or {},
        embedding_model=row.embedding_model,
        embedding_dim=row.embedding_dim,
        status=cast("RagSourceStatus", row.status),
        error=row.error,
        progress=row.progress,
        documents=documents,
        chunks=chunks,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class RagStore:
    """CRUD + ingest bookkeeping + hybrid search for retrieval sources."""

    # ── sources ──────────────────────────────────────────────────────────

    async def create_source(
        self,
        session: AsyncSession,
        *,
        name: str,
        kind: RagSourceKind,
        config: dict[str, Any],
        embedding_model: str,
    ) -> RagSource:
        row = RagSourceRow(
            id=new_rag_source_id(),
            name=name,
            kind=kind,
            config=config,
            embedding_model=embedding_model,
            embedding_dim=None,
            status="empty",
            error=None,
            progress=None,
            created_at=now(),
            updated_at=now(),
        )
        session.add(row)
        await session.flush()
        return _source_domain(row)

    async def find_source_by_name(self, session: AsyncSession, *, name: str) -> RagSource | None:
        """The newest source with exactly this ``name``. Names are human labels, not unique —
        newest-first keeps the lookup deterministic. The sample installer uses this to reuse a
        previously created sample source instead of minting one per install."""
        row = (
            (
                await session.execute(
                    select(RagSourceRow)
                    .where(RagSourceRow.name == name)
                    .order_by(RagSourceRow.created_at.desc(), RagSourceRow.id.desc())
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )
        return _source_domain(row) if row is not None else None

    async def source_exists(self, session: AsyncSession, source_id: str) -> bool:
        """Existence only — no counts. The up-front run check and the upload gate call this on
        hot paths where ``get_source``'s two COUNT aggregations are waste."""
        found = (
            await session.execute(select(RagSourceRow.id).where(RagSourceRow.id == source_id))
        ).scalar_one_or_none()
        return found is not None

    async def get_source(self, session: AsyncSession, source_id: str) -> RagSource | None:
        row = await session.get(RagSourceRow, source_id)
        if row is None:
            return None
        documents, chunks = await self.counts(session, source_id)
        return _source_domain(row, documents=documents, chunks=chunks)

    async def list_sources(
        self, session: AsyncSession, *, limit: int = 100, before: str | None = None
    ) -> list[RagSource]:
        """Sources, newest first — keyset pagination on ``(created_at, id)`` DESC, mirroring
        the other list stores; an unknown ``before`` id is ignored."""
        docs_sq = (
            select(func.count())
            .select_from(RagDocumentRow)
            .where(RagDocumentRow.source_id == RagSourceRow.id)
            .scalar_subquery()
        )
        chunks_sq = (
            select(func.count())
            .select_from(RagChunkRow)
            .where(RagChunkRow.source_id == RagSourceRow.id)
            .scalar_subquery()
        )
        stmt = select(RagSourceRow, docs_sq, chunks_sq).order_by(
            RagSourceRow.created_at.desc(), RagSourceRow.id.desc()
        )
        if before:
            anchor = await session.get(RagSourceRow, before)
            if anchor is not None:
                stmt = stmt.where(
                    tuple_(RagSourceRow.created_at, RagSourceRow.id)
                    < (anchor.created_at, anchor.id)
                )
        rows = (await session.execute(stmt.limit(limit))).all()
        return [_source_domain(r, documents=d, chunks=c) for r, d, c in rows]

    async def update_source(
        self,
        session: AsyncSession,
        source_id: str,
        *,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> RagSource | None:
        row = await session.get(RagSourceRow, source_id)
        if row is None:
            return None
        if name is not None:
            row.name = name
        if config is not None:
            row.config = config
        row.updated_at = now()
        await session.flush()
        documents, chunks = await self.counts(session, source_id)
        return _source_domain(row, documents=documents, chunks=chunks)

    async def delete_source(self, session: AsyncSession, source_id: str) -> bool:
        row = await session.get(RagSourceRow, source_id)
        if row is None:
            return False
        await session.delete(row)  # documents + chunks cascade (FK ondelete)
        await session.flush()
        return True

    async def counts(self, session: AsyncSession, source_id: str) -> tuple[int, int]:
        documents = (
            await session.execute(
                select(func.count())
                .select_from(RagDocumentRow)
                .where(RagDocumentRow.source_id == source_id)
            )
        ).scalar_one()
        chunks = (
            await session.execute(
                select(func.count())
                .select_from(RagChunkRow)
                .where(RagChunkRow.source_id == source_id)
            )
        ).scalar_one()
        return documents, chunks

    # ── ingest bookkeeping ───────────────────────────────────────────────

    async def set_source_state(
        self,
        session: AsyncSession,
        source_id: str,
        *,
        status: RagSourceStatus | None = None,
        error: str | None | Literal[False] = False,
        progress: dict[str, Any] | None | Literal[False] = False,
    ) -> None:
        """Ingest-lifecycle writes. ``False`` (the sentinel) = leave the column untouched, so a
        progress tick doesn't clear a prior error and vice versa."""
        values: dict[str, Any] = {"updated_at": now()}
        if status is not None:
            values["status"] = status
        if error is not False:
            values["error"] = error
        if progress is not False:
            values["progress"] = progress
        await session.execute(
            update(RagSourceRow).where(RagSourceRow.id == source_id).values(**values)
        )

    async def claim_embedding_dim(self, session: AsyncSession, source_id: str, dim: int) -> int:
        """Record the dimension the embedding model actually returned, first writer wins.
        Returns the authoritative dim (an existing different value means a concurrent ingest
        already pinned it — the caller compares and fails its document honestly)."""
        await session.execute(
            update(RagSourceRow)
            .where(RagSourceRow.id == source_id, RagSourceRow.embedding_dim.is_(None))
            .values(embedding_dim=dim, updated_at=now())
        )
        row = await session.get(RagSourceRow, source_id)
        assert row is not None
        return int(row.embedding_dim) if row.embedding_dim is not None else dim

    async def is_document_current(
        self, session: AsyncSession, *, source_id: str, uri: str, content_hash: str
    ) -> bool:
        """True when the document at ``uri`` is already embedded with exactly this text hash —
        the caller skips re-chunking/re-embedding (the re-crawl economy). Read-only."""
        existing = (
            await session.execute(
                select(RagDocumentRow.content_hash, RagDocumentRow.status).where(
                    RagDocumentRow.source_id == source_id, RagDocumentRow.uri == uri
                )
            )
        ).one_or_none()
        return (
            existing is not None
            and existing.content_hash == content_hash
            and existing.status == "embedded"
        )

    async def _document_row(
        self, session: AsyncSession, *, source_id: str, uri: str, title: str | None
    ) -> RagDocumentRow:
        row = (
            await session.execute(
                select(RagDocumentRow).where(
                    RagDocumentRow.source_id == source_id, RagDocumentRow.uri == uri
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = RagDocumentRow(
                id=f"rdoc_{new_ulid()}",
                source_id=source_id,
                uri=uri,
                title=title,
                content_hash=None,
                status="pending",
                error=None,
                chars=0,
                created_at=now(),
                updated_at=now(),
            )
            session.add(row)
        return row

    async def replace_document(
        self,
        session: AsyncSession,
        *,
        source_id: str,
        uri: str,
        title: str | None,
        content_hash: str,
        chars: int,
        dim: int,
        chunks: list[Chunk],
        embeddings: list[list[float]],
    ) -> str:
        """Land a fully-embedded document in ONE transaction: upsert the row, drop the previous
        chunks, insert the new ones, mark ``embedded``. Called only AFTER embedding succeeded —
        the last good chunks must never be deleted before their replacement exists, or a
        transient embedding outage during a re-crawl becomes retrieval data loss."""
        assert len(chunks) == len(embeddings)
        row = await self._document_row(session, source_id=source_id, uri=uri, title=title)
        await session.flush()  # a fresh row needs its PK before the delete/inserts reference it
        await session.execute(delete(RagChunkRow).where(RagChunkRow.document_id == row.id))
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            session.add(
                RagChunkRow(
                    id=f"rchk_{new_ulid()}",
                    document_id=row.id,
                    source_id=source_id,
                    position=chunk.position,
                    text=chunk.text,
                    heading=chunk.heading,
                    dim=dim,
                    embedding=embedding,
                    created_at=now(),
                )
            )
        row.title = title
        row.content_hash = content_hash
        row.chars = chars
        row.status = "embedded"
        row.error = None
        row.updated_at = now()
        await session.flush()
        return row.id

    async def record_document_failure(
        self, session: AsyncSession, *, source_id: str, uri: str, title: str | None, error: str
    ) -> str:
        """Record a failed fetch/parse/embed WITHOUT touching any existing chunks: the last good
        content keeps serving retrieval while the row honestly says the latest attempt failed
        (and, having left ``embedded``, is retried by the next ingest — no stale-hash skip)."""
        row = await self._document_row(session, source_id=source_id, uri=uri, title=title)
        row.status = "failed"
        row.error = error
        row.updated_at = now()
        await session.flush()
        return row.id

    async def list_documents(
        self, session: AsyncSession, source_id: str, *, limit: int = 500
    ) -> list[RagDocument]:
        chunks_sq = (
            select(func.count())
            .select_from(RagChunkRow)
            .where(RagChunkRow.document_id == RagDocumentRow.id)
            .scalar_subquery()
        )
        rows = (
            await session.execute(
                select(RagDocumentRow, chunks_sq)
                .where(RagDocumentRow.source_id == source_id)
                .order_by(RagDocumentRow.created_at.asc(), RagDocumentRow.id.asc())
                .limit(limit)
            )
        ).all()
        return [
            RagDocument(
                id=r.id,
                source_id=r.source_id,
                uri=r.uri,
                title=r.title,
                status=r.status,
                error=r.error,
                chars=r.chars,
                chunks=c,
                created_at=r.created_at,
                updated_at=r.updated_at,
            )
            for r, c in rows
        ]

    async def reconcile_orphaned_sources(self, session: AsyncSession) -> int:
        """Startup honesty sweep, mirroring the run sweep: an ingest interrupted by a restart
        can never lie about still running. Whitelist of in-flight states only."""
        docs = await session.execute(
            update(RagDocumentRow)
            .where(RagDocumentRow.status == "pending")
            .values(
                status="failed",
                error="interrupted: control-plane restarted while the document was ingesting",
                updated_at=now(),
            )
        )
        sources = await session.execute(
            update(RagSourceRow)
            .where(RagSourceRow.status == "ingesting")
            .values(
                status="failed",
                error="interrupted: control-plane restarted while the source was ingesting",
                updated_at=now(),
            )
        )
        swept_sources = cast("CursorResult[Any]", sources).rowcount or 0
        swept_docs = cast("CursorResult[Any]", docs).rowcount or 0
        return swept_sources + swept_docs

    # ── search ───────────────────────────────────────────────────────────

    async def ensure_dim_index(self, session: AsyncSession, dim: int) -> None:
        """Create the partial HNSW index for one embedding dimension, idempotently. Runtime DDL
        by design: dimensions are data (each source pins a model), so the index set can't be a
        static migration. Cheap at this scale; plain CREATE INDEX (brief write lock) suffices."""
        dim = int(dim)  # defense in depth — interpolated into DDL below
        await session.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_rag_chunk_hnsw_{dim} ON rag_chunk "
                f"USING hnsw ((embedding::vector({dim})) vector_cosine_ops) WHERE dim = {dim}"
            )
        )

    async def search(
        self,
        session: AsyncSession,
        *,
        source_id: str,
        dim: int,
        query_text: str,
        query_vector: list[float],
        top_k: int = 5,
        min_similarity: float | None = None,
    ) -> list[RagMatch]:
        """Hybrid retrieval: top-``_POOL`` by cosine distance, top-``_POOL`` by full-text rank,
        fused with RRF, top-``top_k`` returned. ``min_similarity`` gates the vector leg only —
        a keyword-only hit has no similarity to gate on and exact-term matches are precisely
        what that leg exists to keep."""
        dim = int(dim)  # typmod can't be a bind parameter; everything else is bound
        qvec = "[" + ",".join(str(float(v)) for v in query_vector) + "]"
        stmt = text(
            f"""
            WITH vec AS (
                SELECT id,
                       (embedding::vector({dim})) <=> (:qvec)::vector({dim}) AS distance,
                       row_number() OVER (
                           ORDER BY (embedding::vector({dim})) <=> (:qvec)::vector({dim})
                       ) AS rank
                FROM rag_chunk
                WHERE source_id = :source_id AND dim = {dim}
                ORDER BY distance
                LIMIT :pool
            ),
            fts AS (
                SELECT id,
                       row_number() OVER (
                           ORDER BY ts_rank_cd(tsv, websearch_to_tsquery('english', :query)) DESC
                       ) AS rank
                FROM rag_chunk
                WHERE source_id = :source_id AND dim = {dim}
                  AND tsv @@ websearch_to_tsquery('english', :query)
                LIMIT :pool
            ),
            fused AS (
                SELECT COALESCE(vec.id, fts.id) AS id,
                       COALESCE(1.0 / (:rrf_k + vec.rank), 0)
                     + COALESCE(1.0 / (:rrf_k + fts.rank), 0) AS score,
                       vec.distance AS distance
                FROM vec FULL OUTER JOIN fts ON vec.id = fts.id
            )
            SELECT c.id AS chunk_id, c.document_id, c.text, c.heading, c.position,
                   d.uri, d.title, f.score, f.distance
            FROM fused f
            JOIN rag_chunk c ON c.id = f.id
            JOIN rag_document d ON d.id = c.document_id
            ORDER BY f.score DESC, c.id
            LIMIT :top_k
            """
        )
        rows = (
            await session.execute(
                stmt,
                {
                    "qvec": qvec,
                    "source_id": source_id,
                    "query": query_text,
                    "pool": _POOL,
                    "rrf_k": _RRF_K,
                    "top_k": top_k,
                },
            )
        ).mappings()
        matches: list[RagMatch] = []
        for row in rows:
            similarity = None if row["distance"] is None else 1.0 - float(row["distance"])
            if (
                min_similarity is not None
                and similarity is not None
                and similarity < min_similarity
            ):
                continue
            matches.append(
                RagMatch(
                    chunk_id=row["chunk_id"],
                    document_id=row["document_id"],
                    uri=row["uri"],
                    title=row["title"],
                    heading=row["heading"],
                    position=row["position"],
                    text=row["text"],
                    score=float(row["score"]),
                    similarity=similarity,
                )
            )
        return matches
