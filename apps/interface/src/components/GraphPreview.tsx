// A miniature, static render of an agent's graph — the "canvas screenshot" for a card. It is NOT a
// rasterized screenshot (we don't capture the live canvas); it's a cheap, crisp SVG drawn from the
// agent's IR: nodes as kind-coloured boxes (matching the canvas's NodeView palette), edges as flow
// lines, positioned from the saved `view` layout (or auto-laid-out when a version has none). Deriving
// it from the IR means it's always in sync and costs no capture pipeline.

import type { IRDocument } from "@theygent/ir-types";
import { autoLayout } from "../adapter/layout";
import { useTheme } from "../lib/theme";

// Node fill-by-kind, matching components/NodeView's KIND_STYLE dots (boundary/activity/orchestration).
const KIND_COLOR: Record<string, string> = {
  boundary: "#34d399",
  activity: "#60a5fa",
  orchestration: "#fbbf24",
};
const DEFAULT_COLOR = "#60a5fa";

// Surface colours per theme (mirrors the canvas): a dark pane in dark mode, a light pane in light.
const PALETTE = {
  dark: { bg: "#0b0e14", node: "#161b26", edge: "#475569" },
  light: { bg: "#eef1f6", node: "#ffffff", edge: "#cbd5e1" },
};

// Approximate the canvas node footprint so the bounding box + edge anchors line up with the layout.
const NW = 150;
const NH = 48;

export function GraphPreview({ ir, className = "" }: { ir: IRDocument; className?: string }) {
  const { resolved } = useTheme();
  const c = resolved === "light" ? PALETTE.light : PALETTE.dark;
  const nodes = ir.nodes ?? [];
  const edges = ir.edges ?? [];

  if (nodes.length === 0) {
    return <div className={className} style={{ backgroundColor: c.bg }} />;
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
    <div className={className} style={{ backgroundColor: c.bg }}>
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
              stroke={c.edge}
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
                fill={c.node}
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
