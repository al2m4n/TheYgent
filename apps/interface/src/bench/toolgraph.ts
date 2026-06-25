// The tool / MCP tester's THROWAWAY graph (M18 §2.6) — test a single tool the way you'd test a model.
//
// A tool is not a data-plane model, so it does NOT run through the data plane: it runs through the
// agent/run path (§1.5). The implementation is a one-node graph `input → mcp_tool → output` composed
// inline and run via the existing `/graphs/runs` (`api.runGraph`). This adds **no new execution path
// and no new backend** — it is the agent bench pointed at a single tool (§2.6 the one rule; do NOT
// add an `/admin/mcp/.../invoke` endpoint). The graph is ephemeral: it is never saved to the registry.
//
// The tool's args are templated from the run input with the M10 named-port grammar the walker already
// speaks: `$in.in.<name>` selects field `<name>` of the value on the `mcp_tool` node's default `in`
// port (the input boundary feeds the whole input object there). So an input `{ image: "data:…" }`
// with `argNames: ["image"]` resolves the tool's `image` arg to that data URL at run time.

import type { IRDocument } from "@theygent/ir-types";

export interface ToolGraphSpec {
  /** the logical name of a registered MCP server (resolved by the control-plane MCP manager). */
  server: string;
  /** a tool that server exposes. */
  tool: string;
  /** the input field names forwarded as the tool's args, each templated as `$in.in.<name>`. */
  argNames: string[];
}

/**
 * Compose the ephemeral `input → mcp_tool → output` IR for a single tool. Pure — the caller runs the
 * result via `api.runGraph` (the existing inline-IR path). Every required in-port is fed by a `data`
 * edge so the graph passes `validate_graph`; the `mcp_tool`'s success value binds its non-error `out`
 * handle (walker `_success_handles`), which the `output` boundary consumes as the run output.
 */
export function buildToolGraph({ server, tool, argNames }: ToolGraphSpec): IRDocument {
  // Each declared arg pulls one field off the input object via the §8.5 port-addressed token.
  const args: Record<string, string> = {};
  for (const name of argNames) args[name] = `$in.in.${name}`;

  return {
    schemaVersion: "1.0",
    id: "bench.tool-tester",
    name: "Tool tester (throwaway)",
    version: "0.0.0",
    nodes: [
      {
        id: "n_in",
        type: "input",
        kind: "boundary",
        config: {},
        ports: { in: [], out: [{ id: "out", type: "any", required: true }] },
      },
      {
        id: "n_tool",
        type: "mcp_tool",
        kind: "activity",
        config: { server, tool, args },
        ports: {
          in: [{ id: "in", type: "any", required: true }],
          out: [{ id: "out", type: "any", required: true }],
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
        id: "e_in_tool",
        source: "n_in",
        sourceHandle: "out",
        target: "n_tool",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
      {
        id: "e_tool_out",
        source: "n_tool",
        sourceHandle: "out",
        target: "n_out",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
    ],
  } as IRDocument;
}
