// Which node types run ONLY on the durable runtime (a durable wait, a child workflow, a bounded
// loop, a fan-out) — the interactive Run path rejects them, so an agent containing one can only run
// via a trigger or the durable-run endpoint.
//
// This set MIRRORS the backend `_DURABLE_ONLY_TYPES` (apps/control-plane app.py) — keep the two in
// sync. It can't be derived from the generated registry: durability doesn't map to a node's `kind`
// (subgraph/human are boundary, loop/map are orchestration), so there's no machine-readable flag to
// key on; a small hand-kept mirror is the honest option.

import type { IRDocument } from "@theygent/ir-types";

export const DURABLE_ONLY = new Set(["human", "subgraph", "loop", "map"]);

/** True when the agent contains any durable-only node — i.e. it can't run on the interactive path. */
export function isDurableOnly(ir: IRDocument): boolean {
  return (ir.nodes ?? []).some((n) => DURABLE_ONLY.has(n.type));
}
