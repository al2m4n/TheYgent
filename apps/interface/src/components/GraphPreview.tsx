// A miniature, static render of an agent's graph — the "canvas screenshot" for a card. It is NOT a
// rasterized screenshot (we don't capture the live canvas); it's a cheap, crisp SVG drawn from the
// agent's IR: nodes as kind-coloured boxes (matching the canvas's NodeView palette), edges as flow
// lines, positioned from the saved `view` layout (or auto-laid-out when a version has none). Deriving
// it from the IR means it's always in sync and costs no capture pipeline.

import type { IRDocument } from "@theygent/ir-types";
import { autoLayout } from "../adapter/layout";

// Node fill-by-kind, matching components/NodeView's KIND_STYLE dots (boundary/activity/orchestration).
const KIND_COLOR: Record<string, string> = {
  boundary: "#34d399",
  activity: "#60a5fa",
  orchestration: "#fbbf24",
};
const DEFAULT_COLOR = "#60a5fa";

// Surface colours ride the app's theme tokens (mirrors the canvas), so a theme change can never
// drift this preview from the real canvas it stands in for. Edges use the themed slate ramp.
const BG = "var(--c-bg)";
const NODE_FILL = "var(--c-elev)";
const EDGE_STROKE = "var(--color-slate-600)";

// Approximate the canvas node footprint so the bounding box + edge anchors line up with the layout.
const NW = 150;
const NH = 48;

export function GraphPreview({ ir, className = "" }: { ir: IRDocument; className?: string }) {
  const nodes = ir.nodes ?? [];
  const edges = ir.edges ?? [];

  if (nodes.length === 0) {
    // A visible hint, not a blank pane — an intentionally empty graph should not read as a preview
    // that failed to load.
    return (
      <div
        className={`flex items-center justify-center ${className}`}
        style={{ backgroundColor: BG }}
      >
        <span className="text-xs text-slate-500">Empty graph</span>
      </div>
    );
  }

  // Positions: the saved layout if every node has one, else a deterministic auto-layout.
  const viewNodes = ((
    ir.view as { nodes?: Record<string, { position?: { x: number; y: number } }> }
  )?.nodes ?? {}) as Record<string, { position?: { x: number; y: number } }>;
  const haveAll = nodes.every((n) => viewNodes[n.id]?.position);
  const pos: Record<string, { x: number; y: number }> = haveAll
    ? Object.fromEntries(
        nodes.map((n) => [n.id, viewNodes[n.id].position as { x: number; y: number }]),
      )
    : autoLayout(nodes, edges);

  const xs = nodes.map((n) => pos[n.id].x);
  const ys = nodes.map((n) => pos[n.id].y);
  const pad = 36;
  const minX = Math.min(...xs) - pad;
  const minY = Math.min(...ys) - pad;
  const vbW = Math.max(...xs) + NW + pad - minX;
  const vbH = Math.max(...ys) + NH + pad - minY;

  return (
    <div className={className} style={{ backgroundColor: BG }}>
      <svg
        viewBox={`${minX} ${minY} ${vbW} ${vbH}`}
        width="100%"
        height="100%"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        aria-label="Agent graph preview"
      >
        {edges.map((e) => {
          const a = pos[e.source];
          const b = pos[e.target];
          if (!a || !b) return null;
          // Flow direction: from the source's right edge to the target's left edge.
          return (
            <line
              key={e.id}
              x1={a.x + NW}
              y1={a.y + NH / 2}
              x2={b.x}
              y2={b.y + NH / 2}
              style={{ stroke: EDGE_STROKE }}
              strokeWidth={3}
            />
          );
        })}
        {nodes.map((n) => {
          const color = KIND_COLOR[n.kind] ?? DEFAULT_COLOR;
          return (
            <g key={n.id}>
              <rect
                x={pos[n.id].x}
                y={pos[n.id].y}
                width={NW}
                height={NH}
                rx={12}
                style={{ fill: NODE_FILL }}
                stroke={color}
                strokeWidth={3}
              />
              <circle cx={pos[n.id].x + 18} cy={pos[n.id].y + NH / 2} r={7} fill={color} />
            </g>
          );
        })}
      </svg>
    </div>
  );
}
