"""The in-process ingest service — crawl/parse → chunk → embed → upsert, with honest progress.

Deliberately the same shape as the inference plane's model downloader: plain asyncio background
tasks, progress the UI polls, cancel, and a startup sweep (in ``RagStore``) that marks work a
restart interrupted as ``failed`` — cheap honesty, not resume. Durable/resumable ingestion is
an additive upgrade behind this same service seam.

Concurrency model: one task per ingest operation, grouped per source. Uploads may run
concurrently; a crawl is exclusive for its source. The source row's ``status`` says
``ingesting`` while the group is non-empty and settles to ``ready``/``failed``/``cancelled``
when it drains. Every DB write is its own short transaction off the app sessionmaker (the
service outlives any request).

Embeddings go through the gateway seam with the source's LOGICAL model id — the dimension is
whatever the inference plane returns, discovered on the first batch and pinned on the source.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import TYPE_CHECKING, Any

from theygent_control_plane.rag.chunking import Chunk, chunk_markdown, embedding_text
from theygent_control_plane.rag.crawl import CrawlConfig, CrawledPage, crawl_site
from theygent_control_plane.rag.parse import parse_document
from theygent_control_plane.rag.store import RagSource, RagStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)

_EMBED_BATCH = 32


class IngestBusy(RuntimeError):
    """The source already has an exclusive ingest (a crawl) running — surfaced as a 409."""


class _SourceJob:
    """In-memory state for one source's active ingest group (single process, like download
    jobs): live counters flushed to the row's ``progress``, the task set, a crawl-exclusivity
    flag, and the last fatal error for the settle decision.

    ``settle`` is tracked SEPARATELY from ``tasks``, deliberately: the settle task writes the
    terminal status, so it must never be counted as "active work" (or a cancel in the drain
    window would kill the very write that records the cancel) and must never be cancelled —
    only awaited (shutdown) or chained onto (a new drain while the previous settle runs)."""

    def __init__(self) -> None:
        self.tasks: set[asyncio.Task[None]] = set()
        self.settle: asyncio.Task[None] | None = None
        self.crawling = False
        self.counters: dict[str, int] = {"pages": 0, "documents": 0, "chunks": 0, "unchanged": 0}
        self.last_error: str | None = None
        self.cancelled = False


class IngestService:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        gateway: Any,
        *,
        store: RagStore | None = None,
        embed_batch: int = _EMBED_BATCH,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._gateway = gateway
        self._store = store or RagStore()
        self._embed_batch = embed_batch
        self._jobs: dict[str, _SourceJob] = {}

    # ── public surface (the endpoints call these) ────────────────────────

    def is_active(self, source_id: str) -> bool:
        job = self._jobs.get(source_id)
        return job is not None and bool(job.tasks)

    def start_crawl(self, source: RagSource) -> None:
        """Kick off (or restart) the crawl for a ``crawl`` source. Exclusive per source."""
        if self.is_active(source.id):
            raise IngestBusy(f"source {source.id!r} is already ingesting")
        job = self._jobs.setdefault(source.id, _SourceJob())
        job.crawling = True
        job.cancelled = False
        job.counters = {"pages": 0, "documents": 0, "chunks": 0, "unchanged": 0}
        job.last_error = None
        self._spawn(job, source, self._run_crawl(job, source))

    def start_upload(
        self, source: RagSource, *, filename: str, content_type: str | None, data: bytes
    ) -> None:
        """Ingest one uploaded file in the background. Uploads may run concurrently; only a
        running crawl blocks them (it owns the source's progress story)."""
        job = self._jobs.setdefault(source.id, _SourceJob())
        if job.crawling and job.tasks:
            raise IngestBusy(f"source {source.id!r} is mid-crawl; retry when it finishes")
        job.cancelled = False
        self._spawn(job, source, self._run_upload(job, source, filename, content_type, data))

    def cancel(self, source_id: str) -> bool:
        """Cancel every in-flight INGEST task for the source (never the settle task — that is
        the write that records the outcome). Returns False when nothing was active."""
        job = self._jobs.get(source_id)
        if job is None or not job.tasks:
            return False
        job.cancelled = True
        for task in list(job.tasks):
            task.cancel()
        return True

    async def shutdown(self) -> None:
        for job in self._jobs.values():
            for task in list(job.tasks):
                task.cancel()
        pending = [t for job in self._jobs.values() for t in job.tasks]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # The done-callbacks that spawn the settle writes run via call_soon — yield so they
        # fire, then await every settle so its terminal-status write lands before the engine
        # is disposed.
        await asyncio.sleep(0)
        settles = [job.settle for job in self._jobs.values() if job.settle is not None]
        if settles:
            await asyncio.gather(*settles, return_exceptions=True)

    # ── task plumbing ────────────────────────────────────────────────────

    def _spawn(self, job: _SourceJob, source: RagSource, coro: Any) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro)
        job.tasks.add(task)
        task.add_done_callback(lambda t: self._on_done(job, source.id, t))

    def _on_done(self, job: _SourceJob, source_id: str, task: asyncio.Task[None]) -> None:
        job.tasks.discard(task)
        if not job.tasks:
            job.crawling = False
            # Chain onto any still-running settle so two drain windows can't interleave their
            # status writes (last write must reflect the LAST drain).
            previous = job.settle
            settle: asyncio.Task[None] = asyncio.create_task(
                self._settle(job, source_id, after=previous)
            )
            job.settle = settle

            def _finish(t: asyncio.Task[None]) -> None:
                if job.settle is t:
                    job.settle = None
                if not t.cancelled() and t.exception() is not None:
                    # a settle failure must be visible, never swallowed
                    logger.error("rag.settle_failed", extra={"source_id": source_id})

            settle.add_done_callback(_finish)

    async def _settle(
        self, job: _SourceJob, source_id: str, after: asyncio.Task[None] | None = None
    ) -> None:
        """The group drained — decide the source's terminal status honestly."""
        if after is not None:
            await asyncio.gather(after, return_exceptions=True)
        if job.tasks:
            # New work started between the drain and this write — that group's own settle
            # (chained on this task) owns the terminal status; writing one now would race it.
            return
        async with self._sessionmaker() as session, session.begin():
            _, chunks = await self._store.counts(session, source_id)
            if job.cancelled:
                status, error = "cancelled", None
            elif job.last_error is not None and chunks == 0:
                status, error = "failed", job.last_error
            elif chunks == 0:
                status, error = (
                    "failed",
                    "nothing was ingested — no page/document produced any content",
                )
            else:
                # Content exists → the source is usable; a partial failure stays visible as a
                # note (amber, not red — the same honesty style as empty-output runs).
                status, error = "ready", job.last_error
            await self._store.set_source_state(
                session, source_id, status=status, error=error, progress=job.counters
            )
        if chunks:
            async with self._sessionmaker() as session, session.begin():
                source = await self._store.get_source(session, source_id)
                if source is not None and source.embedding_dim is not None:
                    await self._store.ensure_dim_index(session, source.embedding_dim)
        logger.info(
            "rag.ingest_settled",
            extra={"source_id": source_id, "status": status, **job.counters},
        )

    async def _flush_progress(self, job: _SourceJob, source_id: str) -> None:
        async with self._sessionmaker() as session, session.begin():
            await self._store.set_source_state(
                session, source_id, status="ingesting", progress=dict(job.counters)
            )

    # ── the two ingest shapes ────────────────────────────────────────────

    async def _run_crawl(self, job: _SourceJob, source: RagSource) -> None:
        try:
            async with self._sessionmaker() as session, session.begin():
                await self._store.set_source_state(
                    session,
                    source.id,
                    status="ingesting",
                    error=None,
                    progress=dict(job.counters),
                )
            config = CrawlConfig(
                root_url=str(source.config.get("root_url") or "").strip(),
                max_pages=int(source.config.get("max_pages") or CrawlConfig.max_pages),
                render_js=bool(source.config.get("render_js") or False),
            )
            if not config.root_url:
                raise ValueError("crawl source has no root_url configured")

            async def on_visit(_url: str) -> None:
                job.counters["pages"] += 1
                await self._flush_progress(job, source.id)

            async def on_page(page: CrawledPage) -> None:
                await self._ingest_text(
                    job, source, uri=page.url, title=page.title, markdown=page.markdown
                )

            await crawl_site(config, on_page, on_visit=on_visit)
            if job.counters["pages"] == 0:
                job.last_error = (
                    f"crawl fetched no pages from {config.root_url} "
                    "(unreachable, or robots.txt disallows it?)"
                )
        except asyncio.CancelledError:
            job.cancelled = True  # settle() records the cancelled status
        except Exception as exc:
            job.last_error = str(exc)
            logger.warning("rag.crawl_failed", extra={"source_id": source.id, "error": str(exc)})

    async def _run_upload(
        self,
        job: _SourceJob,
        source: RagSource,
        filename: str,
        content_type: str | None,
        data: bytes,
    ) -> None:
        try:
            async with self._sessionmaker() as session, session.begin():
                await self._store.set_source_state(
                    session, source.id, status="ingesting", progress=dict(job.counters)
                )
            parsed = await asyncio.to_thread(
                parse_document, data, filename=filename, content_type=content_type
            )
            await self._ingest_text(
                job, source, uri=filename, title=parsed.title, markdown=parsed.text
            )
        except asyncio.CancelledError:
            job.cancelled = True
        except Exception as exc:
            job.last_error = f"{filename}: {exc}"
            logger.warning(
                "rag.upload_failed",
                extra={"source_id": source.id, "uri": filename, "error": str(exc)},
            )

    # ── the shared pipeline: text → chunks → embeddings → rows ──────────

    async def _ingest_text(
        self, job: _SourceJob, source: RagSource, *, uri: str, title: str | None, markdown: str
    ) -> None:
        content_hash = f"sha256:{hashlib.sha256(markdown.encode('utf-8')).hexdigest()}"
        async with self._sessionmaker() as session:
            current = await self._store.is_document_current(
                session, source_id=source.id, uri=uri, content_hash=content_hash
            )
        if current:
            job.counters["unchanged"] += 1
            await self._flush_progress(job, source.id)
            return

        # Chunk + embed BEFORE any row is touched, then land the whole replacement in one
        # transaction — the last good chunks are never deleted until their successors exist,
        # so a transient embedding outage mid-re-crawl degrades to "stale content + an honest
        # failed row", never to lost retrieval content.
        try:
            chunks = chunk_markdown(markdown)
            embeddings = await self._embed_all(source.embedding_model, chunks)
            async with self._sessionmaker() as session, session.begin():
                if embeddings:
                    dim = len(embeddings[0])
                    authoritative = await self._store.claim_embedding_dim(session, source.id, dim)
                    if authoritative != dim:
                        raise RuntimeError(
                            f"embedding dimension changed mid-source: got {dim}, "
                            f"source is pinned at {authoritative}"
                        )
                else:
                    dim = source.embedding_dim or 0
                await self._store.replace_document(
                    session,
                    source_id=source.id,
                    uri=uri,
                    title=title,
                    content_hash=content_hash,
                    chars=len(markdown),
                    dim=dim,
                    chunks=chunks,
                    embeddings=embeddings,
                )
            job.counters["documents"] += 1
            job.counters["chunks"] += len(chunks)
            await self._flush_progress(job, source.id)
        except asyncio.CancelledError:
            # Nothing was replaced (the landing is atomic) — record the interruption honestly
            # without touching whatever content the document had before.
            async with self._sessionmaker() as session, session.begin():
                await self._store.record_document_failure(
                    session,
                    source_id=source.id,
                    uri=uri,
                    title=title,
                    error="cancelled mid-ingest",
                )
            raise
        except Exception as exc:
            job.last_error = f"{uri}: {exc}"
            async with self._sessionmaker() as session, session.begin():
                await self._store.record_document_failure(
                    session, source_id=source.id, uri=uri, title=title, error=str(exc)
                )
            logger.warning(
                "rag.document_failed",
                extra={"source_id": source.id, "uri": uri, "error": str(exc)},
            )

    async def _embed_all(self, model: str, chunks: list[Chunk]) -> list[list[float]]:
        embeddings: list[list[float]] = []
        for start in range(0, len(chunks), self._embed_batch):
            batch = [embedding_text(c) for c in chunks[start : start + self._embed_batch]]
            response = await self._gateway.embed(model=model, inputs=batch)
            data = sorted(response.data, key=lambda d: d.index)
            embeddings.extend(list(d.embedding) for d in data)
        return embeddings
