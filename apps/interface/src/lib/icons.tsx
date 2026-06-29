// Per-node-type icons for the canvas — Lucide (https://lucide.dev), a crisp single-stroke SVG set.
//
// An icon is NEVER part of the IR's hashed content: a *default* is derived here from the node `type`
// (exactly parallel to how `kind` colour is derived in NodeView), and a user *override* lives in the
// `view` block, which the server strips before hashing (decision §1.4). So changing a node's icon is
// a pure layout change, like dragging. The stored token (in `view.nodes[id].icon`, and the values of
// `DEFAULT_NODE_ICONS`) is a Lucide **icon name** (kebab-case, the icon's identity on lucide.dev) —
// `ICON_REGISTRY` maps that name to the React component. Storing a name (not a component) keeps the
// `view` field a plain string, so it round-trips through save/load untouched.

import {
  Bell,
  Bot,
  Boxes,
  Brain,
  ChartColumn,
  CircleCheck,
  CircleHelp,
  Code,
  Cpu,
  Database,
  Eye,
  FileText,
  Filter,
  Gauge,
  Globe,
  Hand,
  Key,
  Lightbulb,
  LogIn,
  LogOut,
  type LucideIcon,
  MessageSquare,
  Mic,
  Network,
  Package,
  Plug,
  Puzzle,
  Repeat,
  Replace,
  Rocket,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Split,
  Star,
  Target,
  Timer,
  Volume2,
  Webhook,
  Workflow,
  Wrench,
  Zap,
} from "lucide-react";
import { Suspense, lazy } from "react";

/** Lucide icon name (the kebab id used on lucide.dev) → the React component. The picker palette and
 * the per-type defaults reference icons by name; this is the one place that resolves a name to a
 * renderable component, so an icon name is a stable, serialisable token everywhere else. */
export const ICON_REGISTRY: Record<string, LucideIcon> = {
  "log-in": LogIn,
  "log-out": LogOut,
  brain: Brain,
  bot: Bot,
  "message-square": MessageSquare,
  wrench: Wrench,
  plug: Plug,
  split: Split,
  repeat: Repeat,
  boxes: Boxes,
  package: Package,
  workflow: Workflow,
  hand: Hand,
  settings: Settings,
  search: Search,
  "chart-column": ChartColumn,
  database: Database,
  globe: Globe,
  key: Key,
  zap: Zap,
  puzzle: Puzzle,
  "file-text": FileText,
  "circle-check": CircleCheck,
  "circle-help": CircleHelp,
  target: Target,
  rocket: Rocket,
  bell: Bell,
  lightbulb: Lightbulb,
  star: Star,
  "shield-check": ShieldCheck,
  mic: Mic,
  "volume-2": Volume2,
  code: Code,
  eye: Eye,
  filter: Filter,
  sparkles: Sparkles,
  webhook: Webhook,
  timer: Timer,
  cpu: Cpu,
  gauge: Gauge,
  replace: Replace,
  network: Network,
};

/** The default icon (a Lucide name) for each executable node `type`. Anything not listed falls back
 * to the gear (`settings`). Every value here is a key of `ICON_REGISTRY`. */
export const DEFAULT_NODE_ICONS: Record<string, string> = {
  input: "log-in",
  output: "log-out",
  llm: "brain",
  tool: "wrench",
  mcp_tool: "plug",
  router: "split",
  human: "hand",
  loop: "repeat",
  map: "boxes",
  subgraph: "workflow",
  // M19 palette
  transcribe: "mic",
  speak: "volume-2",
  guardrail: "shield-check",
  ratelimit: "gauge",
  quota: "chart-column",
  transform: "replace",
};

/** Shown for any node `type` without a dedicated default (e.g. a future M-something node). */
export const FALLBACK_ICON = "settings";

/** The default icon name for a node `type`, derived (never stored on the IR). */
export function defaultIconFor(nodeType: string): string {
  return DEFAULT_NODE_ICONS[nodeType] ?? FALLBACK_ICON;
}

// A Lucide icon name is lowercase kebab (e.g. `shield-check`, `volume-2`). The user may pick ANY of
// them in the inspector, so we trust any override that matches this shape; a non-matching override —
// e.g. a legacy emoji saved before the switch to Lucide — degrades to the type default instead of
// rendering as a broken glyph. (A kebab string that isn't a real icon falls back to the gear at
// render time via `NodeIcon`, so this stays a cheap, allocation-free check.)
const LUCIDE_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** The icon name a node actually renders: an explicit user override (from `view`, any Lucide icon)
 * or the type default. */
export function resolveIcon(nodeType: string, override?: string | null): string {
  if (override && LUCIDE_NAME.test(override)) return override;
  return defaultIconFor(nodeType);
}

/** A curated set of suggestions the icon picker shows BEFORE the user searches (the full Lucide set —
 * see `iconsFull.ts` — is searchable once they type). Includes the type defaults plus a handful of
 * generally-useful glyphs. Every entry is a key of `ICON_REGISTRY`, so each renders synchronously. */
export const ICON_CHOICES: string[] = [
  "brain",
  "bot",
  "message-square",
  "wrench",
  "plug",
  "split",
  "repeat",
  "boxes",
  "package",
  "workflow",
  "log-in",
  "log-out",
  "hand",
  "settings",
  "code",
  "search",
  "filter",
  "replace",
  "chart-column",
  "database",
  "globe",
  "key",
  "zap",
  "webhook",
  "timer",
  "cpu",
  "puzzle",
  "file-text",
  "shield-check",
  "gauge",
  "mic",
  "volume-2",
  "eye",
  "sparkles",
  "circle-check",
  "circle-help",
  "target",
  "rocket",
  "bell",
  "lightbulb",
  "star",
  "network",
];

interface NodeIconProps {
  name: string;
  className?: string;
  size?: number;
  strokeWidth?: number;
}

// Render ANY Lucide icon by name from the full set (`iconsFull.ts`), loaded lazily as its own chunk.
// Reached only for a CUSTOM override outside the curated `ICON_REGISTRY` — every node-type default is
// curated, so the canvas's default icons never pull this chunk. An unknown name falls back to the
// gear, so a stale/garbage override can never crash the canvas.
const AnyLucideIcon = lazy(async () => {
  const { iconByName } = await import("./iconsFull");
  function Resolved({ name, ...rest }: NodeIconProps) {
    const Icon = iconByName(name) ?? ICON_REGISTRY[FALLBACK_ICON];
    return <Icon aria-hidden {...rest} />;
  }
  return { default: Resolved };
});

/** Render a node icon by its Lucide name. A curated icon renders synchronously; any other icon
 * resolves from the full Lucide set (lazy chunk), showing the gear for the instant it loads. Colour
 * comes from `currentColor` (drive it with `text-*`). */
export function NodeIcon({ name, className, size = 16, strokeWidth = 2 }: NodeIconProps) {
  const Curated = ICON_REGISTRY[name];
  if (Curated) {
    return <Curated className={className} size={size} strokeWidth={strokeWidth} aria-hidden />;
  }
  const Gear = ICON_REGISTRY[FALLBACK_ICON];
  return (
    <Suspense
      fallback={<Gear className={className} size={size} strokeWidth={strokeWidth} aria-hidden />}
    >
      <AnyLucideIcon name={name} className={className} size={size} strokeWidth={strokeWidth} />
    </Suspense>
  );
}
