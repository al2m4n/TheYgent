// Auto-layout for a view-less IR (M15 §2.1 / §8.6). An imported or LLM-generated graph is a
// valid, runnable IR with no `view` block — it must still render. We lay it out by longest-path
// layering: each node's column is one past its deepest predecessor, rows stack within a column.
// This is layout ONLY — it produces a `view`, never touches hashed content (decision §1.4).

import type { Edge as IREdge, Node as IRNode } from "@theygent/ir-types";
import type { XYPosition } from "@xyflow/react";

const COL_GAP = 260;
const ROW_GAP = 110;
export const ORIGIN: XYPosition = { x: 60, y: 60 };

/** Deterministic positions for every node id, keyed by id. Stable for a given graph (so two
 * loads of the same view-less IR land identically — the round-trip test relies on it). */
export function autoLayout(nodes: IRNode[], edges: IREdge[]): Record<string, XYPosition> {
  const ids = nodes.map((n) => n.id);
  const idSet = new Set(ids);
  const preds = new Map<string, string[]>();
  for (const id of ids) preds.set(id, []);
  for (const e of edges) {
    if (idSet.has(e.source) && idSet.has(e.target)) {
      preds.get(e.target)?.push(e.source);
    }
  }

  // Longest-path column for each node, memoised; cycles (which the backend rejects anyway) are
  // broken by a visited-guard so layout never infinite-loops on a malformed in-progress graph.
  const column = new Map<string, number>();
  const visiting = new Set<string>();
  const columnOf = (id: string): number => {
    const cached = column.get(id);
    if (cached !== undefined) return cached;
    if (visiting.has(id)) return 0;
    visiting.add(id);
    const ps = preds.get(id) ?? [];
    const col = ps.length === 0 ? 0 : Math.max(...ps.map((p) => columnOf(p) + 1));
    visiting.delete(id);
    column.set(id, col);
    return col;
  };

  // Group by column in stable node order so rows are deterministic.
  const rows = new Map<number, number>();
  const out: Record<string, XYPosition> = {};
  for (const n of nodes) {
    const col = columnOf(n.id);
    const row = rows.get(col) ?? 0;
    rows.set(col, row + 1);
    out[n.id] = { x: ORIGIN.x + col * COL_GAP, y: ORIGIN.y + row * ROW_GAP };
  }
  return out;
}
