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
          role: p.role ?? "data",
        })),
        out: (n.ports?.out ?? []).map((p) => ({
          id: p.id,
          type: p.type ?? "any",
          required: p.required ?? true,
          role: p.role ?? "data",
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

function portFrom(view: {
  id: string;
  type: string;
  required: boolean;
  role: "data" | "control" | "tool";
}): Port {
  const port: Port = { id: view.id, type: view.type, required: view.required };
  // `role` defaults to `data` (the server fills it — D2), so OMIT the default and emit only an
  // explicit `control`/`tool` — a role-less (pre-M19) IR round-trips to itself, byte-for-byte
  // (§2.10 / M22).
  if (view.role === "control" || view.role === "tool") port.role = view.role;
  return port;
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

  // M22: keep `ir.tools` in sync with the wired tool nodes (the global registry — like ir.models).
  return withDerivedTools({ ...base, nodes, edges, view });
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
// No-code convenience: seed a friendlier starting config for a freshly DROPPED node than the IR
// schema's bare default (which is intentionally empty). Builder-only and applied at add time — it
// never changes the IR schema, the generated registry default, or any existing node. The llm's most
// common shape is "send this node's input straight to the model", so seed one user message templated
// to `$in` (the value on the default in-port) — the author edits/extends it from there, and because
// `$in` is a runtime reference it already tracks whatever gets wired in, so no re-seed on rewiring.
function builderDefaultConfig(
  type: string,
  base: Record<string, unknown>,
): Record<string, unknown> {
  const config = structuredClone(base);
  if (type === "llm") {
    if (Array.isArray(config.messages) && config.messages.length === 0) {
      config.messages = [{ role: "user", content: "$in" }];
    }
    // M21 tool-calling fields default to "no tools" — keep a freshly dropped llm node clean (and
    // hash-equivalent to a hand-authored {model, messages}); they're configured via the Tools panel
    // once the tool-calling loop ships (m21.md Phase 6). The RawConfig/Code views still expose them.
    for (const k of ["tools", "toolChoice", "maxToolIterations"]) delete config[k];
  }
  return config;
}

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
    config: builderDefaultConfig(type, spec.defaultConfig),
    ports: structuredClone(spec.ports),
  };
  const withNode = { ...ir, nodes: [...(ir.nodes ?? []), node] };
  return setNodePositions(withNode, { [id]: position });
}

export interface ConnectResult {
  ir?: IRDocument;
  error?: string;
}

/** Create an IR edge from a canvas connection (§2.2 / M19 §2.10). Rejects connections to
 * non-existent handles, a **cross-role** connection (data→control or control→data — the role of the
 * handles must match), and a second `data` edge into one in-port (the ambiguous-multi-input rule the
 * backend enforces, mirrored here for an immediate red rather than a 400 at save). The edge
 * `channel` is **derived** from the connected handles' role (data→data ⇒ `data`, control→control ⇒
 * `control`) — not a separate toggle (M19 §2.10). */
export function connect(ir: IRDocument, c: Connection): ConnectResult {
  const src = (ir.nodes ?? []).find((n) => n.id === c.source);
  const tgt = (ir.nodes ?? []).find((n) => n.id === c.target);
  if (!src || !tgt) return { error: "edge references a node that no longer exists" };
  const outPort = (src.ports?.out ?? []).find((p) => p.id === c.sourceHandle);
  if (!c.sourceHandle || !outPort) {
    return { error: `node ${src.id} has no out-port ${c.sourceHandle ?? "(none)"}` };
  }
  const inPort = (tgt.ports?.in ?? []).find((p) => p.id === c.targetHandle);
  if (!c.targetHandle || !inPort) {
    return { error: `node ${tgt.id} has no in-port ${c.targetHandle ?? "(none)"}` };
  }
  // The edge `channel` is DERIVED from the handles it joins, never a separate toggle. Connect
  // like-to-like: data↔data (M19 §2.10), control↔control, tool↔tool (M22 — a tool/mcp_tool node's
  // `use` OUT-handle into an llm's `tools` port, making the tool model-callable). The roles match, so
  // one rule covers all three channels.
  const srcRole = outPort.role ?? "data";
  const tgtRole = inPort.role ?? "data";
  if (srcRole !== tgtRole) {
    return {
      error: `cannot connect a ${srcRole} handle to a ${tgtRole} handle — connect like-to-like (data↔data, control↔control, tool↔tools)`,
    };
  }
  const channel: "data" | "control" | "tool" = srcRole;
  // The ambiguous-multi-input rule applies to DATA edges only (a control edge imposes no value).
  const dup =
    channel === "data" &&
    (ir.edges ?? []).some(
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
    channel,
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

/** Bind a `tool` (http) node to a connection (M19 §2.10 / §1.1): set the node's `config.tool` to a
 * logical key and declare `ir.tools[key] = { kind:"http", connection }`. The IR references the
 * connection by **id** — never the secret (which is resolved server-side at step time). */
export function setHttpToolBinding(
  ir: IRDocument,
  nodeId: string,
  toolKey: string,
  connectionId: string,
): IRDocument {
  const tools = { ...(ir.tools ?? {}) };
  tools[toolKey] = { kind: "http", connection: connectionId };
  const nodes = (ir.nodes ?? []).map((n) =>
    n.id === nodeId ? { ...n, config: { ...(n.config ?? {}), tool: toolKey } } : n,
  );
  return { ...ir, tools, nodes };
}

/** A tool binding declared once in `ir.tools` (M21) — the model-callable tools any llm node may then
 * select. The three kinds: `builtin` (a registered backend tool), `http` (connection-backed REST),
 * `mcp` (a server + tool). Adding/removing one changes hashed *content* (it's authoring, like a
 * node), never the `view`. */
export type ToolBinding = NonNullable<IRDocument["tools"]>[string];

/** The ir.tools BINDING for a tool/mcp_tool node, derived from its inline config — the mirror of the
 * runtime's `_capability_binding` (walker.py). `mcp_tool` → mcp; a `tool` with a connection/url →
 * http; else a builtin ref. Returns null for any other node type. */
function toolBindingFromNode(n: IRNode): ToolBinding | null {
  const cfg = (n.config ?? {}) as Record<string, unknown>;
  if (n.type === "mcp_tool") {
    const b: Record<string, unknown> = { kind: "mcp", tool: cfg.tool ?? "" };
    if (cfg.server) b.server = cfg.server;
    if (cfg.connection) b.connection = cfg.connection;
    return b as unknown as ToolBinding;
  }
  if (n.type === "tool") {
    if (cfg.connection || cfg.urlTemplate) {
      const b: Record<string, unknown> = { kind: "http", connection: cfg.connection ?? "" };
      for (const k of [
        "description",
        "parameterSchema",
        "method",
        "urlTemplate",
        "bodyTemplate",
        "headers",
        "responseMap",
        "idempotencyKey",
        "timeoutSeconds",
      ]) {
        if (cfg[k] != null) b[k] = cfg[k];
      }
      return b as unknown as ToolBinding;
    }
    return { kind: "builtin", ref: (cfg.tool as string) ?? "" } as unknown as ToolBinding;
  }
  return null;
}

/** Re-derive `ir.tools` as a GLOBAL REGISTRY of the tools wired into the agent (M22, like `ir.models`
 * auto-fills as you add llm bindings). A capability tool node — the source of a `channel:"tool"` edge
 * — contributes `ir.tools[<node id>] = <its binding>` (the binding LIVES on the node; this is the
 * index, tagged with its `kind`). Pre-existing legacy keys that aren't a node id (the M6/M19
 * step-tool refs via `config.tool`) are preserved; stale node-id mirrors are dropped. Idempotent, so
 * a load→save round-trip is identity. */
export function withDerivedTools(ir: IRDocument): IRDocument {
  const nodes = ir.nodes ?? [];
  const edges = ir.edges ?? [];
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const capabilityIds = new Set(
    edges.filter((e) => (e.channel ?? "data") === "tool").map((e) => e.source),
  );
  const tools: Record<string, ToolBinding> = {};
  // keep legacy entries whose key is NOT a current node id (M6/M19 config.tool refs); drop stale.
  for (const [k, v] of Object.entries(ir.tools ?? {})) {
    if (!nodeById.has(k)) tools[k] = v as ToolBinding;
  }
  for (const id of capabilityIds) {
    const n = nodeById.get(id);
    const b = n ? toolBindingFromNode(n) : null;
    if (b) tools[id] = b;
  }
  return { ...ir, tools };
}

/** The three tool kinds, presented as ONE "Tool" node with a kind picker (M22 D1) rather than two
 * palette entries. `builtin`/`rest` are the `tool` node type; `mcp` is the `mcp_tool` type. */
export type ToolKind = "builtin" | "rest" | "mcp";

/** The current kind of a tool/mcp_tool node — DERIVED from config, the same rule as
 * `toolBindingFromNode` and the backend's `_capability_binding`: `mcp_tool` ⇒ `mcp`; a `tool` node
 * carrying a connection or url ⇒ `rest`; otherwise a `builtin` ref. */
export function toolKindOf(n: IRNode): ToolKind {
  if (n.type === "mcp_tool") return "mcp";
  const cfg = (n.config ?? {}) as Record<string, unknown>;
  return cfg.connection || cfg.urlTemplate ? "rest" : "builtin";
}

/** Switch a tool node between its three kinds (M22 D1 — one node, the kind is a picker, not two
 * palette types). `builtin`/`rest` keep the `tool` node type; `mcp` swaps to `mcp_tool`. The swap
 * reseeds `config` from the target type's default shape (carrying `description`/`tool` across) so the
 * kind is re-derivable: `rest` seeds a `urlTemplate` placeholder so an empty REST tool still reads as
 * http (matching the backend's `_capability_binding`). Ports/label/icon/edges are preserved — both
 * types declare the identical `use` capability handle — and `ir.tools` is re-derived so the registry
 * shows the new kind. A no-op if the node already has that kind. */
export function setToolKind(ir: IRDocument, nodeId: string, kind: ToolKind): IRDocument {
  const node = (ir.nodes ?? []).find((n) => n.id === nodeId);
  if (!node || toolKindOf(node) === kind) return ir;
  const cfg = (node.config ?? {}) as Record<string, unknown>;
  const targetType = kind === "mcp" ? "mcp_tool" : "tool";
  const spec = NODE_TYPES[targetType];
  const next: Record<string, unknown> = structuredClone(spec.defaultConfig);
  if (cfg.description) next.description = cfg.description;
  if (cfg.tool) next.tool = cfg.tool;
  if (kind === "rest") {
    if (cfg.connection) next.connection = cfg.connection;
    if (cfg.urlTemplate) next.urlTemplate = cfg.urlTemplate;
    if (!next.connection && !next.urlTemplate) next.urlTemplate = "https://";
    if (cfg.method) next.method = cfg.method;
  } else if (kind === "mcp") {
    if (cfg.server) next.server = cfg.server;
    if (cfg.connection) next.connection = cfg.connection;
  }
  const nodes = (ir.nodes ?? []).map((n) =>
    n.id === nodeId ? { ...n, type: targetType, kind: spec.kind as NodeKind, config: next } : n,
  );
  return withDerivedTools({ ...ir, nodes });
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
