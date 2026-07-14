"""Retrieval (RAG) — sources, ingestion, and hybrid search over pgvector.

The package splits along the same seams the rest of the control plane uses:

* ``chunking``  — pure text → chunks (no I/O, unit-testable).
* ``parse``     — uploaded bytes → markdown-ish text (markitdown behind one function).
* ``crawl``     — docs-site crawling (crawlee behind one function; static fetch by default,
                  headless browser when the source opts in).
* ``store``     — domain models + the DB store (rows ↔ domain, hybrid search SQL).
* ``retrieve``  — the injected retrieval backend the walker's ``rag`` node calls
                  (embeds the query over the gateway seam, then queries the store).
* ``ingest``    — the in-process background ingest service (crawl/parse → chunk → embed →
                  upsert), progress on the ``rag_source`` row.
"""

from theygent_control_plane.rag.ingest import IngestService
from theygent_control_plane.rag.retrieve import RagRetriever
from theygent_control_plane.rag.store import RagSource, RagStore

__all__ = ["IngestService", "RagRetriever", "RagSource", "RagStore"]
