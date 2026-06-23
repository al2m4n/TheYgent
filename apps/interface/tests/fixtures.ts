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

/**
 * The NASTY fixture — the one that makes "lossless" earn the word (§8.3/§8.6). It deliberately
 * exercises every adapter surface that could silently drop data on the IR→RF→IR round-trip:
 *
 *  • a `channel: "control"` edge (sequencing, no value) alongside data edges;
 *  • a `router` node whose out-edges carry `condition` strings;
 *  • a `tool` node with the two-out-port ok/`err` shape (incl. an `error`-typed port);
 *  • a multi-in-port `llm` node (`file` + `question`) — the M10 named-port composition;
 *  • a `view` block carrying NON-position data (`collapsed: true`, a `viewport`) that must be
 *    stripped from hashed content, never leak into it.
 *
 * If the round-trip is identity-on-view-stripped HERE, it is genuinely lossless — not just for the
 * boring input→llm→output single-data-edge case. Default-filled (like a stored version) so the
 * comparison is field-for-field.
 */
export function nastyGraph(): IRDocument {
  const inPort = (id: string, required = true) => ({ id, type: "any", required });
  const outPort = (id: string, type = "any") => ({ id, type, required: true });
  return {
    schemaVersion: "1.0",
    id: "agent.nasty",
    name: "Nasty agent",
    version: "0.3.0",
    contentHash: "sha256:nasty-placeholder-server-owns-this",
    metadata: { note: "exercises the lossy surfaces" },
    models: {
      fast: { binding: "mlx", model: "local-fast", source: "hf", params: { temperature: 0.2 } },
    },
    tools: { echo: { kind: "builtin", ref: "echo" } },
    nodes: [
      {
        id: "n_in",
        type: "input",
        kind: "boundary",
        label: null,
        config: {},
        ports: { in: [], out: [outPort("out")] },
      },
      {
        id: "n_route",
        type: "router",
        kind: "orchestration",
        label: "route",
        config: { select: "$in.in.branch" },
        ports: { in: [inPort("in")], out: [outPort("yes"), outPort("no")] },
      },
      {
        id: "n_tool",
        type: "tool",
        kind: "activity",
        label: "echo",
        config: { tool: "echo", args: {} },
        ports: { in: [inPort("in")], out: [outPort("out"), outPort("err", "error")] },
      },
      {
        id: "n_llm",
        type: "llm",
        kind: "activity",
        label: "compose",
        config: { model: "fast", messages: [{ role: "user", content: "$in.file / $in.question" }] },
        ports: { in: [inPort("file"), inPort("question")], out: [outPort("out")] },
      },
      {
        id: "n_out",
        type: "output",
        kind: "boundary",
        label: null,
        config: {},
        ports: { in: [inPort("in")], out: [] },
      },
    ],
    edges: [
      {
        id: "e1",
        source: "n_in",
        sourceHandle: "out",
        target: "n_route",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
      // router-driven out-edges with conditions:
      {
        id: "e2",
        source: "n_route",
        sourceHandle: "yes",
        target: "n_tool",
        targetHandle: "in",
        channel: "data",
        condition: "$in.in.ok",
      },
      {
        id: "e4",
        source: "n_route",
        sourceHandle: "no",
        target: "n_llm",
        targetHandle: "question",
        channel: "data",
        condition: "fallback",
      },
      // ok branch feeds the multi-in-port llm:
      {
        id: "e3",
        source: "n_tool",
        sourceHandle: "out",
        target: "n_llm",
        targetHandle: "file",
        channel: "data",
        condition: null,
      },
      {
        id: "e5",
        source: "n_llm",
        sourceHandle: "out",
        target: "n_out",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
      // a CONTROL edge (pure sequencing) into the same in-port as a data edge — allowed:
      {
        id: "e6",
        source: "n_tool",
        sourceHandle: "err",
        target: "n_out",
        targetHandle: "in",
        channel: "control",
        condition: null,
      },
    ],
    view: {
      viewport: { x: 10, y: 20, zoom: 0.8 },
      nodes: {
        n_in: { position: { x: 0, y: 0 } },
        // a collapsed node — NON-position view data that must never reach hashed content:
        n_route: { position: { x: 240, y: 0 }, collapsed: true },
        n_tool: { position: { x: 480, y: -80 } },
        n_llm: { position: { x: 720, y: 40 } },
        n_out: { position: { x: 960, y: 40 } },
      },
    },
  };
}
