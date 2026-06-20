// Client-side IR authoring hints (M8 §3.1). These are *hints* that give the composer a
// red-squiggle as you type — the control-plane's `parse_document` + `validate_graph` stay
// authoritative (a real run returns 400 invalid_ir with the precise message). We keep this a
// pure, synchronous function so it is trivially unit-testable (§5) and reusable by the
// CodeMirror linter.
//
// The top-level required envelope keys are read from the bundled IRDocument JSON Schema
// (generated from packages/ir — the single source of truth), so this list can't silently
// drift from the backend. The type→kind table mirrors packages/ir's NODE_TYPE_KIND.

import irSchema from "./ir.schema.json";

export interface IRIssue {
  message: string;
  severity: "error" | "warning";
}

// Mirror of packages/ir NODE_TYPE_KIND (theygent-graph-schema.md §8.3). A type/kind mismatch
// is a validation error in the IR package; surfacing it early is the highest-value hint.
const NODE_TYPE_KIND: Record<string, string> = {
  input: "boundary",
  output: "boundary",
  human: "boundary",
  subgraph: "boundary",
  agent: "activity",
  llm: "activity",
  tool: "activity",
  mcp_tool: "activity",
  rag: "activity",
  retriever: "activity",
  memory: "activity",
  code: "activity",
  router: "orchestration",
  condition: "orchestration",
  loop: "orchestration",
  iterator: "orchestration",
  map: "orchestration",
};

const ENVELOPE_REQUIRED: string[] = Array.isArray((irSchema as { required?: unknown }).required)
  ? ((irSchema as { required: string[] }).required as string[])
  : ["id", "version", "nodes", "edges"];

/** Parse + structurally validate an IR document string. `null` doc means a JSON parse error. */
export function validateIR(text: string): { doc: unknown; issues: IRIssue[] } {
  if (text.trim() === "") {
    return { doc: null, issues: [{ message: "IR document is empty", severity: "error" }] };
  }
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch (e) {
    return {
      doc: null,
      issues: [{ message: `invalid JSON: ${(e as Error).message}`, severity: "error" }],
    };
  }

  const issues: IRIssue[] = [];
  if (typeof doc !== "object" || doc === null || Array.isArray(doc)) {
    issues.push({ message: "IR document must be a JSON object", severity: "error" });
    return { doc, issues };
  }
  const obj = doc as Record<string, unknown>;

  for (const key of ENVELOPE_REQUIRED) {
    if (!(key in obj)) {
      issues.push({ message: `missing required field "${key}"`, severity: "error" });
    }
  }

  const nodes = obj.nodes;
  const nodeIds = new Set<string>();
  if (nodes !== undefined) {
    if (!Array.isArray(nodes)) {
      issues.push({ message: '"nodes" must be an array', severity: "error" });
    } else {
      if (nodes.length === 0) {
        issues.push({ message: "graph has no nodes", severity: "warning" });
      }
      for (const [i, raw] of nodes.entries()) {
        if (typeof raw !== "object" || raw === null) {
          issues.push({ message: `node ${i} is not an object`, severity: "error" });
          continue;
        }
        const node = raw as Record<string, unknown>;
        const id = typeof node.id === "string" ? node.id : undefined;
        if (!id) issues.push({ message: `node ${i} is missing "id"`, severity: "error" });
        else nodeIds.add(id);
        const type = node.type as string | undefined;
        const kind = node.kind as string | undefined;
        const label = id ?? `#${i}`;
        if (!type) issues.push({ message: `node "${label}" is missing "type"`, severity: "error" });
        if (!kind) issues.push({ message: `node "${label}" is missing "kind"`, severity: "error" });
        if (type && kind && type in NODE_TYPE_KIND && NODE_TYPE_KIND[type] !== kind) {
          issues.push({
            message: `node "${label}": type "${type}" must have kind "${NODE_TYPE_KIND[type]}", not "${kind}"`,
            severity: "error",
          });
        }
        if (type && !(type in NODE_TYPE_KIND)) {
          issues.push({ message: `node "${label}": unknown type "${type}"`, severity: "warning" });
        }
      }
    }
  }

  const edges = obj.edges;
  if (edges !== undefined) {
    if (!Array.isArray(edges)) {
      issues.push({ message: '"edges" must be an array', severity: "error" });
    } else {
      for (const [i, raw] of edges.entries()) {
        if (typeof raw !== "object" || raw === null) {
          issues.push({ message: `edge ${i} is not an object`, severity: "error" });
          continue;
        }
        const edge = raw as Record<string, unknown>;
        for (const end of ["source", "target"] as const) {
          const ref = edge[end];
          if (typeof ref !== "string") {
            issues.push({ message: `edge ${i} is missing "${end}"`, severity: "error" });
          } else if (nodeIds.size > 0 && !nodeIds.has(ref)) {
            issues.push({
              message: `edge ${i} ${end} "${ref}" references no node`,
              severity: "error",
            });
          }
        }
      }
    }
  }

  return { doc, issues };
}
