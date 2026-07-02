// The loop node's editor support: the validator bounds it (maxIterations ≥ 1), and the inspector
// surfaces that it's durable-only + lets you pin the body by a real version. A loop runs a saved
// body agent a bounded number of times, feeding each output into the next input.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import type { IRDocument } from "@theygent/ir-types";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { validateGraph } from "../src/lib/validate";

vi.mock("../src/lib/api", () => ({
  api: {
    listAgents: vi.fn().mockResolvedValue([{ id: "agent.body" }]),
    getAgent: vi.fn().mockResolvedValue({
      id: "agent.body",
      versions: [
        { version: "0.2.0", content_hash: "sha256:b" },
        { version: "0.1.0", content_hash: "sha256:a" },
      ],
    }),
    listModels: vi.fn().mockResolvedValue([]),
    listConnections: vi.fn().mockResolvedValue([]),
    listMcpServers: vi.fn().mockResolvedValue([]),
    listTriggers: vi.fn().mockResolvedValue([]),
  },
}));

import { Inspector } from "../src/components/Inspector";

// input → loop(body, pinned, maxIterations) → output
function loopGraph(loopConfig: Record<string, unknown>): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "a",
    name: "n",
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: [
      {
        id: "n_in",
        type: "input",
        kind: "boundary",
        config: {},
        ports: { in: [], out: [{ id: "out", type: "any" }] },
      },
      {
        id: "n_loop",
        type: "loop",
        kind: "orchestration",
        config: loopConfig,
        ports: {
          in: [{ id: "in", type: "any", required: true }],
          out: [{ id: "out", type: "any" }],
        },
      },
      {
        id: "n_out",
        type: "output",
        kind: "boundary",
        config: {},
        ports: { in: [{ id: "in", type: "any", required: true }], out: [] },
      },
    ],
    edges: [
      {
        id: "e1",
        source: "n_in",
        sourceHandle: "out",
        target: "n_loop",
        targetHandle: "in",
        channel: "data",
      },
      {
        id: "e2",
        source: "n_loop",
        sourceHandle: "out",
        target: "n_out",
        targetHandle: "in",
        channel: "data",
      },
    ],
  } as IRDocument;
}

const errorsOf = (ir: IRDocument) => validateGraph(ir).filter((i) => i.severity === "error");
const pinned = (maxIterations: unknown) => ({
  agent: "agent.body",
  version: "0.1.0",
  maxIterations,
});

describe("validateGraph — loop bounds", () => {
  it("accepts a bounded loop (maxIterations ≥ 1, one pin)", () => {
    expect(errorsOf(loopGraph(pinned(3)))).toHaveLength(0);
  });

  it("flags maxIterations = 0 (the palette default is invalid)", () => {
    const errs = errorsOf(loopGraph(pinned(0)));
    expect(errs.some((e) => /maxIterations must be a whole number/.test(e.message))).toBe(true);
  });

  it("flags a non-integer maxIterations", () => {
    const errs = errorsOf(loopGraph(pinned(2.5)));
    expect(errs.some((e) => /maxIterations must be a whole number/.test(e.message))).toBe(true);
  });

  it("still flags an ambiguous pin (both version and contentHash)", () => {
    const errs = errorsOf(
      loopGraph({
        agent: "agent.body",
        version: "0.1.0",
        contentHash: "sha256:x",
        maxIterations: 3,
      }),
    );
    expect(errs.some((e) => /EXACTLY ONE of 'version' or 'contentHash'/.test(e.message))).toBe(
      true,
    );
  });
});

function renderInspector(ir: IRDocument, id: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Inspector ir={ir} selection={{ kind: "node", id }} onChange={() => {}} onSelect={() => {}} />
    </QueryClientProvider>,
  );
}

describe("Inspector — loop node", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows the durable-only notice", () => {
    renderInspector(loopGraph(pinned(3)), "n_loop");
    expect(screen.getByText(/Durable-only/)).toBeTruthy();
  });

  it("offers a body-version picker (not a raw pin field)", () => {
    renderInspector(loopGraph(pinned(3)), "n_loop");
    expect(screen.getByText(/body version/i)).toBeTruthy();
  });
});
