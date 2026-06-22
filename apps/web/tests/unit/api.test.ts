import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, streamRun } from "../../src/lib/api";

// The apiClient is the ONE auth seam (M8 §3.3): every request must carry the bearer header.
// These tests pin that the header is injected (default + overridable via localStorage) and
// that error bodies map to a typed ApiError — so the later real-auth swap is a one-file change.

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// A minimal in-memory localStorage so the test is independent of the JS environment (Node's
// native Web Storage vs jsdom's differ) — the apiClient only needs getItem/setItem.
function fakeStorage(): Storage {
  const m = new Map<string, string>();
  return {
    getItem: (k) => m.get(k) ?? null,
    setItem: (k, v) => void m.set(k, String(v)),
    removeItem: (k) => void m.delete(k),
    clear: () => m.clear(),
    key: (i) => [...m.keys()][i] ?? null,
    get length() {
      return m.size;
    },
  } as Storage;
}

describe("apiClient header injection", () => {
  beforeEach(() => {
    vi.stubGlobal("localStorage", fakeStorage());
    vi.restoreAllMocks();
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("sends Authorization: Bearer dev-local by default", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ runs: [] }));
    await api.listRuns();
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer dev-local");
  });

  it("uses a stored token when present (the real-auth seam)", async () => {
    localStorage.setItem("theygent.token", "real-token-123");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse({ runs: [] }));
    await api.listRuns();
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer real-token-123");
  });

  it("maps a backend error body to a typed ApiError", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: { message: "unknown run", code: "run_not_found" } }, 404),
    );
    await expect(api.getRun("nope")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      code: "run_not_found",
    });
  });

  // Error translation at the SPA boundary (the consumer side of the M3–M7 error fidelity):
  // the apiClient must surface the backend's human message + code, NOT a bare status line.
  // If this regresses to showing "503 Service Unavailable" instead of the real reason, the
  // UI silently gets worse — these pin that it can't.
  it("translates 503 engine_unavailable to its readable message, not a bare status", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        {
          error: {
            message: "engine for 'triage-fast' is not available on this host",
            code: "engine_unavailable",
            type: "server_error",
          },
        },
        503,
      ),
    );
    const err = await api.getRun("r1").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(503);
    expect(err.code).toBe("engine_unavailable");
    expect(err.message).toBe("engine for 'triage-fast' is not available on this host");
    expect(err.message).not.toMatch(/^503\b/); // never the bare status line
  });

  it("translates 404 model_not_found to its readable message", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse(
        {
          error: {
            message: "unknown logical id 'ghost' (the model field is a logical id)",
            code: "model_not_found",
            type: "invalid_request_error",
          },
        },
        404,
      ),
    );
    const err = await api.getRun("r1").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("model_not_found");
    expect(err.message).toMatch(/unknown logical id 'ghost'/);
  });

  it("falls back to the status line only when the error body is not JSON", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("upstream exploded", { status: 502, statusText: "Bad Gateway" }),
    );
    const err = await api.getRun("r1").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(502);
    expect(err.message).toMatch(/502/); // no JSON body to translate → status line is honest
  });

  it("streamRun injects the bearer header and surfaces a pre-stream error", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      jsonResponse({ error: { message: "bad ir", code: "invalid_ir" } }, 400),
    );
    await expect(streamRun("/graphs/runs", {})).rejects.toBeInstanceOf(ApiError);
  });

  it("streamRun returns an event iterator on a 200 SSE body", async () => {
    const enc = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(enc.encode('event: run\ndata: {"runId":"r1"}\n\n'));
        c.close();
      },
    });
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(body, { status: 200 }));
    const handle = await streamRun("/runs", { input: "hi", model: "m" });
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer dev-local");
    const first = await handle.events.next();
    expect(first.value).toEqual({ event: "run", data: '{"runId":"r1"}' });
  });
});
