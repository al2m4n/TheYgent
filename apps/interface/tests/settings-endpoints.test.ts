// Browser-local endpoint overrides + the settings wire calls: overrides persist to localStorage
// (trailing slash stripped), clear on null/blank, and apply at RESOLVE time — every HTTP call
// picks up the current base, both planes. The settings functions hit the frozen paths with the
// right methods and bodies.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  CONTROL_PLANE_URL,
  INFERENCE_URL,
  api,
  controlPlaneUrl,
  getEndpointOverrides,
  inferenceUrl,
  setEndpointOverride,
} from "../src/lib/api";

function json(data: unknown, status = 200): Response {
  return { ok: status < 400, status, json: async () => data } as unknown as Response;
}

// This test environment ships no localStorage (the app code treats it as optional) — stub an
// in-memory one so the override persistence path is actually exercised.
function stubLocalStorage(): void {
  const store = new Map<string, string>();
  vi.stubGlobal("localStorage", {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => void store.clear(),
    key: (i: number) => [...store.keys()][i] ?? null,
    get length() {
      return store.size;
    },
  } as Storage);
}

beforeEach(stubLocalStorage);

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("endpoint overrides", () => {
  it("persists to localStorage with the trailing slash stripped, and clears on null", () => {
    setEndpointOverride("control", "http://other-host:9999/");
    expect(localStorage.getItem("theygent.url.control")).toBe("http://other-host:9999");
    expect(getEndpointOverrides().control).toBe("http://other-host:9999");
    expect(controlPlaneUrl()).toBe("http://other-host:9999");

    setEndpointOverride("control", null);
    expect(localStorage.getItem("theygent.url.control")).toBeNull();
    expect(getEndpointOverrides().control).toBeNull();
    expect(controlPlaneUrl()).toBe(CONTROL_PLANE_URL);
  });

  it("a blank string clears like null", () => {
    setEndpointOverride("inference", "http://gpu-box:8081");
    expect(inferenceUrl()).toBe("http://gpu-box:8081");
    setEndpointOverride("inference", "   ");
    expect(getEndpointOverrides().inference).toBeNull();
    expect(inferenceUrl()).toBe(INFERENCE_URL);
  });

  it("resolves the base PER CALL — an override set between calls applies to the next one", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        calls.push(url);
        return json({ settings: [], boot: [], orphaned: [], runs: [], models: [] });
      }),
    );

    await api.getSettings();
    expect(calls[0]).toBe(`${CONTROL_PLANE_URL}/settings`);

    setEndpointOverride("control", "http://cp-elsewhere:1234");
    await api.getSettings();
    expect(calls[1]).toBe("http://cp-elsewhere:1234/settings");

    setEndpointOverride("inference", "http://inf-elsewhere:4321");
    await api.getInferenceSettings();
    expect(calls[2]).toBe("http://inf-elsewhere:4321/admin/settings");

    // The overrides reach EVERY call path, not just the settings functions.
    await api.listRuns();
    expect(calls[3]).toBe("http://cp-elsewhere:1234/runs");
    await api.listModels();
    expect(calls[4]).toBe("http://inf-elsewhere:4321/admin/models");
  });
});

describe("settings wire calls", () => {
  it("hits the frozen paths with the right methods and bodies", async () => {
    const seen: { url: string; method: string; body?: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        seen.push({
          url,
          method: init?.method ?? "GET",
          body: init?.body ? JSON.parse(init.body as string) : undefined,
        });
        return json({});
      }),
    );

    await api.patchSettings({ "rag.default_top_k": 8, "telemetry.io_capture": null });
    await api.testOtlp({ endpoint: "http://collector:4318" });
    await api.patchInferenceSettings({ maxResident: null });
    await api.getInferenceDiagnostics();

    expect(seen[0]).toMatchObject({
      url: `${CONTROL_PLANE_URL}/settings`,
      method: "PATCH",
      body: { "rag.default_top_k": 8, "telemetry.io_capture": null },
    });
    expect(seen[1]).toMatchObject({
      url: `${CONTROL_PLANE_URL}/settings/otlp:test`,
      method: "POST",
      body: { endpoint: "http://collector:4318" },
    });
    expect(seen[2]).toMatchObject({
      url: `${INFERENCE_URL}/admin/settings`,
      method: "PATCH",
      body: { maxResident: null },
    });
    expect(seen[3]).toMatchObject({
      url: `${INFERENCE_URL}/admin/diagnostics`,
      method: "GET",
    });
  });
});
