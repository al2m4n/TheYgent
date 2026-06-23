// Realistic IR fixtures for the adapter tests. Shaped like a STORED agent version (M11): every
// optional field default-filled (the server hashes `ir.model_dump(exclude_none=False)`, D2), so a
// load→render→reconstruct round-trip is field-for-field identical. `view` is re-attached as the
// loader does (lib/agent.fromStoredVersion).

import type { IRDocument } from "@theygent/ir-types";

export function sampleGraph(): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "agent.demo",
    name: "Demo agent",
    version: "0.1.0",
    contentHash: "sha256:placeholder-server-owns-this",
    metadata: null,
    models: {
      default: { binding: "mlx", model: "local-fast", source: null, params: {} },
    },
    tools: {},
    nodes: [
      {
        id: "n_in",
        type: "input",
        kind: "boundary",
        label: null,
        config: {},
        ports: { in: [], out: [{ id: "out", type: "any", required: true }] },
      },
      {
        id: "n_llm",
        type: "llm",
        kind: "activity",
        label: "answer",
        config: {
          model: "default",
          messages: [{ role: "user", content: "$in" }],
        },
        ports: {
          in: [{ id: "in", type: "any", required: true }],
          out: [{ id: "out", type: "any", required: true }],
        },
      },
      {
        id: "n_out",
        type: "output",
        kind: "boundary",
        label: null,
        config: {},
        ports: { in: [{ id: "in", type: "any", required: true }], out: [] },
      },
    ],
    edges: [
      {
        id: "e1",
        source: "n_in",
        sourceHandle: "out",
        target: "n_llm",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
      {
        id: "e2",
        source: "n_llm",
        sourceHandle: "out",
        target: "n_out",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
    ],
    view: {
      nodes: {
        n_in: { position: { x: 40, y: 80 } },
        n_llm: { position: { x: 300, y: 80 } },
        n_out: { position: { x: 560, y: 80 } },
      },
    },
  };
}

/** The same graph with NO `view` block (an imported / LLM-generated agent). */
export function sampleGraphNoView(): IRDocument {
  const { view: _view, ...rest } = sampleGraph();
  return rest;
}
