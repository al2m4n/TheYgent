// Client-side graph validation — a FAST, in-editor mirror of the backend's authoritative
// `validate_graph` (packages/ir/graph.py). It surfaces the same structural problems inline so you
// see them before Save instead of as a 400. The backend stays authoritative: this never blocks a
// save (a real run re-validates and returns the precise error); it is a hint surface, the M15
// analogue of the cockpit's `ir-validate.ts`. Checks mirror graph.py's order where it makes sense.

import { type IRDocument, NODE_TYPES, kindForType } from "@theygent/ir-types";
import Ajv, { type ValidateFunction } from "ajv";

export interface ValidationIssue {
  severity: "error" | "warning";
  message: string;
  nodeId?: string;
  edgeId?: string;
}

// Validate a node's `config` against its PER-TYPE JSON Schema (§2.3) — not just JSON parse-ability.
// "Guarded" must mean schema-valid, or the editor would let you save config that parses but the
// backend's Pydantic rejects. We compile each type's schema once (Ajv, lenient on meta-schema
// strictness since these are Pydantic-emitted draft schemas) and cache it.
const ajv = new Ajv({ strict: false, allErrors: true, allowUnionTypes: true });
const validatorCache = new Map<string, ValidateFunction | null>();

function configValidator(type: string): ValidateFunction | null {
  if (validatorCache.has(type)) return validatorCache.get(type) ?? null;
  const schema = NODE_TYPES[type]?.configSchema;
  let fn: ValidateFunction | null = null;
  if (schema && Object.keys(schema).length > 0) {
    try {
      fn = ajv.compile(schema as object);
    } catch {
      fn = null; // a schema Ajv can't compile is a non-blocking miss, never a crash.
    }
  }
  validatorCache.set(type, fn);
  return fn;
}

// Single-value consumers take exactly ONE in-port (m10.md — `output`/`router`). Multi-input is for
// value-producers; a >1-in-port consumer is an ambiguous forward.
const SINGLE_IN_CONSUMERS = new Set(["output", "router"]);
// subgraph/loop/map compose a SAVED, PINNED agent body — exactly one of version/contentHash.
const PINNED_BODY_TYPES = new Set(["subgraph", "loop", "map"]);

export function validateGraph(ir: IRDocument): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  const nodes = ir.nodes ?? [];
  const edges = ir.edges ?? [];
  const models = ir.models ?? {};

  // duplicate node id
  const seen = new Set<string>();
  for (const n of nodes) {
    if (seen.has(n.id))
      issues.push({ severity: "error", message: "duplicate node id", nodeId: n.id });
    seen.add(n.id);
  }
  const nodeById = new Map(nodes.map((n) => [n.id, n]));

  if (nodes.length === 0) {
    issues.push({ severity: "warning", message: "graph has no nodes" });
  }

  // 1–3: per-node — known type, correct kind, per-type config sanity.
  for (const n of nodes) {
    const spec = NODE_TYPES[n.type];
    const expectedKind = kindForType(n.type);
    if (!expectedKind) {
      issues.push({ severity: "error", message: `unknown node type '${n.type}'`, nodeId: n.id });
      continue;
    }
    if (n.kind !== expectedKind) {
      issues.push({
        severity: "error",
        message: `type '${n.type}' must have kind '${expectedKind}', got '${n.kind}'`,
        nodeId: n.id,
      });
    }
    // required config keys present + non-empty (the common "didn't fill it in" case).
    const required = (spec?.configSchema?.required as string[] | undefined) ?? [];
    const config = (n.config ?? {}) as Record<string, unknown>;
    for (const key of required) {
      const v = config[key];
      const empty =
        v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);
      if (empty) {
        issues.push({ severity: "error", message: `config '${key}' is required`, nodeId: n.id });
      }
    }
    // config must validate against the per-type JSON Schema (§2.3) — catches schema-invalid-but-
    // parseable config (e.g. a number where a string is required) the inspector's JSON editor alone
    // would let through. This is what makes the editor block such a save instead of 400-ing at run.
    const validate = configValidator(n.type);
    if (validate && !validate(config)) {
      for (const err of validate.errors ?? []) {
        const where = err.instancePath ? `config${err.instancePath}` : "config";
        issues.push({
          severity: "error",
          message: `${where} ${err.message ?? "is invalid"}`,
          nodeId: n.id,
        });
      }
    }
    // llm references a declared model binding (§8.4).
    if (n.type === "llm") {
      const m = config.model;
      if (typeof m === "string" && m !== "" && !(m in models)) {
        issues.push({
          severity: "error",
          message: `references undeclared model '${m}' (declare it in Model bindings)`,
          nodeId: n.id,
        });
      }
    }
    // subgraph/loop/map pin EXACTLY ONE of version / contentHash (m14.md §1.2).
    if (PINNED_BODY_TYPES.has(n.type)) {
      const hasVersion = Boolean(config.version);
      const hasHash = Boolean(config.contentHash);
      if (hasVersion === hasHash) {
        issues.push({
          severity: "error",
          message: `a ${n.type} body must pin EXACTLY ONE of 'version' or 'contentHash'`,
          nodeId: n.id,
        });
      }
    }
  }

  // 4: edges reference existing nodes + declared handles.
  for (const e of edges) {
    const src = nodeById.get(e.source);
    const tgt = nodeById.get(e.target);
    if (!src) {
      issues.push({
        severity: "error",
        message: `edge has unknown source '${e.source}'`,
        edgeId: e.id,
      });
    } else if (!(src.ports?.out ?? []).some((p) => p.id === e.sourceHandle)) {
      issues.push({
        severity: "error",
        message: `'${e.source}' has no out-port '${e.sourceHandle}'`,
        edgeId: e.id,
      });
    }
    if (!tgt) {
      issues.push({
        severity: "error",
        message: `edge has unknown target '${e.target}'`,
        edgeId: e.id,
      });
    } else if (!(tgt.ports?.in ?? []).some((p) => p.id === e.targetHandle)) {
      issues.push({
        severity: "error",
        message: `'${e.target}' has no in-port '${e.targetHandle}'`,
        edgeId: e.id,
      });
    }
  }

  // 4b: at most one DATA edge per in-port (ambiguous multi-input).
  const fed = new Set<string>();
  for (const e of edges) {
    if ((e.channel ?? "data") !== "data") continue;
    const key = `${e.target} ${e.targetHandle}`;
    if (fed.has(key)) {
      issues.push({
        severity: "error",
        message: `in-port '${e.targetHandle}' on '${e.target}' is fed by >1 data edge (ambiguous)`,
        edgeId: e.id,
      });
    }
    fed.add(key);
  }

  // M22: a CAPABILITY tool node (the source of a `tool` edge) supplies its args from the model, not
  // an upstream edge — so it is exempt from the required-in-port rule (mirrors graph.py §5).
  const capabilityNodes = new Set(
    edges.filter((e) => (e.channel ?? "data") === "tool").map((e) => e.source),
  );

  // 5: every REQUIRED in-port is fed by a data edge (input boundaries + capability tools excepted).
  for (const n of nodes) {
    if (n.type === "input" || capabilityNodes.has(n.id)) continue;
    for (const p of n.ports?.in ?? []) {
      if ((p.required ?? true) && !fed.has(`${n.id} ${p.id}`)) {
        issues.push({
          severity: "error",
          message: `required in-port '${p.id}' is not connected`,
          nodeId: n.id,
        });
      }
    }
    // single-value consumers take exactly ONE in-port.
    if (SINGLE_IN_CONSUMERS.has(n.type) && (n.ports?.in?.length ?? 0) > 1) {
      issues.push({
        severity: "error",
        message: `'${n.type}' consumes one value but declares ${n.ports?.in?.length} in-ports`,
        nodeId: n.id,
      });
    }
  }

  // M22: a tool/mcp_tool node is a CAPABILITY (wired to an llm via `tool` edges) OR a STEP
  // (data/control edges) — not both (mirrors graph.py §4c, so the editor flags it before a 400).
  for (const n of nodes) {
    if (n.type !== "tool" && n.type !== "mcp_tool") continue;
    const chans = new Set(edges.filter((e) => e.source === n.id).map((e) => e.channel ?? "data"));
    if (chans.has("tool") && [...chans].some((c) => c !== "tool")) {
      issues.push({
        severity: "error",
        message: "a tool node is either a capability (wired to an llm) or a step, not both",
        nodeId: n.id,
      });
    }
  }

  // 6: cycle detection (Kahn over data + control edges — both impose ordering). M22: `tool` edges
  // are a capability wire (no ordering — the model calls lazily), so EXCLUDE them, matching the
  // backend's topological_order (graph.py).
  if (
    detectCycle(
      nodes,
      edges.filter((e) => (e.channel ?? "data") !== "tool"),
    )
  ) {
    issues.push({ severity: "error", message: "graph has a cycle (not allowed)" });
  }

  return issues;
}

function detectCycle(
  nodes: { id: string }[],
  edges: { source: string; target: string }[],
): boolean {
  const ids = new Set(nodes.map((n) => n.id));
  const indegree = new Map<string, number>();
  const succ = new Map<string, string[]>();
  for (const n of nodes) {
    indegree.set(n.id, 0);
    succ.set(n.id, []);
  }
  for (const e of edges) {
    if (!ids.has(e.source) || !ids.has(e.target)) continue;
    succ.get(e.source)?.push(e.target);
    indegree.set(e.target, (indegree.get(e.target) ?? 0) + 1);
  }
  const ready = [...indegree.entries()].filter(([, d]) => d === 0).map(([id]) => id);
  let visited = 0;
  while (ready.length > 0) {
    const id = ready.pop() as string;
    visited += 1;
    for (const nxt of succ.get(id) ?? []) {
      const d = (indegree.get(nxt) ?? 0) - 1;
      indegree.set(nxt, d);
      if (d === 0) ready.push(nxt);
    }
  }
  return visited !== nodes.length;
}
