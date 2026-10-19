// The RAG page's document list (RTL over a stubbed control plane): an expanded source loads its
// documents a page at a time and follows the server's `next_after` cursor on Load more, so a
// source with more documents than one page never shows a silently cut list.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Rag } from "../src/routes/Rag";

const SOURCE = {
  id: "rag_01",
  name: "Team docs",
  kind: "upload",
  config: {},
  embedding_model: "embed-small",
  embedding_dim: 384,
  status: "ready",
  error: null,
  progress: null,
  documents: 3,
  chunks: 9,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function doc(id: string, uri: string) {
  return {
    id,
    source_id: SOURCE.id,
    uri,
    title: null,
    status: "embedded",
    error: null,
    chars: 120,
    chunks: 3,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

function jsonResponse(data: unknown): Response {
  return { ok: true, status: 200, json: async () => data } as unknown as Response;
}

function stubControlPlane(): URL[] {
  const documentCalls: URL[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (raw: string) => {
      const url = new URL(raw, "http://localhost");
      const path = url.pathname.replace(/^\/__[a-z]+/, "");
      if (path === "/rag/sources") return jsonResponse({ sources: [SOURCE] });
      if (path === "/rag/sources/rag_01/documents") {
        documentCalls.push(url);
        return url.searchParams.get("after") === "doc_b"
          ? jsonResponse({ documents: [doc("doc_c", "c.md")], next_after: null })
          : jsonResponse({
              documents: [doc("doc_a", "a.md"), doc("doc_b", "b.md")],
              next_after: "doc_b",
            });
      }
      return jsonResponse({});
    }),
  );
  return documentCalls;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("RAG page — document list", () => {
  it("loads documents a page at a time and follows next_after on Load more", async () => {
    const documentCalls = stubControlPlane();
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={client}>
        <Rag />
      </QueryClientProvider>,
    );

    fireEvent.click(await screen.findByTitle("Show documents"));
    expect(await screen.findByText("a.md")).toBeTruthy();
    expect(screen.getByText("b.md")).toBeTruthy();
    expect(screen.getByText("Showing 2 of 3 documents")).toBeTruthy();
    expect(documentCalls[0].searchParams.get("limit")).toBe("100");
    expect(documentCalls[0].searchParams.has("after")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    expect(await screen.findByText("c.md")).toBeTruthy();
    expect(documentCalls[1].searchParams.get("after")).toBe("doc_b");
    // The last page carries no cursor, so the list is complete and offers nothing more.
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load more" })).toBeNull());
    expect(screen.queryByText(/Showing/)).toBeNull();
  });
});
