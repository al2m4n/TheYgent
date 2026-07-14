# RAG

A **rag** node retrieves from a [RAG source](../../rag/index.md) — a named document collection you filled by uploading files or crawling a site. It runs the source's hybrid search (semantic similarity fused with keyword full-text) and returns the best-matching passages, each with its document, heading path, and scores. It is an **activity** node.

Like a [tool node](tools.md), it has two lives: a **step** in the dataflow, or a **capability** the model calls on demand. The capability wiring is what powers "answer from my documents" agents — the model chooses when to search and what to ask.

## Ports

| Port | Direction | Required | Description |
|---|---|---|---|
| `in` | in | no | The query in step mode (used when `query` is not set). Left unfed in capability mode — the model supplies the query. |
| `out` | out | — | The retrieval result on success. |
| `err` | out | — | The error message when retrieval fails. Its port type is `error`. |
| `use` | out | — | The violet capability handle. Wire it into an llm's `tools` port to let the model search. |

## Configuration

| Field | Type | Default | Description |
|---|---|---|---|
| `source` | string | *(required)* | The RAG source's id, picked from a dropdown in the inspector. The id is stable: re-ingesting the source never changes this agent's version. |
| `topK` | int | `5` | How many passages to return (1–50). |
| `minSimilarity` | float | `null` | Optional quality floor on semantic similarity (0–1). Keyword-only matches are not filtered by it — exact-term hits are kept on purpose. |
| `query` | string | `null` | Step mode only: the query as a [`$in` template](../input-references.md). Leave empty to use the `in` port's whole value. Ignored in capability mode. |
| `description` | string | `null` | Capability mode: the "use this when…" hint the model reads to decide when to search. Defaults to a generic knowledge-base description — a specific one ("Search the payments API documentation") makes the model call it at the right times. |

## As a capability (the model searches)

1. Drop a **rag** node and pick its source.
2. Drag its violet **`use`** handle into the llm node's violet **`tools`** port.

The model now sees a function named after the node's id taking one argument, `query`. When it calls, TheYgent runs the retrieval and feeds the matches back as the tool result; the model reads them and answers (usually citing the `uri` of the passages it used). `topK` and `minSimilarity` stay authored knobs — the model cannot override them.

!!! tip "Name the node meaningfully"
    The node **id is the function name** the model sees. `docs_search` beats `n_rag1` — models call well-named functions more reliably.

## As a step (deterministic retrieval)

Wire the node inline with data edges: `input → rag → llm → output`. The query is the `in` port's value, or the rendered `query` template — for example `"$in.in.question"` when the run input is an object. The result binds to `out` and flows downstream, so a prompt can inject the passages itself (`$in` templating on the llm's messages).

## The result shape

```json
{
  "source_id": "rag_01ABC…",
  "source_name": "product-docs",
  "query": "how do I install it",
  "matches": [
    {
      "text": "Install the gadget by plugging …",
      "heading": "Setup > Installation",
      "uri": "https://docs.example.com/setup/",
      "title": "Setup",
      "score": 0.032,
      "similarity": 0.81,
      "document_id": "rdoc_…",
      "chunk_id": "rchk_…",
      "position": 3
    }
  ]
}
```

`score` is the fused hybrid rank (use it for ordering); `similarity` is the raw semantic similarity when the vector leg matched (`null` for a keyword-only hit).

## Failure modes

The rag node follows the tool ok/err contract — a retrieval failure binds a clear message to `err` and the run continues:

- The source exists but has **no embedded content yet** (still ingesting, or every ingest failed).
- The **embedding model is unreachable** at query time.
- The model behind the source's logical id started returning a **different vector dimension** (the id was repointed at another model) — re-ingest the source.

One check happens earlier: an agent whose rag node references a **deleted or unknown source** is rejected before a run is created (`rag_source_not_found`).

Both runtimes execute the node identically; on a [durable run](../../running/durable.md) each retrieval is a journaled step, so a resumed run never re-searches completed steps.

## Related pages

- [RAG sources](../../rag/index.md) — creating and managing the collections this node searches
- [LLM](llm.md) — the `tools` port and the tool-calling loop
- [Referencing inputs](../input-references.md) — the `$in` grammar the `query` template uses
