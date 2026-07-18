# RAG sources

A **RAG source** is a named collection of documents your agents can search — your product docs, a contract folder, an internal wiki. You fill it once (upload files or crawl a site), TheYgent chunks and embeds the text against a model *you* choose, and stores the vectors in your own Postgres. From then on, any agent can retrieve from it through the [rag node](../building/nodes/rag.md): as a fixed pipeline step, or as a tool the model calls on demand.

RAG — retrieval-augmented generation — exists because a document collection never fits a model's context window. Instead of pasting documents into prompts, the agent asks a search question, gets back the handful of most relevant passages, and answers from those. Everything stays in your trust domain: the documents, the vectors, and the embedding calls (which go to your inference plane, not a third party).

You manage sources on the **RAG** page in the sidebar.

## Before you start: an embedding model

Retrieval needs an **embedding model** — a small model that turns text into vectors so that similar meanings land near each other. Chat models can't do this; it is its own modality, served by your inference plane like any other model:

- **Local (recommended):** register an embedding GGUF with llama.cpp — for example `nomic-embed-text-v1.5` (768 dimensions, ~84 MB) — with `modality: embeddings`. See [Installing local models](../models/installing.md) and the [model management API](../reference/api.md).
- **Hosted:** any `openai-compatible` binding whose upstream serves `/v1/embeddings` works the same way.

Two things to know:

1. **The model is pinned per source.** Vectors from different embedding models are not comparable, so each source locks its model at creation. Changing it later means creating a new source (or re-ingesting into one). The source's create form only offers this choice once, on purpose.
2. **It's a logical id, like every model in TheYgent.** The source stores the id (say `nomic-embed`), and your inference plane decides what serves it — swap the engine behind the id any time.

!!! tip "Pooling"
    Most embedding GGUFs work out of the box (`mean` pooling). Models that pool on the last token can set `params: {"pooling": "last"}` on their registration.

## Creating a source

Click **New source** on the RAG page. Every source has a name, a kind, and its embedding model.

### Upload sources

An **upload** source is a bucket you drop files into. Two ways in, same result:

- the **Upload files** button (multi-select file picker), or
- **drag & drop** files anywhere onto the source's row — it highlights while you hover.

Supported formats: PDF, DOCX, PPTX, XLSX, Markdown, plain text, HTML, CSV, JSON, and EPUB, up to 50 MiB per file. Unsupported files are skipped with a notice, and one bad file never stops the rest of a batch. Text-based PDFs extract well; scanned/image-only PDFs are not OCR'd yet.

### Crawl sources

A **crawl** source ingests a website — point it at a docs root and TheYgent walks the pages:

| Field | What it does |
|---|---|
| **Root URL** | Where the crawl starts. The crawl stays on the same origin **and under the root's path** — pointing at `https://example.com/docs` never wanders into the rest of the site. |
| **Max pages** | The crawl budget (default 200). The crawl stops when it runs out, whatever is left. |
| **Render JavaScript** | Off by default. Turn it on for script-rendered sites; it uses a headless browser, which needs a one-time `playwright install chromium` on the machine running the control plane. Most docs sites don't need it. |

The crawler respects `robots.txt`, fetches politely (a few pages at a time), and strips navigation/boilerplate so only each page's main content is ingested. Click **Crawl** to start (creating the source starts the first crawl automatically) and **Re-crawl** any time the site changes — unchanged pages are detected by content hash and skipped, so a re-crawl only re-embeds what actually changed.

## Watching an ingest

Ingestion runs in the background. A progress card appears bottom-right with live counters — pages fetched, documents embedded, chunks stored — and survives navigating away or reloading the page. You can **Cancel** from the card; the source row shows the same status (`ingesting` → `ready`, `failed`, or `cancelled`).

Statuses are honest:

- **ready** — the source has searchable content. If a few pages failed along the way, the source is still `ready` and the error note says what went wrong.
- **failed** — nothing usable was ingested (site unreachable, embedding model down, …), or a restart interrupted the job.
- A failed re-ingest **never destroys what you already had**: new content replaces old only after it has embedded successfully, so a transient outage leaves the previous content serving.

Expand a source row (click its name) to see every document with its status, chunk count, and any per-document error.

## Testing retrieval

Every source row has a **Query** button — an inline search box that runs *exactly* the retrieval a rag node would run, so you can check what an agent would see before wiring anything:

- Each match shows its score, the document it came from, its heading path (e.g. `Install > macOS`), and the passage text.
- **sim** is the semantic (cosine) similarity when the vector leg matched. Search is **hybrid**: meaning-based similarity is fused with keyword full-text search, so paraphrases *and* exact identifiers both rank.

If the results look wrong, that's a signal to fix the source (crawl scope, missing documents) — not the agent.

## Using a source in an agent

Drop a **rag** node in the editor and pick the source in its inspector. Two wirings:

- **As a capability** — drag the node's violet `use` handle into an llm's `tools` port. The model decides when to search and what to ask; it receives the matching passages (with their source URLs) as a tool result and answers from them. This is the usual shape for a "chat with my docs" agent.
- **As a step** — wire it inline with data edges; the in-port value (or a `$in` template) becomes the query and the matches flow downstream.

See the [rag node reference](../building/nodes/rag.md) for ports, configuration, and the result shape.

Agents reference a source by its stable id, so re-crawling or uploading more documents **never changes a published agent's version** — the agent always searches the source's current content.

## Managing sources

- The list has the same **search box and filter chips** (kind, status) as the MCP page.
- **Rename** and crawl-config changes are a `PATCH` away (the API) — the id, and therefore every agent reference, is unaffected.
- **Delete** removes the source with all its documents and vectors (a confirmation dialog stands in the way). Agents that still reference it will refuse to run with a clear `rag_source_not_found` error.

## Related pages

- [The rag node](../building/nodes/rag.md) — ports, config, capability vs step
- [Installing local models](../models/installing.md) — getting an embedding model
- [API reference](../reference/api.md) — the `/rag/*` endpoints
