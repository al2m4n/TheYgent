// The icon picker's result grid — search across EVERY Lucide icon (not a curated subset).
//
// Lazy-loaded by the inspector (so the full ~1,700-icon set in `iconsFull` only loads when a user
// actually opens the picker). With an empty query it shows the curated suggestions; once the user
// types, it filters the entire Lucide name set. Results are capped so a broad query can't try to
// paint 1,700 SVGs at once — narrow the search to see more.

import { useMemo } from "react";
import { ICON_CHOICES } from "../lib/icons";
import { ALL_ICON_NAMES, iconByName } from "../lib/iconsFull";

const RESULT_CAP = 120;

export default function IconPickerGrid({
  query,
  current,
  onPick,
}: {
  query: string;
  current: string;
  onPick: (name: string) => void;
}) {
  const q = query.trim().toLowerCase();
  const matches = useMemo(
    () => (q ? ALL_ICON_NAMES.filter((n) => n.includes(q)) : ICON_CHOICES),
    [q],
  );
  const shown = matches.slice(0, RESULT_CAP);

  if (matches.length === 0) {
    return <p className="mt-1.5 text-[11px] text-slate-600">No icons match “{query}”.</p>;
  }

  return (
    <div className="mt-1.5">
      <div className="flex flex-wrap gap-1">
        {shown.map((name) => {
          const Icon = iconByName(name);
          if (!Icon) return null;
          const active = current === name;
          return (
            <button
              key={name}
              type="button"
              title={name}
              aria-label={name}
              aria-pressed={active}
              onClick={() => onPick(name)}
              className={`flex h-7 w-7 items-center justify-center rounded border transition-colors ${
                active
                  ? "border-blue-500 bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-200"
                  : "border-slate-700 bg-[var(--c-surface)] text-slate-300 hover:border-slate-500"
              }`}
            >
              <Icon size={16} aria-hidden />
            </button>
          );
        })}
      </div>
      {matches.length > RESULT_CAP && (
        <p className="mt-1 text-[10px] text-slate-600">
          Showing {RESULT_CAP} of {matches.length} — keep typing to narrow it down.
        </p>
      )}
    </div>
  );
}
