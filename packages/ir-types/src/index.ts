// @theygent/ir-types — the generated IR contract the frontends consume.
//
// The ONE rule this package enforces: the IR types are GENERATED from `packages/ir` (the
// Pydantic source of truth), never hand-written in TypeScript. `ir.d.ts` is produced by
// `json-schema-to-typescript` from `ir.schema.json` (itself dumped from `IRDocument`); the
// node-type registry (`node-types.json`) is derived from `NODE_TYPE_KIND` + the per-type config
// models. Regenerate both with `pnpm --filter @theygent/ir-types generate`. A drift between this
// package and Python is caught by the CI type-drift guard (re-generate + `git diff --exit-code`).

import nodeTypesJson from "./node-types.json";

// The generated IR document shapes: IRDocument, Node, Edge, Port, Ports, ModelBinding, …
// Re-exported as types only — `ir.d.ts` is pure declarations, zero runtime.
export type * from "./ir";

import type { Kind, Ports } from "./ir";

// ── the node-type registry ────────────────────────────────────────────────────
// Each executable node `type` → its determinism `kind` (so a React-Flow node never carries
// `kind`; the canvas looks it up here), its per-type `config` JSON Schema, a default `config`
// filled from that schema, and the default `ports` a freshly-dropped node declares. The palette
// derives its list from this object — a new type added in Python appears for free.

export interface NodeTypeSpec {
  /** The node `type` selector (`llm`, `tool`, `router`, …). */
  type: string;
  /** The determinism class the compiler lowers on — from Python's NODE_TYPE_KIND. */
  kind: Kind;
  /** JSON Schema of this type's `config` (drives the inspector form / validation hints). */
  configSchema: Record<string, unknown>;
  /** A shaped default `config` for a freshly-dropped node (fields present, values empty). */
  defaultConfig: Record<string, unknown>;
  /** The default in/out ports a freshly-dropped node of this type declares. */
  ports: Ports;
}

export interface NodeTypeRegistry {
  types: Record<string, NodeTypeSpec>;
}

const registry = nodeTypesJson as NodeTypeRegistry;

/** The full node-type registry keyed by `type`. */
export const NODE_TYPES: Record<string, NodeTypeSpec> = registry.types;

/** The sorted list of palette-able node types (derived, never hardcoded). */
export const NODE_TYPE_LIST: NodeTypeSpec[] = Object.keys(NODE_TYPES)
  .sort()
  .map((t) => NODE_TYPES[t]);

/** Look up the determinism `kind` for a node `type` (the lookup that keeps `kind` off RF nodes). */
export function kindForType(type: string): Kind | undefined {
  return NODE_TYPES[type]?.kind;
}
