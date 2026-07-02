// Durable-only detection: an agent that contains a durable-only node (human/subgraph/loop/map) can't
// run on the interactive path, so the Bench routes it to the durable-run endpoint. This mirrors the
// backend _DURABLE_ONLY_TYPES set.

import type { IRDocument } from "@theygent/ir-types";
import { describe, expect, it } from "vitest";
import { DURABLE_ONLY, isDurableOnly } from "../src/lib/durable";

function graphWith(type: string): IRDocument {
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
        ports: { in: [], out: [{ id: "out", type: "any" }] },
      },
      {
        id: "n_x",
        type,
        kind: "orchestration",
        ports: { in: [{ id: "in", type: "any" }], out: [{ id: "out", type: "any" }] },
      },
      {
        id: "n_out",
        type: "output",
        kind: "boundary",
        ports: { in: [{ id: "in", type: "any" }], out: [] },
      },
    ],
    edges: [],
  } as IRDocument;
}

describe("isDurableOnly", () => {
  it("is true when any durable-only node is present", () => {
    for (const t of DURABLE_ONLY) {
      expect(isDurableOnly(graphWith(t))).toBe(true);
    }
  });

  it("is false for a plain interactive graph (input → llm → output)", () => {
    expect(isDurableOnly(graphWith("llm"))).toBe(false);
    expect(isDurableOnly(graphWith("tool"))).toBe(false);
    expect(isDurableOnly(graphWith("router"))).toBe(false);
  });

  it("mirrors the backend durable-only set exactly", () => {
    expect([...DURABLE_ONLY].sort()).toEqual(["human", "loop", "map", "subgraph"]);
  });
});
