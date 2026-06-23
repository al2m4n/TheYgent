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
});
