// Client-side IR authoring hints. These are *hints* that give the composer a red-squiggle as you
// type — the control-plane's `parse_document` + `validate_graph` stay authoritative (a real run
// returns 400 invalid_ir with the precise message). We keep this a pure, synchronous function so
// it is trivially unit-testable and reusable by the CodeMirror linter.
//
// The top-level required envelope keys are read from the GENERATED IRDocument JSON Schema
// (@theygent/ir-types — the single source of truth, covered by the CI drift guard), so this list
// can't silently drift from the backend. Node kinds likewise come from the generated registry
// (via expectedKind, which also handles the guardrail's per-instance kind).

import { NODE_TYPES } from "@theygent/ir-types";
import irSchema from "@theygent/ir-types/schema";
import { DURABLE_ONLY } from "./durable";
import { expectedKind } from "./kind";

export interface IRIssue {
  message: string;
  severity: "error" | "warning";
}

// Node types the IR schema DECLARES but no runtime executes yet — legal to author, refused at run
// (the control plane answers 400 node_type_unavailable). Their kinds are pinned by the backend's
// NODE_TYPE_KIND; they are absent from the generated registry (which covers executable types).
const DECLARED_ONLY_KIND: Record<string, string> = {
  agent: "activity",
  rag: "activity",
  retriever: "activity",
  memory: "activity",
  code: "activity",
  condition: "orchestration",
  iterator: "orchestration",
};

const ENVELOPE_REQUIRED: string[] = Array.isArray((irSchema as { required?: unknown }).required)
  ? ((irSchema as { required: string[] }).required as string[])
  : ["schemaVersion", "id", "name", "version"];

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
        if (type) {
          // Executable types come from the generated registry (expectedKind also derives the
          // guardrail's per-instance kind); schema-declared-but-unexecutable types have a pinned
          // kind and a "refused at run" warning; anything else is unknown.
          const expected = expectedKind({ type, config: node.config }) ?? DECLARED_ONLY_KIND[type];
          if (kind && expected && expected !== kind) {
            issues.push({
              message: `node "${label}": type "${type}" must have kind "${expected}", not "${kind}"`,
              severity: "error",
            });
          }
          if (!(type in NODE_TYPES) && type in DECLARED_ONLY_KIND) {
            issues.push({
              message: `node "${label}": type "${type}" is declared but not executable yet — the control plane refuses to run it`,
              severity: "warning",
            });
          } else if (!(type in NODE_TYPES)) {
            issues.push({
              message: `node "${label}": unknown type "${type}"`,
              severity: "warning",
            });
          }
        }
      }
    }
  }

  // Durable-only node types can't run on the interactive /graphs/runs path this composer feeds —
  // say so up front (save the graph as an agent and use "Run durably" instead of hitting a 400).
  if (Array.isArray(nodes)) {
    const durable = [
      ...new Set(
        nodes
          .map((n) => (typeof n === "object" && n !== null ? (n as { type?: unknown }).type : null))
          .filter((t): t is string => typeof t === "string" && DURABLE_ONLY.has(t)),
      ),
    ];
    if (durable.length > 0) {
      issues.push({
        message: `durable-only node type(s) ${durable.join(", ")}: this graph can't run interactively — save it as an agent and use "Run durably" (needs THEYGENT_DURABLE=1)`,
        severity: "warning",
      });
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
