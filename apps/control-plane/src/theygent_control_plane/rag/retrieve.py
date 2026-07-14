"""The retrieval backend a ``rag`` node (and the query endpoint) calls.

Injected the same way as the connection resolver and gate backend: the walker never opens a DB
session — this backend owns its sessions, and reaches the embedding model ONLY over the
gateway seam with the source's logical model id (the plane split holds; the control plane
never imports an embedding library).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from theygent_control_plane.rag.store import RagStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    from theygent_gateway_client import GatewayClient


class RetrievalError(RuntimeError):
    """A retrieval-time failure with an actionable message — the executor binds it to ``err``."""


MAX_TOP_K = 50


class RagRetriever:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: Any,
        *,
        store: RagStore | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._gateway: GatewayClient = gateway
        self._store = store or RagStore()

    async def retrieve(
        self,
        *,
        source_id: str,
        query: str,
        top_k: int = 5,
        min_similarity: float | None = None,
    ) -> dict[str, Any]:
        """Embed ``query`` with the source's pinned model, hybrid-search its chunks, and return
        a serializable result (the ``rag`` node's step output / the model-facing tool result).
        Raises :class:`RetrievalError` with an actionable message on every failure shape —
        unknown source, nothing ingested yet, embedding failure."""
        query = (query or "").strip()
        if not query:
            raise RetrievalError("retrieval query is empty")
        top_k = max(1, min(int(top_k), MAX_TOP_K))

        async with self._sessionmaker() as session:
            source = await self._store.get_source(session, source_id)
        if source is None:
            raise RetrievalError(f"unknown rag source {source_id!r}")
        if source.embedding_dim is None or source.chunks == 0:
            raise RetrievalError(
                f"rag source {source.name!r} ({source_id}) has no embedded content yet "
                f"(status: {source.status}) — ingest documents or a crawl first"
            )

        try:
            response = await self._gateway.embed(model=source.embedding_model, inputs=[query])
            query_vector = list(response.data[0].embedding)
        except Exception as exc:
            raise RetrievalError(
                f"query embedding failed on model {source.embedding_model!r}: {exc}"
            ) from exc
        if len(query_vector) != source.embedding_dim:
            raise RetrievalError(
                f"embedding model {source.embedding_model!r} returned {len(query_vector)} "
                f"dimensions but source {source.name!r} was ingested at {source.embedding_dim} — "
                "the model behind the logical id changed; re-ingest the source"
            )

        async with self._sessionmaker() as session:
            matches = await self._store.search(
                session,
                source_id=source_id,
                dim=source.embedding_dim,
                query_text=query,
                query_vector=query_vector,
                top_k=top_k,
                min_similarity=min_similarity,
            )
        return {
            "source_id": source_id,
            "source_name": source.name,
            "query": query,
            "matches": [m.public_dump() for m in matches],
        }
