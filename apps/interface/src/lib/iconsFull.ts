// The FULL Lucide icon set (all ~1,700 icons), for the icon picker's "search every icon" experience.
//
// This module statically imports lucide's complete `icons` map, so it is heavy. It is
// only ever reached through a dynamic `import()` (the lazy `IconPickerGrid`, and `NodeIcon`'s fallback
// for a non-curated override), so Vite splits it into its own chunk — the main canvas bundle stays
// lean and the full set loads on demand the first time someone opens the picker or renders a custom
// icon. The curated set in `icons.tsx` covers every node-type DEFAULT synchronously, so the canvas
// itself never needs this chunk.

import { icons as LUCIDE_ICONS, type LucideIcon } from "lucide-react";

// lucide keys its `icons` map by PascalCase component name (e.g. `ShieldCheck`); we address icons by
// kebab name everywhere (the id used on lucide.dev, and what the `view` block stores). This is the
// SAME conversion the curated `DEFAULT_NODE_ICONS`/`ICON_REGISTRY` keys use, so a default and its
// full-set entry resolve to one identical name — no duplicate, and the active default highlights.
function toKebab(pascal: string): string {
  return pascal
    .replace(/([a-z0-9])([A-Z])/g, "$1-$2")
    .replace(/([a-zA-Z])([0-9])/g, "$1-$2")
    .toLowerCase();
}

const BY_NAME = new Map<string, LucideIcon>();
for (const [pascal, Icon] of Object.entries(LUCIDE_ICONS as Record<string, LucideIcon>)) {
  BY_NAME.set(toKebab(pascal), Icon);
}

/** Every Lucide icon name (kebab), sorted — the searchable universe for the picker. */
export const ALL_ICON_NAMES: string[] = [...BY_NAME.keys()].sort();

/** Resolve any Lucide icon by kebab name, or `undefined` if there is no such icon. */
export function iconByName(name: string): LucideIcon | undefined {
  return BY_NAME.get(name);
}
