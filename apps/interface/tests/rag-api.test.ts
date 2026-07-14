// Retrieval-source api fetchers — wire-shape checks against a stubbed fetch (the same discipline
// as the bench data-plane guard): list unwrapping, the action-suffix routes, and the raw-body
// upload (file mime as Content-Type, encoded filename in the query string — never multipart/JSON).

import { afterEach, describe, expect, it, vi } from "vitest";
import { CONTROL_PLANE_URL, api } from "../src/lib/api";

const SOURCE = {
  id: "rag_01",
  name: "docs",
  kind: "crawl",
  config: {},
  embedding_model: "embed-small",
  embedding_dim: 384,
  status: "ingesting",
  error: null,
  progress: { pages: 3 },
  documents: 3,
  chunks: 40,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function fakeFetch(calls: { url: string; init?: RequestInit }[], payload: unknown) {
  return vi.fn(async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      status: 200,
      json: async () => payload,
    } as unknown as Response;
  });
}

describe("rag api fetchers", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("listRagSources unwraps the { sources } envelope from the control plane", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal("fetch", fakeFetch(calls, { sources: [SOURCE] }));
    const sources = await api.listRagSources();
    expect(calls[0].url).toBe(`${CONTROL_PLANE_URL}/rag/sources`);
    expect(sources).toEqual([SOURCE]);
  });

  it("ingest/cancel POST the action-suffix routes", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal("fetch", fakeFetch(calls, SOURCE));
    await api.ingestRagSource("rag_01");
    await api.cancelRagIngest("rag_01");
    expect(calls.map((c) => c.url)).toEqual([
      `${CONTROL_PLANE_URL}/rag/sources/rag_01:ingest`,
      `${CONTROL_PLANE_URL}/rag/sources/rag_01:cancel`,
    ]);
    for (const c of calls) expect(c.init?.method).toBe("POST");
  });

  it("uploadRagDocument sends the RAW file body with its mime + encoded filename in the query", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal("fetch", fakeFetch(calls, SOURCE));
    const file = new File(["hello"], "notes & drafts.md", { type: "text/markdown" });
    await api.uploadRagDocument("rag_01", file);
    const [{ url, init }] = calls;
    expect(url).toBe(
      `${CONTROL_PLANE_URL}/rag/sources/rag_01/documents?filename=${encodeURIComponent("notes & drafts.md")}`,
    );
    expect(init?.method).toBe("POST");
    // The body is the file itself — no multipart wrapper, no JSON serialization.
    expect(init?.body).toBe(file);
    expect((init?.headers as Record<string, string>)["Content-Type"]).toBe("text/markdown");
  });

  it("queryRagSource POSTs the query body and returns the match envelope", async () => {
    const result = {
      source_id: "rag_01",
      source_name: "docs",
      query: "q",
      matches: [],
    };
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal("fetch", fakeFetch(calls, result));
    const out = await api.queryRagSource("rag_01", { query: "q", top_k: 3 });
    expect(calls[0].url).toBe(`${CONTROL_PLANE_URL}/rag/sources/rag_01/query`);
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ query: "q", top_k: 3 });
    expect(out).toEqual(result);
  });
});
