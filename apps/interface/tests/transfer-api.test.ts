// The transfer api fetchers — wire-shape checks against a stubbed fetch (the rag-api discipline):
// exact paths + methods on BOTH planes' base URLs, the snake_case control bodies vs the camelCase
// inference bodies, the raw-body artifact PUT, and the 204 agent delete.

import { afterEach, describe, expect, it, vi } from "vitest";
import { CONTROL_PLANE_URL, INFERENCE_URL, api } from "../src/lib/api";

function fakeFetch(calls: { url: string; init?: RequestInit }[], payload: unknown, status = 200) {
  return vi.fn(async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return {
      ok: status < 400,
      status,
      json: async () => payload,
    } as unknown as Response;
  });
}

describe("transfer api fetchers", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("exportControlBundle POSTs the include list to /export", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const bundle = { format_version: 1, exported_at: "2026-01-01T00:00:00Z", agents: [] };
    vi.stubGlobal("fetch", fakeFetch(calls, bundle));
    const out = await api.exportControlBundle(["agents", "runs"]);
    expect(calls[0].url).toBe(`${CONTROL_PLANE_URL}/export`);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({ include: ["agents", "runs"] });
    expect(out).toEqual(bundle);
  });

  it("importControlBundle POSTs the bundle to /import and unwraps { report }", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const report = { agents: { created: 1 }, warnings: [{ code: "needs_secret", message: "c1" }] };
    vi.stubGlobal("fetch", fakeFetch(calls, { report }));
    const bundle = { format_version: 1, exported_at: "2026-01-01T00:00:00Z" };
    const out = await api.importControlBundle(bundle);
    expect(calls[0].url).toBe(`${CONTROL_PLANE_URL}/import`);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(bundle);
    expect(out).toEqual(report);
  });

  it("putArtifact PUTs the RAW body under the encoded ref with the content type", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      fakeFetch(calls, { ref: "art_1", contentType: "audio/wav", bytes: 3 }, 201),
    );
    const blob = new Blob(["abc"]);
    const out = await api.putArtifact("art_1", blob, "audio/wav");
    expect(calls[0].url).toBe(`${CONTROL_PLANE_URL}/artifacts/art_1`);
    expect(calls[0].init?.method).toBe("PUT");
    // The body is the blob itself — no JSON wrapper; the mime rides the Content-Type header.
    expect(calls[0].init?.body).toBe(blob);
    expect((calls[0].init?.headers as Record<string, string>)["Content-Type"]).toBe("audio/wav");
    expect(out).toEqual({ ref: "art_1", contentType: "audio/wav", bytes: 3 });
  });

  it("deleteAgent DELETEs the encoded agent id", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string | URL, init?: RequestInit) => {
        calls.push({ url: String(url), init });
        return { ok: true, status: 204 } as unknown as Response;
      }),
    );
    await api.deleteAgent("agent one");
    expect(calls[0].url).toBe(`${CONTROL_PLANE_URL}/agents/${encodeURIComponent("agent one")}`);
    expect(calls[0].init?.method).toBe("DELETE");
  });

  it("exportInferenceBundle GETs the inference plane's /admin/export", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const bundle = { formatVersion: 1, models: [], credentialNames: ["HF_TOKEN"] };
    vi.stubGlobal("fetch", fakeFetch(calls, bundle));
    const out = await api.exportInferenceBundle();
    expect(calls[0].url).toBe(`${INFERENCE_URL}/admin/export`);
    expect(calls[0].init?.method ?? "GET").toBe("GET");
    expect(out).toEqual(bundle);
  });

  it("importInferenceBundle POSTs the camelCase bundle verbatim to /admin/import", async () => {
    const calls: { url: string; init?: RequestInit }[] = [];
    const result = {
      registered: ["m1"],
      skipped: [],
      downloads: [],
      warnings: [],
      credentialNames: ["HF_TOKEN"],
    };
    vi.stubGlobal("fetch", fakeFetch(calls, result));
    const models = [
      { logicalId: "m1", binding: { binding: "llamacpp", source: "hf" }, install: null },
    ];
    // formatVersion and credentialNames ride through — the server's version fence and the
    // credential echo both depend on the client not narrowing the bundle.
    const out = await api.importInferenceBundle({
      formatVersion: 1,
      models,
      credentialNames: ["HF_TOKEN"],
    });
    expect(calls[0].url).toBe(`${INFERENCE_URL}/admin/import`);
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      formatVersion: 1,
      models,
      credentialNames: ["HF_TOKEN"],
    });
    expect(out).toEqual(result);
  });
});
