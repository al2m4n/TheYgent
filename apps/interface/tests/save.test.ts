// Save-payload + contentHash tests (§4). The frontend sends IR + `view` and DISPLAYS the
// server-computed contentHash — it never computes a hash itself (§1.2). We mock the network at
// `fetch` so this stays a pure unit test of the save path.

import { afterEach, beforeEach, vi } from "vitest";
import { toSavePayload } from "../src/lib/agent";
import type { AgentDetail } from "../src/lib/api";
import { latestHash, saveAgent } from "../src/lib/save";
import { sampleGraph } from "./fixtures";

function mockDetail(contentHash: string): AgentDetail {
  return {
    id: "agent.demo",
    name: "Demo agent",
    created_at: "now",
    updated_at: "now",
    versions: [{ version: "0.1.0", content_hash: contentHash, seq: 1, created_at: "now" }],
  };
}

let calls: { url: string; init: RequestInit }[] = [];

beforeEach(() => {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return {
        ok: true,
        status: 201,
        json: async () => mockDetail("sha256:server-computed-abc123"),
      } as Response;
    }),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("toSavePayload", () => {
  it("sends the IR WITH its view block (the server strips/hashes it)", () => {
    const ir = sampleGraph();
    const payload = toSavePayload(ir);
    expect(payload.ir.view).toBeDefined();
    expect(payload.ir.nodes).toHaveLength(3);
  });
});

describe("saveAgent (§2.4 / §4)", () => {
  it("a new agent POSTs to /agents with { ir } carrying the view", async () => {
    const ir = sampleGraph();
    const detail = await saveAgent(ir, false);

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toMatch(/\/agents$/);
    expect(calls[0].init.method).toBe("POST");
    const body = JSON.parse(calls[0].init.body as string);
    expect(body.ir.id).toBe("agent.demo");
    expect(body.ir.view).toBeDefined(); // view rides along; server strips it
    expect(body.ir.nodes).toHaveLength(3);

    // The displayed hash is whatever the SERVER returned — the FE computed nothing.
    expect(latestHash(detail)).toEqual({
      version: "0.1.0",
      contentHash: "sha256:server-computed-abc123",
    });
  });

  it("an existing agent POSTs to /agents/{id}/versions", async () => {
    await saveAgent(sampleGraph(), true);
    expect(calls[0].url).toMatch(/\/agents\/agent\.demo\/versions$/);
    expect(calls[0].init.method).toBe("POST");
  });

  it("never computes a hash client-side (no crypto/hash call in the payload)", () => {
    const payload = toSavePayload(sampleGraph());
    // The contentHash on the IR is only ever the placeholder/server value — the FE leaves it as-is.
    expect(JSON.stringify(payload)).not.toContain("sha256:server-computed");
  });

  // The common real-world save: a graph the user opened as "new" whose id already exists. The raw
  // API does NOT auto-route; the FE composes 409 agent_exists → add-version (M11 seam #1). This is
  // the most-used path, so it gets a dedicated test rather than riding on the happy create.
  it("create → 409 agent_exists → switches to add-version and shows the new server hash", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        // First call (POST /agents) — the id already exists.
        if (/\/agents$/.test(url)) {
          return {
            ok: false,
            status: 409,
            json: async () => ({ error: { message: "already exists", code: "agent_exists" } }),
          } as Response;
        }
        // Second call (POST /agents/{id}/versions) — succeeds with the persisted hash.
        return {
          ok: true,
          status: 200,
          json: async () => mockDetail("sha256:from-add-version-xyz"),
        } as Response;
      }),
    );

    const detail = await saveAgent(sampleGraph(), false); // FE thinks it's new

    expect(calls).toHaveLength(2);
    expect(calls[0].url).toMatch(/\/agents$/); // tried create first
    expect(calls[1].url).toMatch(/\/agents\/agent\.demo\/versions$/); // then add-version
    expect(calls[1].init.method).toBe("POST");
    // and the displayed hash is the one add-version returned.
    expect(latestHash(detail)?.contentHash).toBe("sha256:from-add-version-xyz");
  });

  it("a non-409 create error is NOT swallowed (no silent add-version)", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return {
          ok: false,
          status: 400,
          json: async () => ({ error: { message: "bad ir", code: "invalid_ir" } }),
        } as Response;
      }),
    );
    await expect(saveAgent(sampleGraph(), false)).rejects.toMatchObject({ code: "invalid_ir" });
    expect(calls).toHaveLength(1); // did NOT fall through to add-version
  });
});
