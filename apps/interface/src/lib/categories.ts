// The category colour system + per-vocabulary mappings, shared by Badge, the filter chips, and the
// tables — so a category (an engine, a run status, an MCP transport) reads the SAME colour in the
// table cell, the filter chip, and anywhere else. One place to change a category's colour.

export type Tone =
  | "slate"
  | "blue"
  | "green"
  | "amber"
  | "red"
  | "violet"
  | "cyan"
  | "teal"
  | "pink"
  | "orange";

// Each tone carries three looks: `badge` (solid pill — the default Badge), `dot` (a colour swatch the
// filter chips show even when inactive, so the colour identifies the category at rest), and
// `activeChip` (the selected-filter look — a tinted, ringed pill).
// `badge`/`activeChip` carry a light + a `dark:` variant for the SEMANTIC colours so a chip is light
// on a light theme and dark on a dark theme. (slate is the exception — it rides the inverted slate
// ramp from index.css, so a single set of slate classes already adapts.)
export const TONES: Record<Tone, { badge: string; dot: string; activeChip: string }> = {
  slate: {
    badge: "bg-slate-800 text-slate-300",
    dot: "bg-slate-400",
    activeChip: "border-slate-500 bg-slate-600/30 text-slate-100",
  },
  blue: {
    badge: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
    dot: "bg-blue-400",
    activeChip: "border-blue-500 bg-blue-500/15 text-blue-700 dark:text-blue-200",
  },
  green: {
    badge: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
    dot: "bg-emerald-400",
    activeChip: "border-emerald-500 bg-emerald-500/15 text-emerald-700 dark:text-emerald-200",
  },
  amber: {
    badge: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
    dot: "bg-amber-400",
    activeChip: "border-amber-500 bg-amber-500/15 text-amber-700 dark:text-amber-200",
  },
  red: {
    badge: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300",
    dot: "bg-red-400",
    activeChip: "border-red-500 bg-red-500/15 text-red-700 dark:text-red-200",
  },
  violet: {
    badge: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-300",
    dot: "bg-violet-400",
    activeChip: "border-violet-500 bg-violet-500/15 text-violet-700 dark:text-violet-200",
  },
  cyan: {
    badge: "bg-cyan-100 text-cyan-700 dark:bg-cyan-950 dark:text-cyan-300",
    dot: "bg-cyan-400",
    activeChip: "border-cyan-500 bg-cyan-500/15 text-cyan-700 dark:text-cyan-200",
  },
  teal: {
    badge: "bg-teal-100 text-teal-700 dark:bg-teal-950 dark:text-teal-300",
    dot: "bg-teal-400",
    activeChip: "border-teal-500 bg-teal-500/15 text-teal-700 dark:text-teal-200",
  },
  pink: {
    badge: "bg-pink-100 text-pink-700 dark:bg-pink-950 dark:text-pink-300",
    dot: "bg-pink-400",
    activeChip: "border-pink-500 bg-pink-500/15 text-pink-700 dark:text-pink-200",
  },
  orange: {
    badge: "bg-orange-100 text-orange-700 dark:bg-orange-950 dark:text-orange-300",
    dot: "bg-orange-400",
    activeChip: "border-orange-500 bg-orange-500/15 text-orange-700 dark:text-orange-200",
  },
};

export function toneOf(tone: string | undefined): {
  badge: string;
  dot: string;
  activeChip: string;
} {
  return TONES[(tone ?? "slate") as Tone] ?? TONES.slate;
}

// ── per-vocabulary colour maps ───────────────────────────────────────────────
// The engine/binding enum (mlx | vllm | llamacpp | openai-compatible) — the registry's category.
export const ENGINE_TONE: Record<string, Tone> = {
  mlx: "violet",
  vllm: "cyan",
  llamacpp: "green",
  "openai-compatible": "blue",
};
export const engineTone = (binding: string): Tone => ENGINE_TONE[binding] ?? "slate";

// Run status (created | streaming | waiting | completed | failed) — kept in sync with format.ts's
// StatusBadge so a status reads the same colour in the list row, the filter chip, and the detail pill.
export const STATUS_TONE: Record<string, Tone> = {
  completed: "green",
  failed: "red",
  streaming: "amber",
  waiting: "violet",
  created: "slate",
};
export const statusTone = (status: string): Tone => STATUS_TONE[status] ?? "slate";

// MCP transport (stdio today; http/sse reserved for the tool-plane seam).
export const TRANSPORT_TONE: Record<string, Tone> = {
  stdio: "blue",
  http: "teal",
  sse: "violet",
};
export const transportTone = (transport: string): Tone => TRANSPORT_TONE[transport] ?? "slate";

// ── helpers ──────────────────────────────────────────────────────────────────
// Count items by a category key (skips null/undefined) — drives the per-chip counts in the filter bar.
export function countBy<T>(
  items: T[],
  key: (x: T) => string | null | undefined,
): Record<string, number> {
  const out: Record<string, number> = {};
  for (const it of items) {
    const k = key(it);
    if (k != null) out[k] = (out[k] ?? 0) + 1;
  }
  return out;
}

// Toggle a value in/out of a selection array (the filter-chip click handler everywhere).
export function toggle(selected: string[], value: string): string[] {
  return selected.includes(value) ? selected.filter((v) => v !== value) : [...selected, value];
}
