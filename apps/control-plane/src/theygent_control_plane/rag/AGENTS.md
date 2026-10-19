# AGENTS.md — control plane · `rag/`

Retrieval: named sources (an upload bucket or a crawled docs site), background ingestion
(parse or crawl → chunk → embed → upsert), and hybrid search (pgvector plus Postgres full text,
fused by reciprocal rank) for the `rag` node and the query endpoint. Rules in
[apps/control-plane/AGENTS.md](../../../AGENTS.md) apply first; the design is in the RAG section
of [control-plane.md](../../../../../docs/dev-docs/control-plane.md).

## Where it sits

- **Called by:** the `/rag/sources*` routes in [app.py](../app.py) (CRUD, `/documents` paged
  listing and upload, `:ingest`, `:cancel`, `/query`); the walker's `execute_rag` (a `rag` step,
  or a capability on an `llm` node's `tools` port) and the durable `_rag_step`; the sample
  installer seeds the handbook source; [transfer.py](../transfer.py) exports source definitions.
- **Calls:** the inference plane's `/v1/embeddings` through the app's gateway client, using the
  source's pinned *logical* embedding model id. It never imports an embedding library.
- **Tables:** `rag_source`, `rag_document`, `rag_chunk` (migration `0015_rag`; ORM rows in
  [models.py](../models.py)). Needs the `vector` extension (the pgvector image).
- **Settings:** `rag.*` keys in [settings.py](../settings.py) (chunk size and overlap, crawl
  concurrency, upload cap, default top-k, default embedding model for new sources).

## Map

<!-- agents-map: covers=. -->

| File | What it does |
|------|--------------|
| [`chunking.py`](chunking.py) | Pure. Heading-aware markdown chunking: never crosses a heading, packs blocks to a token budget, blind-splits only an oversized block (with overlap). The embedded text is prefixed with the heading path; the stored text is the raw chunk. Tokens are estimated, never tokenized. |
| [`parse.py`](parse.py) | Uploaded bytes → markdown-ish text through markitdown (plain text and markdown skip it). Runs in a worker thread. No OCR. |
| [`crawl.py`](crawl.py) | `crawl_site`: crawlee for the walk (static HTTP by default, Playwright when the source sets `render_js`), trafilatura for the text. Same origin and under the root URL's path prefix only. A missing browser raises `BrowserNotInstalled` with an install hint. |
| [`store.py`](store.py) | Domain models (`RagSource`, `RagDocument`, `RagMatch`) and `RagStore`: rows ↔ domain, the keyset-paged document listing (an unknown cursor raises `UnknownDocumentCursor`), the startup sweep that marks interrupted ingests `failed`, and the one-statement hybrid search that filters on `dim` and casts `embedding::vector(<dim>)` to hit the per-dimension partial HNSW index. |
| [`ingest.py`](ingest.py) | `IngestService`: one asyncio task per ingest, grouped per source. Uploads may overlap; a crawl is exclusive for its source. Chunks are embedded before any row is replaced, so a failure leaves the old documents intact. Progress lives on the `rag_source` row; `IngestBusy` refuses a conflicting job. |
| [`retrieve.py`](retrieve.py) | `RagRetriever`: the injected backend the walker calls. Embeds the query, runs the store's search, owns its own DB sessions (the walker never opens one). |

## Invariants specific to this package

- **A graph references a source by stable id.** Re-ingesting never changes an agent's
  `contentHash`.
- **A source's embedding model is fixed at creation.** Vectors of different dimensions share a
  table, separated by `dim`.
- **Honest progress, not resume.** A restart marks in-flight ingests `failed`; it never pretends
  they finished.

## Tests

`tests/test_rag.py` (sources, ingest, search, the node in both runtimes; embeddings from the
fake inference plane) and `tests/test_rag_parse.py`.
