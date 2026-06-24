// The IR ⇄ React Flow view adapter (M15 §2.1 / §8.6) — the load-bearing module the whole
// milestone hinges on. THIS FILE AND `types.ts` ARE THE ONLY MODULES THAT KNOW REACT FLOW'S
// NODE/EDGE SHAPE. App state, API calls, and persistence speak the generated IR types
// (@theygent/ir-types) — never React Flow's (the one rule, §0).
//
// Two directions:
//   • Load  (irToReactFlow):  IR → React Flow nodes/edges for the canvas. Positions come from the
//     `view` block, or AUTO-LAYOUT when `view` is absent (an imported/generated graph still
//     renders). RF node `data` carries ONLY label/type/ports — never kind/models/contentHash.
//   • Save  (reactFlowToIr):  React Flow → IR, the inverse. Structure (nodes/edges/ports) is
//     reconstructed from canvas state; positions split back into a `view` block; the graph-level
//     envelope (models/tools/id/name/version/metadata) and per-node `config` (edited in the
//     inspector, not rendered on the node) carry over from the IR being edited. Identity on
//     view-stripped content — proven by the round-trip test.
//
// In the app, the IR (including its `view`) is the single source of truth held in route state; the
// canvas is a CONTROLLED React Flow derived from it via `irToReactFlow`, and every interaction is
// one of the IR→IR mutation helpers below. `reactFlowToIr` is the documented inverse (and the
// headline test) — it is never the path by which RF types reach app state.

import {
  type IRDocument,
  type Edge as IREdge,
  type Node as IRNode,
  NODE_TYPES,
  type Kind as NodeKind,
  type Port,
  kindForType,
} from "@theygent/ir-types";
import type { Connection, Edge as RFEdge, Node as RFNode, XYPosition } from "@xyflow/react";
import { autoLayout } from "./layout";
import type { TheygentEdgeData, TheygentNodeData, ViewBlock } from "./types";

export type TheygentRFNode = RFNode<TheygentNodeData, "theygent">;
export type TheygentRFEdge = RFEdge<TheygentEdgeData>;

export interface RFGraph {
  nodes: TheygentRFNode[];
  edges: TheygentRFEdge[];
}

export type { Selection, TheygentEdgeData, TheygentNodeData, ViewBlock } from "./types";

// ── Load: IR → React Flow ─────────────────────────────────────────────────────

function resolvePositions(
  nodes: IRNode[],
  edges: IREdge[],
  view: ViewBlock,
): Record<string, XYPosition> {
  // Start from a deterministic auto-layout, then override with any explicit `view` positions, so a
  // partially-positioned view (e.g. one new node not yet dragged) still renders sensibly.
  const positions = autoLayout(nodes, edges);
  const vnodes = view.nodes ?? {};
  for (const n of nodes) {
    const p = vnodes[n.id]?.position;
    if (p && typeof p.x === "number" && typeof p.y === "number") {
      positions[n.id] = { x: p.x, y: p.y };
    }
  }
  return positions;
}

export function irToReactFlow(ir: IRDocument): RFGraph {
  const nodes = ir.nodes ?? [];
  const edges = ir.edges ?? [];
  const view = (ir.view ?? {}) as ViewBlock;
  const positions = resolvePositions(nodes, edges, view);

  const vnodes = view.nodes ?? {};
  const rfNodes: TheygentRFNode[] = nodes.map((n) => {
    const data: TheygentNodeData = {
      label: n.label ?? n.id,
      nodeType: n.type,
      ports: {
        in: (n.ports?.in ?? []).map((p) => ({
          id: p.id,
          type: p.type ?? "any",
          required: p.required ?? true,
        })),
        out: (n.ports?.out ?? []).map((p) => ({
          id: p.id,
          type: p.type ?? "any",
          required: p.required ?? true,
        })),
      },
    };
    // The icon override (if any) is a `view`-sourced display field — added ONLY when the user set
    // one, so a node with no override carries exactly label/nodeType/ports (the one rule holds).
    const icon = vnodes[n.id]?.icon;
    if (typeof icon === "string" && icon.length > 0) data.icon = icon;
    return { id: n.id, type: "theygent", position: positions[n.id] ?? { x: 0, y: 0 }, data };
  });

  const rfEdges: TheygentRFEdge[] = edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    sourceHandle: e.sourceHandle,
    targetHandle: e.targetHandle,
    // A control edge is pure sequencing (no value) — animate it so data (solid) vs control reads
    // at a glance.
    animated: (e.channel ?? "data") === "control",
    data: { channel: e.channel ?? "data", condition: e.condition ?? null },
  }));

  return { nodes: rfNodes, edges: rfEdges };
}

// ── Save: React Flow → IR (the inverse — identity on view-stripped content) ────

function portFrom(view: { id: string; type: string; required: boolean }): Port {
  return { id: view.id, type: view.type, required: view.required };
}

export function reactFlowToIr(rf: RFGraph, base: IRDocument): IRDocument {
  const baseNodeById = new Map((base.nodes ?? []).map((n) => [n.id, n]));

  const nodes: IRNode[] = rf.nodes.map((rn) => {
    const b = baseNodeById.get(rn.id);
    const type = rn.data.nodeType;
    // `kind` is NEVER on the RF node (the one rule) — look it up from the registry, falling back
    // to the IR being edited for any type outside the executable palette.
    const kind: NodeKind = kindForType(type) ?? b?.kind ?? "activity";
    const node: IRNode = {
      id: rn.id,
      type,
      kind,
      // `config` is edited in the inspector (an IR mutation), not rendered on the node — so it
      // carries over from the IR being edited, never reconstructed from RF data.
      config: b?.config ?? NODE_TYPES[type]?.defaultConfig ?? {},
      ports: {
        in: rn.data.ports.in.map(portFrom),
        out: rn.data.ports.out.map(portFrom),
      },
    };
    // Preserve the original label exactly — adding an id-fallback label would change hashed
    // content. A label edit goes through `updateNodeLabel` (an IR mutation), so the base is always
    // current; a brand-new node takes the canvas label. `null` (not omitted) mirrors the server's
    // default-filled dump (D2), so a stored→reconstructed round-trip is field-for-field identical.
    node.label = (b ? b.label : rn.data.label) ?? null;
    return node;
  });

  const edges: IREdge[] = rf.edges.map((re) => ({
    id: re.id,
    source: re.source,
    sourceHandle: re.sourceHandle ?? "",
    target: re.target,
    targetHandle: re.targetHandle ?? "",
    channel: re.data?.channel ?? "data",
    condition: re.data?.condition ?? null,
  }));

  // Split positions back into a `view` block — layout, never hashed (§8.6). The icon override (a
  // display-only field) round-trips here too, so it survives save/load without touching content.
  const viewNodes: Record<string, { position: XYPosition; icon?: string }> = {};
  for (const rn of rf.nodes) {
    viewNodes[rn.id] = { position: rn.position };
    if (typeof rn.data.icon === "string" && rn.data.icon.length > 0) {
      viewNodes[rn.id].icon = rn.data.icon;
    }
  }
  const view: ViewBlock = { ...(base.view as ViewBlock | undefined), nodes: viewNodes };

  return { ...base, nodes, edges, view };
}

// ── IR → IR mutation helpers (the canvas interactions, §2.2) ──────────────────
// Each returns a NEW IRDocument; React Flow types never appear in their signatures except
// `Connection`/positions, which the canvas (an adapter consumer) hands in and which never escape.

let _counter = 0;
function freshId(prefix: string, taken: Set<string>): string {
  let id: string;
  do {
    _counter += 1;
    id = `${prefix}_${_counter}`;
  } while (taken.has(id));
  return id;
}

/** Set/replace a node's layout position (drag/pan) — touches `view` ONLY (decision §1.4). Merges
 * into the existing view entry so a sibling display field (e.g. an icon override) survives a drag. */
export function setNodePositions(
  ir: IRDocument,
  positions: Record<string, XYPosition>,
): IRDocument {
  const prev = (ir.view as ViewBlock | undefined) ?? {};
  const nextNodes = { ...(prev.nodes ?? {}) };
  for (const [id, position] of Object.entries(positions)) {
    nextNodes[id] = { ...nextNodes[id], position };
  }
  return { ...ir, view: { ...prev, nodes: nextNodes } };
}

/** Set (or clear, with `null`) a node's icon override — a `view`-only display change, never hashed
 * content (§1.4), exactly like a drag. Clearing reverts the node to its type's default icon. */
export function setNodeIcon(ir: IRDocument, id: string, icon: string | null): IRDocument {
  const prev = (ir.view as ViewBlock | undefined) ?? {};
  const nextNodes = { ...(prev.nodes ?? {}) };
  const position = nextNodes[id]?.position ?? { x: 0, y: 0 };
  // Rebuild the entry so clearing simply omits the key (no `delete`); position is the only other
  // per-node view field, so nothing is lost.
  nextNodes[id] = icon && icon.length > 0 ? { position, icon } : { position };
  return { ...ir, view: { ...prev, nodes: nextNodes } };
}

/** Re-run the layered auto-layout, overwriting every node's `view` position (the "Tidy" button).
 * Touches `view` ONLY — a pure layout change, never hashed content (§1.4). Preserves any per-node
 * display field (e.g. an icon override) — only positions are rewritten. */
export function relayout(ir: IRDocument): IRDocument {
  const positions = autoLayout(ir.nodes ?? [], ir.edges ?? []);
  const prev = (ir.view as ViewBlock | undefined) ?? {};
  const nextNodes = { ...(prev.nodes ?? {}) };
  for (const [id, position] of Object.entries(positions)) {
    nextNodes[id] = { ...nextNodes[id], position };
  }
  return { ...ir, view: { ...prev, nodes: nextNodes } };
}

/** Drop a palette `type` onto the canvas: a new IR node with the registry's kind, default config,
 * and declared ports (§2.2). The type list/kind/ports are all derived from the registry — nothing
 * is hardcoded, so an M14-style type appears for free. */
export function addNode(ir: IRDocument, type: string, position: XYPosition): IRDocument {
  const spec = NODE_TYPES[type];
  if (!spec) throw new Error(`unknown node type ${type}`);
  const taken = new Set((ir.nodes ?? []).map((n) => n.id));
  const id = freshId(`n_${type}`, taken);
  const node: IRNode = {
    id,
    type,
    kind: spec.kind,
    label: type,
    config: structuredClone(spec.defaultConfig),
    ports: structuredClone(spec.ports),
  };
  const withNode = { ...ir, nodes: [...(ir.nodes ?? []), node] };
  return setNodePositions(withNode, { [id]: position });
}

export interface ConnectResult {
  ir?: IRDocument;
  error?: string;
}

/** Create an IR edge from a canvas connection (§2.2). Rejects connections to non-existent handles
 * and a second `data` edge into one in-port (the ambiguous-multi-input rule the backend enforces,
 * mirrored here for an immediate red rather than a 400 at save). `channel` defaults to `data`. */
export function connect(ir: IRDocument, c: Connection): ConnectResult {
  const src = (ir.nodes ?? []).find((n) => n.id === c.source);
  const tgt = (ir.nodes ?? []).find((n) => n.id === c.target);
  if (!src || !tgt) return { error: "edge references a node that no longer exists" };
  if (!c.sourceHandle || !(src.ports?.out ?? []).some((p) => p.id === c.sourceHandle)) {
    return { error: `node ${src.id} has no out-port ${c.sourceHandle ?? "(none)"}` };
  }
  if (!c.targetHandle || !(tgt.ports?.in ?? []).some((p) => p.id === c.targetHandle)) {
    return { error: `node ${tgt.id} has no in-port ${c.targetHandle ?? "(none)"}` };
  }
  const dup = (ir.edges ?? []).some(
    (e) =>
      (e.channel ?? "data") === "data" &&
      e.target === c.target &&
      e.targetHandle === c.targetHandle,
  );
  if (dup) {
    return {
      error: `in-port ${c.targetHandle} on ${tgt.id} is already fed by a data edge (ambiguous multi-input)`,
    };
  }
  const taken = new Set((ir.edges ?? []).map((e) => e.id));
  const edge: IREdge = {
    id: freshId("e", taken),
    source: c.source,
    sourceHandle: c.sourceHandle,
    target: c.target,
    targetHandle: c.targetHandle,
    channel: "data",
  };
  return { ir: { ...ir, edges: [...(ir.edges ?? []), edge] } };
}

/** Delete nodes and every incident edge + their `view` entries. */
export function deleteNodes(ir: IRDocument, ids: string[]): IRDocument {
  const gone = new Set(ids);
  const nodes = (ir.nodes ?? []).filter((n) => !gone.has(n.id));
  const edges = (ir.edges ?? []).filter((e) => !gone.has(e.source) && !gone.has(e.target));
  const view = ir.view as ViewBlock | undefined;
  let nextView = view;
  if (view?.nodes) {
    const vnodes = { ...view.nodes };
    for (const id of ids) delete vnodes[id];
    nextView = { ...view, nodes: vnodes };
  }
  return { ...ir, nodes, edges, view: nextView };
}

export function deleteEdges(ir: IRDocument, ids: string[]): IRDocument {
  const gone = new Set(ids);
  return { ...ir, edges: (ir.edges ?? []).filter((e) => !gone.has(e.id)) };
}

/** Replace a node's `config` (the inspector). */
export function updateNodeConfig(
  ir: IRDocument,
  id: string,
  config: Record<string, unknown>,
): IRDocument {
  return {
    ...ir,
    nodes: (ir.nodes ?? []).map((n) => (n.id === id ? { ...n, config } : n)),
  };
}

/** Rename a node (its `label`). */
export function updateNodeLabel(ir: IRDocument, id: string, label: string): IRDocument {
  return {
    ...ir,
    nodes: (ir.nodes ?? []).map((n) => (n.id === id ? { ...n, label } : n)),
  };
}

/** Patch an edge's `channel` (data ↔ control) and/or `condition` (router-driven edges). */
export function updateEdge(
  ir: IRDocument,
  id: string,
  patch: { channel?: "data" | "control"; condition?: string | null },
): IRDocument {
  return {
    ...ir,
    edges: (ir.edges ?? []).map((e) => (e.id === id ? { ...e, ...patch } : e)),
  };
}

/** Duplicate a node: a fresh id, the same type/config/ports, label suffixed " copy", offset on the
 * canvas. Edges are NOT copied (a copy starts unconnected) — keeps required-port validation honest.
 * Returns the new IR and the new node id (so the caller can select it). */
export function duplicateNode(ir: IRDocument, id: string): { ir: IRDocument; newId: string } {
  const src = (ir.nodes ?? []).find((n) => n.id === id);
  if (!src) return { ir, newId: id };
  const taken = new Set((ir.nodes ?? []).map((n) => n.id));
  const newId = freshId(`n_${src.type}`, taken);
  const clone: IRNode = {
    ...structuredClone(src),
    id: newId,
    label: src.label ? `${src.label} copy` : newId,
  };
  const withNode = { ...ir, nodes: [...(ir.nodes ?? []), clone] };
  const srcPos = (ir.view as ViewBlock | undefined)?.nodes?.[id]?.position ?? { x: 0, y: 0 };
  return {
    ir: setNodePositions(withNode, { [newId]: { x: srcPos.x + 40, y: srcPos.y + 40 } }),
    newId,
  };
}
