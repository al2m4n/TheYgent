// The IR ⇄ React Flow view adapter — the load-bearing module the whole
// canvas hinges on. THIS FILE AND `types.ts` ARE THE ONLY MODULES THAT KNOW REACT FLOW'S
// NODE/EDGE SHAPE. App state, API calls, and persistence speak the generated IR types
// (@theygent/ir-types) — never React Flow's (the one rule).
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
} from "@theygent/ir-types";
import type { Connection, Edge as RFEdge, Node as RFNode, XYPosition } from "@xyflow/react";
import { expectedKind } from "../lib/kind";
import { ORIGIN, autoLayout } from "./layout";
import type { TheYgentEdgeData, TheYgentNodeData, ViewBlock } from "./types";

export type TheYgentRFNode = RFNode<TheYgentNodeData, "theygent">;
export type TheYgentRFEdge = RFEdge<TheYgentEdgeData>;

export interface RFGraph {
  nodes: TheYgentRFNode[];
  edges: TheYgentRFEdge[];
}

export type { Selection, TheYgentEdgeData, TheYgentNodeData, ViewBlock } from "./types";

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
  const rfNodes: TheYgentRFNode[] = nodes.map((n) => {
    const data: TheYgentNodeData = {
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

  const rfEdges: TheYgentRFEdge[] = edges.map((e) => ({
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
  // `role` defaults to `data` (the server fills it), so OMIT the default and emit only an
  // explicit `control`/`tool` — a role-less IR (one with no explicit role) round-trips to itself,
  // byte-for-byte.
  if (view.role === "control" || view.role === "tool") port.role = view.role;
  return port;
}

export function reactFlowToIr(rf: RFGraph, base: IRDocument): IRDocument {
  const baseNodeById = new Map((base.nodes ?? []).map((n) => [n.id, n]));

  const nodes: IRNode[] = rf.nodes.map((rn) => {
    const b = baseNodeById.get(rn.id);
    const type = rn.data.nodeType;
    // `config` is edited in the inspector (an IR mutation), not rendered on the node — so it carries
    // over from the IR being edited, never reconstructed from RF data.
    const config = b?.config ?? NODE_TYPES[type]?.defaultConfig ?? {};
    // `kind` is NEVER on the RF node (the one rule) — re-derive it. For the guardrail this depends on
    // its config (a model check ⇒ activity, a rule check ⇒ orchestration), so derive from the SAME
    // config the node carries; every other type has a fixed kind. Fall back to the IR being edited
    // for any type outside the executable palette.
    const kind: NodeKind = expectedKind({ type, config }) ?? b?.kind ?? "activity";
    const node: IRNode = {
      id: rn.id,
      type,
      kind,
      config,
      ports: {
        in: rn.data.ports.in.map(portFrom),
        out: rn.data.ports.out.map(portFrom),
      },
    };
    // Preserve the original label exactly — adding an id-fallback label would change hashed
    // content. A label edit goes through `updateNodeLabel` (an IR mutation), so the base is always
    // current; a brand-new node takes the canvas label. `null` (not omitted) mirrors the server's
    // default-filled dump, so a stored→reconstructed round-trip is field-for-field identical.
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

  // Split positions back into a `view` block — layout, never hashed. The icon override (a
  // display-only field) round-trips here too, so it survives save/load without touching content.
  const viewNodes: Record<string, { position: XYPosition; icon?: string }> = {};
  for (const rn of rf.nodes) {
    viewNodes[rn.id] = { position: rn.position };
    if (typeof rn.data.icon === "string" && rn.data.icon.length > 0) {
      viewNodes[rn.id].icon = rn.data.icon;
    }
  }
  const view: ViewBlock = { ...(base.view as ViewBlock | undefined), nodes: viewNodes };

  // Keep `ir.tools` in sync with the wired tool nodes (the global registry — like ir.models).
  return withDerivedTools({ ...base, nodes, edges, view });
}

// ── IR → IR mutation helpers (the canvas interactions) ────────────────────────
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

/** Set/replace a node's layout position (drag/pan) — touches `view` ONLY (positions are never
 * hashed content). Merges into the existing view entry so a sibling display field (e.g. an icon
 * override) survives a drag. */
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
 * content, exactly like a drag. Clearing reverts the node to its type's default icon. */
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
 * Touches `view` ONLY — a pure layout change, never hashed content. Preserves any per-node
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
 * and declared ports. The type list/kind/ports are all derived from the registry — nothing
 * is hardcoded, so a newly added node type appears for free. */
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
    // Tool-calling fields default to "no tools" — keep a freshly dropped llm node clean (and
    // hash-equivalent to a hand-authored {model, messages}); they're configured via the Tools
    // panel. The RawConfig/Code views still expose them.
    for (const k of ["tools", "toolChoice", "maxToolIterations"]) delete config[k];
  }
  return config;
}

/** A sensible position for a node added by CLICKING a palette item, where there is no drop point:
 * a small cascade off the last node's rendered position, so repeated clicks fan out instead of
 * stacking. Positions resolve exactly as the canvas renders them (explicit `view` positions over
 * the auto-layout), and from the IR alone — the caller never needs the canvas viewport. */
export function defaultAddPosition(ir: IRDocument): XYPosition {
  const nodes = ir.nodes ?? [];
  if (nodes.length === 0) return { ...ORIGIN };
  const positions = resolvePositions(nodes, ir.edges ?? [], (ir.view ?? {}) as ViewBlock);
  const last = positions[nodes[nodes.length - 1].id] ?? ORIGIN;
  return { x: last.x + 40, y: last.y + 40 };
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

/** Create an IR edge from a canvas connection. Rejects connections to
 * non-existent handles, a **cross-role** connection (data→control or control→data — the role of the
 * handles must match), and a second `data` edge into one in-port (the ambiguous-multi-input rule the
 * backend enforces, mirrored here for an immediate red rather than a 400 at save). The edge
 * `channel` is **derived** from the connected handles' role (data→data ⇒ `data`, control→control ⇒
 * `control`) — not a separate toggle. */
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
  // like-to-like: data↔data, control↔control, tool↔tool (a tool/mcp_tool node's
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

// ── port (handle) editing — the named in/out ports a node declares ─────────────────────────────────
//
// Ports are hashed IR content (unlike positions/icons), so these are authoring mutations like
// `updateNodeConfig`, not `view` edits. A node's ports are what the runtime addresses: an out-port
// name is a router branch (`select` resolves to one) and each named handle is an edge target; an
// in-port name is a `$in.<port>` key (multi-input). The Wizard exposes these so a router's branches
// and a node's multiple inputs can be built without dropping to the node's Code view. Renaming
// REWIRES the edges on that handle (wiring survives); removing PRUNES the incident edges (no dangling
// reference). New ports are emitted in the exact shape `reactFlowToIr` re-emits (`{id,type,required}`,
// role only when non-`data`) so a port added here round-trips byte-for-byte.

export type PortSide = "in" | "out";

/** A unique port id on `side`, from a base (e.g. "out" → "out2" if taken). Handle names must be
 * unique within a node's side — they're what an edge's source/target handle references. */
function freshPortId(existing: Port[], base: string): string {
  const taken = new Set(existing.map((p) => p.id));
  if (base && !taken.has(base)) return base;
  const root = base || "port";
  let i = 2;
  while (taken.has(`${root}${i}`)) i += 1;
  return `${root}${i}`;
}

function mapPorts(n: IRNode, side: PortSide, f: (list: Port[]) => Port[]): IRNode {
  const inPorts = n.ports?.in ?? [];
  const outPorts = n.ports?.out ?? [];
  return {
    ...n,
    ports: side === "in" ? { in: f(inPorts), out: outPorts } : { in: inPorts, out: f(outPorts) },
  };
}

/** Add a named port to a node's `in`/`out` side. `role` defaults to `data`; `type: "error"` marks an
 * error out-port (the runtime's error semantics key on the port type, not the name). The id defaults
 * to a unique "in"/"out"-derived name the author then renames. */
export function addPort(
  ir: IRDocument,
  nodeId: string,
  side: PortSide,
  opts: { id?: string; type?: string; required?: boolean; role?: "data" | "control" | "tool" } = {},
): IRDocument {
  return {
    ...ir,
    nodes: (ir.nodes ?? []).map((n) => {
      if (n.id !== nodeId) return n;
      const list = (side === "in" ? n.ports?.in : n.ports?.out) ?? [];
      const role = opts.role ?? "data";
      const id = freshPortId(list, opts.id ?? (side === "in" ? "in" : "out"));
      const port: Port = { id, type: opts.type ?? "any", required: opts.required ?? true };
      if (role !== "data") port.role = role;
      return mapPorts(n, side, (l) => [...l, port]);
    }),
  };
}

/** Rename a port and REWIRE every edge on that handle so wiring survives the rename. A no-op if the
 * new id is empty, unchanged, or already used on that side (the caller keeps ids unique). */
export function renamePort(
  ir: IRDocument,
  nodeId: string,
  side: PortSide,
  oldId: string,
  newId: string,
): IRDocument {
  if (!newId || newId === oldId) return ir;
  const node = (ir.nodes ?? []).find((n) => n.id === nodeId);
  const list = (side === "in" ? node?.ports?.in : node?.ports?.out) ?? [];
  if (list.some((p) => p.id === newId)) return ir; // collision — keep ids unique
  const nodes = (ir.nodes ?? []).map((n) =>
    n.id === nodeId
      ? mapPorts(n, side, (l) => l.map((p) => (p.id === oldId ? { ...p, id: newId } : p)))
      : n,
  );
  const edges = (ir.edges ?? []).map((e) => {
    if (side === "out" && e.source === nodeId && e.sourceHandle === oldId) {
      return { ...e, sourceHandle: newId };
    }
    if (side === "in" && e.target === nodeId && e.targetHandle === oldId) {
      return { ...e, targetHandle: newId };
    }
    return e;
  });
  return { ...ir, nodes, edges };
}

/** Remove a port and PRUNE its incident edges (no dangling handle reference). `ir.tools` is
 * re-derived: dropping a tool node's `use` port removes its capability edge and its registry entry. */
export function removePort(
  ir: IRDocument,
  nodeId: string,
  side: PortSide,
  portId: string,
): IRDocument {
  const nodes = (ir.nodes ?? []).map((n) =>
    n.id === nodeId ? mapPorts(n, side, (l) => l.filter((p) => p.id !== portId)) : n,
  );
  const edges = (ir.edges ?? []).filter(
    (e) =>
      !(
        (side === "out" && e.source === nodeId && e.sourceHandle === portId) ||
        (side === "in" && e.target === nodeId && e.targetHandle === portId)
      ),
  );
  return withDerivedTools({ ...ir, nodes, edges });
}

/** Patch a port's flags: `required` (in-ports — an unfed required in-port is a loud validation
 * error) and `type` ("error" marks an error out-port). */
export function updatePort(
  ir: IRDocument,
  nodeId: string,
  side: PortSide,
  portId: string,
  patch: { required?: boolean; type?: string },
): IRDocument {
  return {
    ...ir,
    nodes: (ir.nodes ?? []).map((n) =>
      n.id === nodeId
        ? mapPorts(n, side, (l) => l.map((p) => (p.id === portId ? { ...p, ...patch } : p)))
        : n,
    ),
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

/** Bind a `tool` (http) node to a connection: set the node's `config.tool` to a
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

/** A tool binding declared once in `ir.tools` — the model-callable tools any llm node may then
 * select. The three kinds: `builtin` (a registered backend tool), `http` (connection-backed REST),
 * `mcp` (a server + tool). Adding/removing one changes hashed *content* (it's authoring, like a
 * node), never the `view`. */
export type ToolBinding = NonNullable<IRDocument["tools"]>[string];

/** The ir.tools BINDING for a tool/mcp_tool node, derived from its inline config — the mirror of the
 * runtime's `_capability_binding` (walker.py). `mcp_tool` → mcp; a `tool` with a connection/url →
 * http; else a builtin ref. Returns null for any other node type. */
function toolBindingFromNode(n: IRNode): ToolBinding | null {
  // A rag capability is dispatched by its NODE ID at runtime (the retrieval source lives in the
  // node's own config) — it must never mint an ir.tools binding, or the registry would carry a
  // shape the backend doesn't read.
  if (n.type === "rag") return null;
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

/** Re-derive `ir.tools` as a GLOBAL REGISTRY of the tools wired into the agent (like `ir.models`
 * auto-fills as you add llm bindings). A capability tool node — the source of a `channel:"tool"` edge
 * — contributes `ir.tools[<node id>] = <its binding>` (the binding LIVES on the node; this is the
 * index, tagged with its `kind`). Pre-existing legacy keys that aren't a node id (step-tool refs
 * via `config.tool`) are preserved; stale node-id mirrors are dropped. Idempotent, so
 * a load→save round-trip is identity. */
export function withDerivedTools(ir: IRDocument): IRDocument {
  const nodes = ir.nodes ?? [];
  const edges = ir.edges ?? [];
  const nodeById = new Map(nodes.map((n) => [n.id, n]));
  const capabilityIds = new Set(
    edges.filter((e) => (e.channel ?? "data") === "tool").map((e) => e.source),
  );
  const tools: Record<string, ToolBinding> = {};
  // keep legacy entries whose key is NOT a current node id (step config.tool refs); drop stale.
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

/** The three tool kinds, presented as ONE "Tool" node with a kind picker rather than two
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

/** Switch a tool node between its three kinds (one node, the kind is a picker, not two
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

// ── the guardrail check (one node whose KIND follows its check — the sole per-instance kind) ───────

/** A guardrail's check config, as the editor writes it. This mirrors the backend `GuardrailConfig`
 * shape; it is a local view-model, not an imported IR type, because the IR document treats a node's
 * `config` as opaque. A `rule` check is deterministic + inline; a `model` check is an LLM-judge call. */
export interface GuardrailCheck {
  type: "rule" | "model";
  rule?: { kind: string; spec?: Record<string, unknown> } | null;
  model?: { model: string; prompt: string; passOn?: string } | null;
}

/** Write a guardrail node's `check` + `onBlock` AND stamp its `kind` in the SAME edit — a rule check
 * is inline (orchestration), a model check is a classifier call (activity). Stamping here is what
 * keeps the node valid: the backend rejects a guardrail whose kind disagrees with its check, so
 * writing the check without re-stamping the kind would produce an unsaveable graph. Editing the check
 * legitimately changes the version hash (a rule vs a model guardrail are different agents) — the same
 * discipline as `setToolKind`; the FE never computes the hash itself. */
export function setGuardrailCheck(
  ir: IRDocument,
  nodeId: string,
  check: GuardrailCheck,
  onBlock: Record<string, unknown> = {},
): IRDocument {
  const kind: NodeKind = check.type === "model" ? "activity" : "orchestration";
  return {
    ...ir,
    nodes: (ir.nodes ?? []).map((n) =>
      n.id === nodeId ? { ...n, kind, config: { ...(n.config ?? {}), check, onBlock } } : n,
    ),
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

// ── bulk-add MCP tools (one gesture: spawn configured nodes, wired to an llm) ─────────────────────

// The spawned batch stacks in a small grid below its anchor so it reads as one gesture; the
// author drags or Tidies from there.
const BULK_GRID_COLS = 3;
const BULK_GRID_X = 240;
const BULK_GRID_Y = 100;

/** A function-name-safe node id from an MCP tool name. The node id IS the function name the model
 * calls, so `read_file` beats a generated `n_mcp_tool_3` — the model sees a meaningful name. A
 * collision (the same tool name from another server) gets a numeric suffix. */
function toolNodeId(name: string, taken: Set<string>): string {
  const base = name.replace(/[^a-zA-Z0-9_-]/g, "_").replace(/^_+|_+$/g, "") || "mcp_tool";
  let id = base;
  let i = 1;
  while (taken.has(id)) {
    i += 1;
    id = `${base}_${i}`;
  }
  return id;
}

/** Bulk-add MCP tools: one configured `mcp_tool` node per tool name, all reaching ONE server
 * (`server` a registered name XOR `connection` an mcp_server connection id — the same exactly-one
 * rule the node config enforces). With `attachTo` (an llm node id), each node's capability handle
 * is wired into that llm's tool-role in-port, so a batch is pick-and-done: no per-node config, no
 * per-node drag. A tool that already has a node for the same server is REUSED (wired if needed),
 * never duplicated — re-adding is idempotent. New nodes stack in a grid below the anchor
 * (`near` ?? `attachTo`); positions are `view`-only, never hashed content. `config.description`
 * stays null: an mcp tool self-describes at runtime via the server's tool list, so freezing the
 * browse-time description into hashed content would only let it drift. */
export function addMcpToolNodes(
  ir: IRDocument,
  opts: {
    server?: string;
    connection?: string;
    tools: string[];
    attachTo?: string;
    near?: string;
  },
): { ir: IRDocument; newIds: string[] } {
  const spec = NODE_TYPES.mcp_tool;
  if (!spec || opts.tools.length === 0) return { ir, newIds: [] };
  const nodes = ir.nodes ?? [];
  const target = opts.attachTo ? nodes.find((n) => n.id === opts.attachTo) : undefined;
  const toolsPort = (target?.ports?.in ?? []).find((p) => p.role === "tool");
  const useHandle = (spec.ports.out ?? []).find((p) => p.role === "tool")?.id ?? "use";

  const sameSource = (cfg: Record<string, unknown>) =>
    opts.server ? cfg.server === opts.server : cfg.connection === opts.connection;

  const takenNodeIds = new Set(nodes.map((n) => n.id));
  const takenEdgeIds = new Set((ir.edges ?? []).map((e) => e.id));
  const newNodes: IRNode[] = [];
  const newEdges: IREdge[] = [];
  const newIds: string[] = [];
  const wireIds: string[] = [];

  for (const name of opts.tools) {
    const existing = nodes.find(
      (n) =>
        n.type === "mcp_tool" &&
        ((n.config ?? {}) as Record<string, unknown>).tool === name &&
        sameSource((n.config ?? {}) as Record<string, unknown>),
    );
    if (existing) {
      wireIds.push(existing.id);
      continue;
    }
    const id = toolNodeId(name, takenNodeIds);
    takenNodeIds.add(id);
    newIds.push(id);
    wireIds.push(id);
    newNodes.push({
      id,
      type: "mcp_tool",
      kind: spec.kind,
      label: name,
      config: {
        ...structuredClone(spec.defaultConfig),
        tool: name,
        server: opts.server ?? null,
        connection: opts.connection ?? null,
      },
      // Fill the `required` default the registry spec omits, so the spawned node is byte-identical
      // to what a canvas load→save re-emits (the batch round-trips as-is).
      ports: {
        in: (spec.ports.in ?? []).map((p) => ({ ...p, required: p.required ?? true })),
        out: (spec.ports.out ?? []).map((p) => ({ ...p, required: p.required ?? true })),
      },
    });
  }

  if (target && toolsPort) {
    const wired = new Set(
      (ir.edges ?? [])
        .filter((e) => (e.channel ?? "data") === "tool" && e.target === target.id)
        .map((e) => e.source),
    );
    for (const id of wireIds) {
      if (wired.has(id)) continue;
      wired.add(id);
      newEdges.push({
        id: freshId("e", takenEdgeIds),
        source: id,
        sourceHandle: useHandle,
        target: target.id,
        targetHandle: toolsPort.id,
        channel: "tool",
        condition: null,
      });
    }
  }

  let next: IRDocument = {
    ...ir,
    nodes: [...nodes, ...newNodes],
    edges: [...(ir.edges ?? []), ...newEdges],
  };

  if (newIds.length > 0) {
    const positions = resolvePositions(
      next.nodes ?? [],
      next.edges ?? [],
      (next.view ?? {}) as ViewBlock,
    );
    const anchorId = opts.near ?? opts.attachTo;
    const anchor = (anchorId ? positions[anchorId] : undefined) ?? defaultAddPosition(ir);
    const place: Record<string, XYPosition> = {};
    newIds.forEach((id, i) => {
      place[id] = {
        x: anchor.x + (i % BULK_GRID_COLS) * BULK_GRID_X,
        y: anchor.y + BULK_GRID_Y + Math.floor(i / BULK_GRID_COLS) * BULK_GRID_Y,
      };
    });
    next = setNodePositions(next, place);
  }

  // Keep the derived ir.tools registry in step (idempotent — save re-derives the same).
  return { ir: withDerivedTools(next), newIds };
}
