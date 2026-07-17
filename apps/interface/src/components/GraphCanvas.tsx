// The canvas: a React Flow view of the IR. The IR (held by the editor route) is the single source
// of truth for STRUCTURE + persistence; React Flow owns the transient interaction state (live drag,
// measured node dimensions, in-pane selection) via useNodesState/useEdgesState. We seed RF from the
// IR whenever the structure changes (add/connect/delete/relabel/load) and commit positions back to
// the IR's `view` on drag stop — layout only, never hashed content (the view block is stripped
// before hashing, so a drag never bumps the version).
//
// Why not fully-controlled (nodes derived from the IR every render): React Flow persists measured
// dimensions and drag state THROUGH its own change pipeline; re-deriving nodes each render drops
// those, so edges/handles lose their anchor mid-drag and the graph flickers/disappears. Letting RF
// own the interaction buffer (and only re-seeding on structure) is the supported pattern. RF's
// node/edge types still never escape this component — the parent speaks only IR (the one rule).

import type { IRDocument } from "@theygent/ir-types";
import {
  Background,
  BackgroundVariant,
  type Connection,
  ControlButton,
  Controls,
  MiniMap,
  type Node as RFNode,
  ReactFlow,
  ReactFlowProvider,
  SelectionMode,
  useEdgesState,
  useNodesInitialized,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import { CircleHelp, Redo2, Undo2, Wand2 } from "lucide-react";
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
import { notify } from "../lib/notify";
import { useTheme } from "../lib/theme";
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
  // Like reseedKey, forces a re-seed from the IR (so positions land back on the canvas), but WITHOUT
  // refitting the viewport. Bumped by undo/redo — a position-only undo changes no structural signature,
  // so RF would otherwise keep the post-drag positions and the undo would look like a no-op.
  resyncKey?: number;
  // A node/edge to transiently flash (e.g. the one an issue points at, on hover) — display only.
  highlight?: Selection;
  // Per-node execution state during an in-canvas test run (running | ok | err | skipped), joined
  // from the run's trace spans by node id. Display only — a class on the node, never IR state.
  runState?: Record<string, string>;
  // "minimal" strips the chrome (controls, minimap, help) — used by the bench, where the canvas is a
  // read-only preview and the controls would only clutter it.
  minimal?: boolean;
  // Undo/redo wired into the Controls toolbar (the editor owns the history). Absent ⇒ no buttons.
  onUndo?: () => void;
  onRedo?: () => void;
  canUndo?: boolean;
  canRedo?: boolean;
  // "Tidy" (re-run auto-layout) lives on the canvas toolbar with the view controls it belongs beside,
  // not in the top chrome. Absent ⇒ no button (e.g. the read-only bench preview). Owned by the editor.
  onTidy?: () => void;
}

type Menu = { x: number; y: number; kind: "node" | "edge"; id: string } | null;

// A signature of everything the canvas renders EXCEPT positions. When it changes we re-seed React
// Flow from the IR; a position-only change (a drag we just committed) leaves it identical, so the
// re-seed never fires mid-interaction and fights the drag. The per-node icon override (a `view`
// display field) IS included — picking an icon must re-seed so the node re-renders, the way a label
// edit does; it's never touched mid-drag, so it can't fight an interaction.
function structuralSignature(ir: IRDocument): string {
  const icons = (ir.view as { nodes?: Record<string, { icon?: string }> } | undefined)?.nodes ?? {};
  return JSON.stringify({
    n: (ir.nodes ?? []).map((n) => [n.id, n.type, n.label, n.ports, icons[n.id]?.icon ?? null]),
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

function GraphCanvasInner({
  ir,
  onChange,
  selection,
  onSelect,
  reseedKey = 0,
  resyncKey = 0,
  highlight,
  runState,
  minimal = false,
  onUndo,
  onRedo,
  canUndo,
  canRedo,
  onTidy,
}: Props) {
  const { screenToFlowPosition, fitView } = useReactFlow();
  const { resolved } = useTheme();
  const dark = resolved === "dark";
  const [rfNodes, setRfNodes, onNodesChange] = useNodesState<TheygentRFNode>([]);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState<TheygentRFEdge>([]);
  const [menu, setMenu] = useState<Menu>(null);
  // Pins the interaction legend open — the button click path for touch/keyboard users, who never
  // get the hover reveal.
  const [helpOpen, setHelpOpen] = useState(false);

  // Read-latest refs so event handlers stay stable but always see current state.
  const irRef = useRef(ir);
  irRef.current = ir;
  const selectionRef = useRef(selection);
  selectionRef.current = selection;
  // When a selection change originates IN-PANE (a click / box-select), skip the next external-sync
  // effect — otherwise it forces RF back to the single parent selection and collapses a multi-select.
  const inPaneSelRef = useRef(false);

  // Re-seed RF from the IR ONLY when the structure changes (the signature differs). The effect runs
  // on every `ir` change, but a position-only change (drag commit) has an identical signature and
  // early-returns — so React Flow keeps its measured dimensions + live state untouched.
  const lastSigRef = useRef<string | null>(null);
  useEffect(() => {
    // `reseedKey`/`resyncKey` are part of the trigger so an explicit re-seed (Tidy) or an undo/redo
    // refreshes positions even though they're excluded from the structural signature (a live drag
    // must NOT re-seed, or it would fight the in-flight motion).
    const sig = `${structuralSignature(ir)}#${reseedKey}#${resyncKey}`;
    if (lastSigRef.current === sig) return;
    lastSigRef.current = sig;
    const g = irToReactFlow(ir);
    const sel = withSelection(g.nodes, g.edges, selectionRef.current);
    setRfNodes(sel.nodes);
    setRfEdges(sel.edges);
  }, [ir, reseedKey, resyncKey, setRfNodes, setRfEdges]);

  // After an explicit re-seed (Tidy), refit the freshly laid-out graph.
  useEffect(() => {
    if (reseedKey > 0) {
      const id = window.setTimeout(() => fitView({ padding: 0.2, duration: 200 }), 60);
      return () => window.clearTimeout(id);
    }
  }, [reseedKey, fitView]);

  // Reflect EXTERNAL selection changes (e.g. the inspector's "Duplicate" selects the new node) into
  // React Flow's highlight. Normal in-pane clicks flow the other way (onSelectionChange → parent) and
  // are skipped here via `inPaneSelRef` — forcing the single parent selection back onto RF would
  // collapse a box/shift multi-selection the instant it's made.
  useEffect(() => {
    if (inPaneSelRef.current) {
      inPaneSelRef.current = false;
      return;
    }
    setRfNodes((ns) =>
      ns.map((n) => ({ ...n, selected: selection?.kind === "node" && selection.id === n.id })),
    );
    setRfEdges((es) =>
      es.map((e) => ({ ...e, selected: selection?.kind === "edge" && selection.id === e.id })),
    );
  }, [selection, setRfNodes, setRfEdges]);

  // Flash the node/edge the editor points at (e.g. hovering an issue) and wear the live test-run
  // state. Both toggle a `className` only — never the IR or RF's selection. Cleared when the
  // highlight goes null / the run states are cleared.
  useEffect(() => {
    setRfNodes((ns) =>
      ns.map((n) => {
        const parts: string[] = [];
        if (highlight?.kind === "node" && highlight.id === n.id) parts.push("theygent-flash");
        const status = runState?.[n.id];
        if (status) parts.push(`theygent-run-${status}`);
        return { ...n, className: parts.length > 0 ? parts.join(" ") : undefined };
      }),
    );
    setRfEdges((es) =>
      es.map((e) => ({
        ...e,
        className:
          highlight?.kind === "edge" && highlight.id === e.id ? "theygent-flash" : undefined,
      })),
    );
  }, [highlight, runState, setRfNodes, setRfEdges]);

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
        // Surface in the one global toast (a stable id replaces, rather than stacks, when the user
        // fumbles several connections in a row) — not a canvas-local banner.
        notify.error(r.error, { id: "canvas-connect-error" });
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
      // This change came from the canvas, so don't let the external-sync effect re-apply over it.
      inPaneSelRef.current = true;
      // A multi-selection has no single inspector target — clear the parent selection (RF keeps the
      // multi-selection internally, so drag-move-together still works), otherwise track the one.
      if (sn.length > 1) onSelect(null);
      else if (sn[0]) onSelect({ kind: "node", id: sn[0].id });
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
  // Coordinates are clamped so a click near the right/bottom edge doesn't push the menu outside
  // the canvas.
  const wrapperRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const openMenu = useCallback((e: React.MouseEvent, kind: "node" | "edge", id: string) => {
    e.preventDefault();
    const rect = wrapperRef.current?.getBoundingClientRect();
    let x = e.clientX - (rect?.left ?? 0);
    let y = e.clientY - (rect?.top ?? 0);
    if (rect) {
      x = Math.max(0, Math.min(x, rect.width - 150));
      y = Math.max(0, Math.min(y, rect.height - 80));
    }
    setMenu({ x, y, kind, id });
  }, []);

  // While the menu is open, Escape and any pointer press outside it dismiss it — pane-level
  // handlers alone miss clicks that land on the toolbar/inspector.
  useEffect(() => {
    if (!menu) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenu(null);
    };
    const onPointerDown = (e: PointerEvent) => {
      if (!menuRef.current?.contains(e.target as Node)) setMenu(null);
    };
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onPointerDown);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("pointerdown", onPointerDown);
    };
  }, [menu]);

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
        // Marquee select: left-drag on the pane draws a selection box (drag any selected node to move
        // them together). Panning moves to middle/right-drag (and Space-drag via the default
        // panActivationKeyCode); scroll still zooms.
        selectionOnDrag
        panOnDrag={[1, 2]}
        selectionMode={SelectionMode.Partial}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={20}
          size={1}
          color={dark ? "#1c2430" : "#c9d2de"}
        />
        {!minimal && (
          // bg + icon colour set here (the RF default leaves the icons inheriting the near-white
          // body colour → invisible on the white default button). slate-300 inverts via the theme
          // ramp, so icons stay legible in both themes.
          <Controls className="!bg-[var(--c-elev)] !text-slate-300" showInteractive={false}>
            {onTidy && (
              <ControlButton onClick={() => onTidy()} title="Tidy layout — auto-arrange the nodes">
                <Wand2 size={15} />
              </ControlButton>
            )}
            {(onUndo || onRedo) && (
              <>
                <ControlButton onClick={() => onUndo?.()} disabled={!canUndo} title="Undo">
                  <Undo2 size={16} />
                </ControlButton>
                <ControlButton onClick={() => onRedo?.()} disabled={!canRedo} title="Redo">
                  <Redo2 size={16} />
                </ControlButton>
              </>
            )}
          </Controls>
        )}
        {!minimal && (
          <MiniMap
            pannable
            zoomable
            // A border + an elevated background (lighter than the canvas in dark, white in light) so
            // the minimap reads against the pane instead of blending in (the "barely visible" fix).
            className="!rounded-md !border !border-slate-700 !bg-[var(--c-elev)]"
            maskColor={dark ? "rgba(11,14,20,0.55)" : "rgba(226,232,240,0.7)"}
            nodeColor={dark ? "#64748b" : "#94a3b8"}
            nodeStrokeColor={dark ? "#94a3b8" : "#64748b"}
          />
        )}
      </ReactFlow>

      {/* interaction legend — collapsed behind a help icon, revealed on hover or focus, and
          toggleable by click/Enter for touch and keyboard users (hidden in minimal) */}
      {!minimal && (
        <div className="group absolute left-3 top-3">
          <button
            type="button"
            aria-label="Canvas help"
            aria-expanded={helpOpen}
            onClick={() => setHelpOpen((v) => !v)}
            onBlur={() => setHelpOpen(false)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setHelpOpen(false);
            }}
            className="flex h-6 w-6 items-center justify-center rounded-full border border-slate-700 bg-[var(--c-surface)]/80 text-slate-400 transition-colors hover:border-slate-500 hover:text-slate-200"
          >
            <CircleHelp size={14} aria-hidden />
          </button>
          <div
            className={`pointer-events-none absolute left-0 top-8 w-max rounded-md border border-slate-800 bg-[var(--c-surface)]/95 px-2.5 py-1.5 text-[10px] leading-relaxed text-slate-500 shadow-lg transition-opacity ${
              helpOpen
                ? "visible opacity-100"
                : "invisible opacity-0 group-focus-within:visible group-focus-within:opacity-100 group-hover:visible group-hover:opacity-100"
            }`}
          >
            <div>
              <span className="text-slate-300">Drag</span> a node from the palette to add
            </div>
            <div>
              <span className="text-slate-300">Drag handle → handle</span> to connect ports
            </div>
            <div>
              <span className="text-slate-300">Click</span> a node to select &amp; edit its config
            </div>
            <div>
              <span className="text-slate-300">Del</span> to remove ·{" "}
              <span className="text-slate-300">Right-click</span> for menu
            </div>
            <div>
              <span className="text-slate-300">Drag</span> empty canvas to box-select; drag any
              selected to move together
            </div>
            <div>
              <span className="text-slate-300">Middle/right-drag</span> or{" "}
              <span className="text-slate-300">Space-drag</span> to pan · scroll to zoom
            </div>
          </div>
        </div>
      )}

      {menu && (
        <div
          ref={menuRef}
          role="menu"
          className="absolute z-10 min-w-[140px] overflow-hidden rounded-md border border-slate-700 bg-[var(--c-elev)] py-1 text-sm shadow-xl"
          style={{ left: menu.x, top: menu.y }}
        >
          {menu.kind === "node" && (
            <button
              type="button"
              role="menuitem"
              className="block w-full px-3 py-1.5 text-left text-slate-200 hover:bg-[var(--c-hover)]"
              onClick={() => runMenu("duplicate")}
            >
              Duplicate node
            </button>
          )}
          <button
            type="button"
            role="menuitem"
            className="block w-full px-3 py-1.5 text-left text-red-600 hover:bg-red-50 dark:text-red-300 dark:hover:bg-red-950"
            onClick={() => runMenu("delete")}
          >
            Delete {menu.kind}
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
