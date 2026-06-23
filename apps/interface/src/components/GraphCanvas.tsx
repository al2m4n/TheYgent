// The canvas: a React Flow view of the IR. The IR (held by the editor route) is the single source
// of truth for STRUCTURE + persistence; React Flow owns the transient interaction state (live drag,
// measured node dimensions, in-pane selection) via useNodesState/useEdgesState. We seed RF from the
// IR whenever the structure changes (add/connect/delete/relabel/load) and commit positions back to
// the IR's `view` on drag stop — layout only, never hashed content (decision §1.4).
//
// Why not fully-controlled (nodes derived from the IR every render): React Flow persists measured
// dimensions and drag state THROUGH its own change pipeline; re-deriving nodes each render drops
// those, so edges/handles lose their anchor mid-drag and the graph flickers/disappears. Letting RF
// own the interaction buffer (and only re-seeding on structure) is the supported pattern. RF's
// node/edge types still never escape this component — the parent speaks only IR (the one rule, §0).

import type { IRDocument } from "@theygent/ir-types";
import {
  Background,
  BackgroundVariant,
  type Connection,
  Controls,
  MiniMap,
  type Node as RFNode,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesInitialized,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  type Selection,
  type TheygentRFEdge,
  type TheygentRFNode,
  addNode,
  connect,
  deleteEdges,
  deleteNodes,
  duplicateNode,
  irToReactFlow,
  setNodePositions,
} from "../adapter";
import { TheygentNode } from "./NodeView";

const nodeTypes = { theygent: TheygentNode };

interface Props {
  ir: IRDocument;
  onChange: (ir: IRDocument) => void;
  selection: Selection;
  onSelect: (s: Selection) => void;
  // Bumped by the editor (e.g. the "Tidy" button) to force a re-seed + refit from the IR even when
  // only `view` positions changed — those are excluded from the structural signature on purpose.
  reseedKey?: number;
}

type Menu = { x: number; y: number; kind: "node" | "edge"; id: string } | null;

// A signature of everything the canvas renders EXCEPT positions. When it changes we re-seed React
// Flow from the IR; a position-only change (a drag we just committed) leaves it identical, so the
// re-seed never fires mid-interaction and fights the drag.
function structuralSignature(ir: IRDocument): string {
  return JSON.stringify({
    n: (ir.nodes ?? []).map((n) => [n.id, n.type, n.label, n.ports]),
    e: (ir.edges ?? []).map((e) => [
      e.id,
      e.source,
      e.sourceHandle,
      e.target,
      e.targetHandle,
      e.channel,
      e.condition,
    ]),
  });
}

function withSelection(
  nodes: TheygentRFNode[],
  edges: TheygentRFEdge[],
  sel: Selection,
): { nodes: TheygentRFNode[]; edges: TheygentRFEdge[] } {
  return {
    nodes: nodes.map((n) => ({ ...n, selected: sel?.kind === "node" && sel.id === n.id })),
    edges: edges.map((e) => ({ ...e, selected: sel?.kind === "edge" && sel.id === e.id })),
  };
}

function GraphCanvasInner({ ir, onChange, selection, onSelect, reseedKey = 0 }: Props) {
  const { screenToFlowPosition, fitView } = useReactFlow();
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<TheygentRFNode>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<TheygentRFEdge>([]);
  const [connectError, setConnectError] = useState<string | null>(null);
  const [menu, setMenu] = useState<Menu>(null);

  // Read-latest refs so event handlers stay stable but always see current state.
  const irRef = useRef(ir);
  irRef.current = ir;
  const selectionRef = useRef(selection);
  selectionRef.current = selection;

  // Re-seed RF from the IR ONLY when the structure changes (the signature differs). The effect runs
  // on every `ir` change, but a position-only change (drag commit) has an identical signature and
  // early-returns — so React Flow keeps its measured dimensions + live state untouched.
  const lastSigRef = useRef<string | null>(null);
  useEffect(() => {
    // `reseedKey` is part of the trigger so an explicit re-seed (Tidy) refreshes positions even
    // though they're excluded from the structural signature (drags must NOT re-seed).
    const sig = `${structuralSignature(ir)}#${reseedKey}`;
    if (lastSigRef.current === sig) return;
    lastSigRef.current = sig;
    const g = irToReactFlow(ir);
    const sel = withSelection(g.nodes, g.edges, selectionRef.current);
    setRfNodes(sel.nodes);
    setRfEdges(sel.edges);
  }, [ir, reseedKey, setRfNodes, setRfEdges]);

  // After an explicit re-seed (Tidy), refit the freshly laid-out graph.
  useEffect(() => {
    if (reseedKey > 0) {
      const id = window.setTimeout(() => fitView({ padding: 0.2, duration: 200 }), 60);
      return () => window.clearTimeout(id);
    }
  }, [reseedKey, fitView]);

  // Reflect EXTERNAL selection changes (e.g. the inspector's "Duplicate" selects the new node) into
  // React Flow's highlight. Normal in-pane clicks flow the other way (onSelectionChange → parent).
  useEffect(() => {
    setRfNodes((ns) =>
      ns.map((n) => ({ ...n, selected: selection?.kind === "node" && selection.id === n.id })),
    );
    setRfEdges((es) =>
      es.map((e) => ({ ...e, selected: selection?.kind === "edge" && selection.id === e.id })),
    );
  }, [selection, setRfNodes, setRfEdges]);

  // Fit the view once, after nodes are first measured (an async-loaded agent would otherwise fit
  // against unmeasured nodes and clip off-screen). Never on edits — that would yank the user's pan.
  const nodesInitialized = useNodesInitialized();
  const fittedRef = useRef(false);
  useEffect(() => {
    if (nodesInitialized && !fittedRef.current && rfNodes.length > 0) {
      fittedRef.current = true;
      fitView({ padding: 0.2, duration: 200 });
    }
  }, [nodesInitialized, rfNodes.length, fitView]);

  // Commit a drag to the IR's `view` (layout only) when it settles — not every tick, so the live
  // drag stays smooth and React Flow owns the in-flight motion.
  const onNodeDragStop = useCallback(
    (_e: unknown, _node: RFNode, dragged: RFNode[]) => {
      const moved: Record<string, { x: number; y: number }> = {};
      for (const n of dragged) moved[n.id] = n.position;
      if (Object.keys(moved).length > 0) onChange(setNodePositions(irRef.current, moved));
    },
    [onChange],
  );

  const onConnect = useCallback(
    (c: Connection) => {
      const r = connect(irRef.current, c);
      if (r.error) {
        setConnectError(r.error);
        window.setTimeout(() => setConnectError(null), 4000);
        return;
      }
      if (r.ir) onChange(r.ir);
    },
    [onChange],
  );

  const onNodesDelete = useCallback(
    (deleted: { id: string }[]) => {
      onChange(
        deleteNodes(
          irRef.current,
          deleted.map((n) => n.id),
        ),
      );
      onSelect(null);
    },
    [onChange, onSelect],
  );

  const onEdgesDelete = useCallback(
    (deleted: { id: string }[]) => {
      onChange(
        deleteEdges(
          irRef.current,
          deleted.map((e) => e.id),
        ),
      );
      onSelect(null);
    },
    [onChange, onSelect],
  );

  const onSelectionChange = useCallback(
    ({ nodes: sn, edges: se }: { nodes: RFNode[]; edges: { id: string }[] }) => {
      if (sn[0]) onSelect({ kind: "node", id: sn[0].id });
      else if (se[0]) onSelect({ kind: "edge", id: se[0].id });
      else onSelect(null);
    },
    [onSelect],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const type = e.dataTransfer.getData("application/theygent-node-type");
      if (!type) return;
      const position = screenToFlowPosition({ x: e.clientX, y: e.clientY });
      onChange(addNode(irRef.current, type, position));
    },
    [onChange, screenToFlowPosition],
  );

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = "move";
  }, []);

  // Right-click → a small context menu (delete / duplicate), positioned in CSS px in the wrapper.
  const wrapperRef = useRef<HTMLDivElement>(null);
  const openMenu = useCallback((e: React.MouseEvent, kind: "node" | "edge", id: string) => {
    e.preventDefault();
    const rect = wrapperRef.current?.getBoundingClientRect();
    setMenu({ x: e.clientX - (rect?.left ?? 0), y: e.clientY - (rect?.top ?? 0), kind, id });
  }, []);

  const runMenu = (action: "delete" | "duplicate") => {
    if (!menu) return;
    const cur = irRef.current;
    if (action === "delete") {
      onChange(menu.kind === "node" ? deleteNodes(cur, [menu.id]) : deleteEdges(cur, [menu.id]));
      onSelect(null);
    } else if (menu.kind === "node") {
      const { ir: next, newId } = duplicateNode(cur, menu.id);
      onChange(next);
      onSelect({ kind: "node", id: newId });
    }
    setMenu(null);
  };

  return (
    <div
      ref={wrapperRef}
      className="relative h-full w-full"
      onDrop={onDrop}
      onDragOver={onDragOver}
    >
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeDragStop={onNodeDragStop}
        onConnect={onConnect}
        onNodesDelete={onNodesDelete}
        onEdgesDelete={onEdgesDelete}
        onSelectionChange={onSelectionChange}
        onNodeContextMenu={(e, n) => openMenu(e, "node", n.id)}
        onEdgeContextMenu={(e, ed) => openMenu(e, "edge", ed.id)}
        onPaneClick={() => setMenu(null)}
        onMoveStart={() => setMenu(null)}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.15}
        proOptions={{ hideAttribution: true }}
        deleteKeyCode={["Backspace", "Delete"]}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#1c2430" />
        <Controls className="!bg-[#161b26] !text-slate-300" showInteractive={false} />
        <MiniMap
          pannable
          zoomable
          className="!bg-[#0e131c]"
          maskColor="rgba(11,14,20,0.6)"
          nodeColor="#334155"
        />
      </ReactFlow>

      {/* interaction legend — makes the structural operations discoverable */}
      <div className="pointer-events-none absolute left-3 top-3 rounded-md border border-slate-800 bg-[#0e131c]/80 px-2.5 py-1.5 text-[10px] leading-relaxed text-slate-500">
        <div>
          <span className="text-slate-300">Drag</span> a node from the palette to add
        </div>
        <div>
          <span className="text-slate-300">Drag handle → handle</span> to connect
        </div>
        <div>
          <span className="text-slate-300">Click</span> to select ·{" "}
          <span className="text-slate-300">Del</span> to remove ·{" "}
          <span className="text-slate-300">Right-click</span> for menu
        </div>
      </div>

      {menu && (
        <div
          className="absolute z-10 min-w-[140px] overflow-hidden rounded-md border border-slate-700 bg-[#161b26] py-1 text-sm shadow-xl"
          style={{ left: menu.x, top: menu.y }}
        >
          {menu.kind === "node" && (
            <button
              type="button"
              className="block w-full px-3 py-1.5 text-left text-slate-200 hover:bg-[#1d2433]"
              onClick={() => runMenu("duplicate")}
            >
              Duplicate node
            </button>
          )}
          <button
            type="button"
            className="block w-full px-3 py-1.5 text-left text-red-300 hover:bg-red-950"
            onClick={() => runMenu("delete")}
          >
            Delete {menu.kind}
          </button>
        </div>
      )}

      {connectError && (
        <div className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-md border border-red-900 bg-red-950 px-3 py-1.5 text-xs text-red-200 shadow-lg">
          {connectError}
          <button
            type="button"
            className="ml-2 text-red-400 hover:text-red-200"
            onClick={() => setConnectError(null)}
          >
            ✕
          </button>
        </div>
      )}
    </div>
  );
}

export function GraphCanvas(props: Props) {
  return (
    <ReactFlowProvider>
      <GraphCanvasInner {...props} />
    </ReactFlowProvider>
  );
}
