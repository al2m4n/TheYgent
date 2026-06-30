// A PLACEHOLDER agent thumbnail: a deterministic identicon — a symmetric pixel grid on a colour
// picked from the agent id (so every agent gets a stable, distinct avatar). This stands in until we
// decide on a real thumbnail (an upload, a generated image, …); to swap it, replace the render here
// and nothing else changes. The "Change" affordance re-rolls the placeholder; the chosen variant
// persists per agent in localStorage — it is pure UI chrome, NEVER the agent IR.

import { useState } from "react";

// A small, mockup-leaning palette. The identicon's colour is `PALETTE[hash % len]`.
const PALETTE = [
  "#7c3aed",
  "#2563eb",
  "#0891b2",
  "#059669",
  "#eab308",
  "#f59e0b",
  "#b45309",
  "#dc2626",
  "#db2777",
  "#6366f1",
];

// FNV-1a — a cheap, stable string hash (deterministic across reloads/machines).
function hashStr(s: string): number {
  let h = 2166136261 >>> 0;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

// Dark pixels on a light background, light pixels on a dark one — so the avatar reads on any colour.
function foregroundFor(bg: string): string {
  const r = Number.parseInt(bg.slice(1, 3), 16);
  const g = Number.parseInt(bg.slice(3, 5), 16);
  const b = Number.parseInt(bg.slice(5, 7), 16);
  const lum = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
  return lum > 0.62 ? "rgba(15,19,28,0.82)" : "rgba(255,255,255,0.92)";
}

export function AgentThumbnail({ seed, className = "" }: { seed: string; className?: string }) {
  const h = hashStr(seed);
  const bg = PALETTE[h % PALETTE.length];
  const fg = foregroundFor(bg);

  // A 5×5 grid, mirrored left↔right for a face-like symmetry (the classic identicon trick). Bits of
  // the hash decide which of the 3 left columns are filled; the outer two mirror.
  const cells: Array<[number, number]> = [];
  for (let y = 0; y < 5; y++) {
    for (let x = 0; x < 3; x++) {
      if ((h >> (y * 3 + x)) & 1) {
        cells.push([x, y]);
        if (x < 2) cells.push([4 - x, y]);
      }
    }
  }

  return (
    <div
      className={`flex items-center justify-center ${className}`}
      style={{ backgroundColor: bg }}
    >
      <svg
        viewBox="-0.5 -0.5 6 6"
        width="52%"
        height="52%"
        shapeRendering="crispEdges"
        role="img"
        aria-label="Agent thumbnail placeholder"
      >
        {cells.map(([x, y]) => (
          <rect key={`${x}-${y}`} x={x} y={y} width={1} height={1} fill={fg} />
        ))}
      </svg>
    </div>
  );
}

// Per-agent placeholder variant (the "Change" re-roll). Persisted so a chosen avatar sticks across
// reloads; bounded by the number of agents. Temporary — goes away when a real thumbnail field lands.
export function useThumbVariant(id: string): { seed: string; reroll: () => void } {
  const key = `theygent.agentThumb.${id}`;
  const [variant, setVariant] = useState<number>(() => {
    try {
      return Number(localStorage.getItem(key)) || 0;
    } catch {
      return 0;
    }
  });
  const reroll = () =>
    setVariant((cur) => {
      const next = cur + 1;
      try {
        localStorage.setItem(key, String(next));
      } catch {
        // no localStorage (tests) — the re-roll still applies for this session.
      }
      return next;
    });
  return { seed: variant ? `${id}#${variant}` : id, reroll };
}
