// Per-node-type icons for the canvas — PURE DISPLAY, zero dependencies.
//
// We use emoji deliberately: they render everywhere (every OS/browser ships them), need no icon
// font or SVG package, and keep the bundle untouched. An icon is NEVER part of the IR's hashed
// content — a *default* is derived here from the node `type` (exactly parallel to how `kind` colour
// is derived in NodeView), and a user *override* lives in the `view` block, which the server strips
// before hashing (decision §1.4). So changing a node's icon is a pure layout change, like dragging.

/** The default icon for each executable node `type`. Anything not listed falls back to the gear. */
export const DEFAULT_NODE_ICONS: Record<string, string> = {
  input: "📥",
  output: "📤",
  llm: "🧠",
  tool: "🔧",
  mcp_tool: "🔌",
  router: "🔀",
  human: "🙋",
  loop: "🔁",
  map: "🗂️",
  subgraph: "📦",
  // M19 palette
  transcribe: "🎙️",
  speak: "🔊",
  guardrail: "🛡️",
  ratelimit: "🚦",
  quota: "📊",
  transform: "🔄",
};

/** Shown for any node `type` without a dedicated default (e.g. a future M-something node). */
export const FALLBACK_ICON = "⚙️";

/** The default icon for a node `type`, derived (never stored on the IR). */
export function defaultIconFor(nodeType: string): string {
  return DEFAULT_NODE_ICONS[nodeType] ?? FALLBACK_ICON;
}

/** The icon a node actually renders: an explicit user override (from `view`) or the type default. */
export function resolveIcon(nodeType: string, override?: string | null): string {
  return override && override.length > 0 ? override : defaultIconFor(nodeType);
}

/** A curated palette the inspector offers when the user wants to swap a node's icon. Includes the
 * type defaults plus a handful of generally-useful glyphs — pick one, or revert to the default. */
export const ICON_CHOICES: string[] = [
  "🧠",
  "🤖",
  "💬",
  "🔧",
  "🔌",
  "🔀",
  "🔁",
  "🗂️",
  "📦",
  "📥",
  "📤",
  "🙋",
  "⚙️",
  "🔍",
  "📊",
  "🗄️",
  "🌐",
  "🔑",
  "⚡",
  "🧩",
  "📝",
  "✅",
  "❓",
  "🎯",
  "🚀",
  "🔔",
  "💡",
  "⭐",
];
