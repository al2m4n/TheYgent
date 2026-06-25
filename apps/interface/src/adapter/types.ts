// The adapter's React-Flow-facing types (M15 §0/§8.6). This file and `index.ts` are the ONLY
// modules that know React Flow's node/edge shape — nothing here escapes into app state, API
// calls, or persistence (the one rule). Crucially, a React-Flow node's `data` carries ONLY what
// the canvas renders — label, the node `type`, and its ports — and NEVER `kind`, `models`, or
// `contentHash` (M15 §1 / Do-NOT). `kind` is looked up from the registry on save; `models`/
// `tools`/`contentHash` are graph-level/server-owned and live on the IR document, not on a node.

import type { XYPosition } from "@xyflow/react";

/** What a canvas node renders. Deliberately minimal — see the file header. */
export interface TheygentNodeData extends Record<string, unknown> {
  /** Human label shown on the node (falls back to the node id when the IR has none). */
  label: string;
  /** The IR node `type` (`llm` | `tool` | `router` | …) — selects styling + handle layout. */
  nodeType: string;
  /** Declared in/out ports — the canvas renders one handle per port id. */
  ports: { in: PortView[]; out: PortView[] };
  /** Optional user-chosen icon override (an emoji). PRESENT ONLY when the user picked one — the
   * type's default icon is derived in NodeView, not carried here. This is the single allowed
   * display-only addition to node data: it is sourced from and round-tripped to the `view` block
   * (never hashed, §1.4), so it never affects logic or version identity — unlike `kind`/`models`/
   * `contentHash`, which must never appear on a node (the one rule). */
  icon?: string;
}

export interface PortView {
  id: string;
  /** Advisory port type (§8.3 — `any`/`error`/…); rendered/round-tripped, not yet enforced. */
  type: string;
  required: boolean;
  /** The channel the handle carries (M19 §2.10): `data` (default — threads a value) or `control`
   * (pure ordering). The canvas renders the two distinctly and only allows same-role connections;
   * the edge `channel` is DERIVED from the handles it joins. Round-tripped to the IR `Port.role`. */
  role: "data" | "control";
}

/** What a canvas edge carries beyond its endpoints (the IR `channel` + router `condition`). */
export interface TheygentEdgeData extends Record<string, unknown> {
  channel: "data" | "control";
  condition: string | null;
}

/** The current canvas selection — a node or an edge, or nothing. Drives the inspector panel. */
export type Selection = { kind: "node" | "edge"; id: string } | null;

// ── the `view` block (§8.6) — layout, NEVER hashed ───────────────────────────
// Positions/zoom/collapsed live ONLY here. A pure layout change leaves the view-stripped IR (and
// thus the server-computed contentHash) untouched (decision §1.4). The shape is the adapter's own
// convention; the server treats `view` as opaque and strips it before hashing.

export interface NodeView {
  position: XYPosition;
  /** Optional user-chosen icon (emoji) for this node — layout/display only, never hashed. */
  icon?: string;
}

export interface ViewBlock {
  nodes?: Record<string, NodeView>;
  viewport?: { x: number; y: number; zoom: number };
  [k: string]: unknown;
}
