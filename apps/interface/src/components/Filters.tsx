// Reusable table-filter UI, shared by every list page so filtering looks and behaves the same
// everywhere (the consistency rule). Two pieces:
//   • CategoryBadge — a colour-coded category pill. Clickable (a filter toggle) or static (a table
//     cell label). Its colour comes from the shared tone system (lib/categories), so the same
//     category reads the same colour in the table and in the filter bar.
//   • FilterBar — a search box + one chip group per facet, sitting on top of a table. Clicking a
//     chip toggles that value; the page owns the selection state and does the actual filtering.

import type { ReactNode } from "react";
import { type Tone, toneOf } from "../lib/categories";

export function CategoryBadge({
  tone = "slate",
  active = false,
  count,
  onClick,
  title,
  children,
}: {
  tone?: Tone;
  active?: boolean;
  count?: number;
  onClick?: () => void;
  title?: string;
  children: ReactNode;
}) {
  const t = toneOf(tone);
  const base =
    "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-medium transition-colors";
  const look = active
    ? `${base} ${t.activeChip}`
    : `${base} border-transparent bg-slate-800/70 text-slate-300 ${onClick ? "hover:bg-slate-700/70" : ""}`;
  const inner = (
    <>
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${t.dot}`} />
      <span className="truncate">{children}</span>
      {count != null && <span className="text-slate-500">{count}</span>}
    </>
  );
  if (onClick) {
    return (
      <button type="button" title={title} onClick={onClick} className={look}>
        {inner}
      </button>
    );
  }
  return (
    <span title={title} className={look}>
      {inner}
    </span>
  );
}

export interface FacetOption {
  value: string;
  label: string;
  tone?: Tone;
  count: number;
}

export interface Facet {
  label: string;
  options: FacetOption[];
  selected: string[];
  onToggle: (value: string) => void;
}

export function FilterBar({
  search,
  onSearch,
  searchPlaceholder = "Search…",
  facets = [],
  trailing,
  total,
  shown,
  onClear,
}: {
  search?: string;
  onSearch?: (value: string) => void;
  searchPlaceholder?: string;
  facets?: Facet[];
  /** Extra controls for the right cluster (e.g. a sort select). */
  trailing?: ReactNode;
  total?: number;
  shown?: number;
  onClear?: () => void;
}) {
  const hasActive = (search ?? "").trim() !== "" || facets.some((f) => f.selected.length > 0);

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border border-slate-800 bg-[var(--c-surface-2)] px-3 py-2">
      {onSearch && (
        <input
          value={search ?? ""}
          onChange={(e) => onSearch(e.target.value)}
          placeholder={searchPlaceholder}
          className="w-44 rounded-md border border-slate-700 bg-[var(--c-surface)] px-2.5 py-1 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-blue-500"
        />
      )}

      {facets
        .filter((f) => f.options.length > 0)
        .map((f) => (
          <div key={f.label} className="flex flex-wrap items-center gap-1.5">
            <span className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
              {f.label}
            </span>
            {f.options.map((o) => (
              <CategoryBadge
                key={o.value}
                tone={o.tone}
                count={o.count}
                active={f.selected.includes(o.value)}
                onClick={() => f.onToggle(o.value)}
              >
                {o.label}
              </CategoryBadge>
            ))}
          </div>
        ))}

      <div className="ml-auto flex items-center gap-3">
        {trailing}
        {hasActive && onClear && (
          <button
            type="button"
            onClick={onClear}
            className="text-xs text-slate-400 hover:text-slate-200"
          >
            Clear filters
          </button>
        )}
        {total != null && shown != null && (
          <span className="text-xs text-slate-500">
            {shown === total ? `${total}` : `${shown} of ${total}`}
          </span>
        )}
      </div>
    </div>
  );
}
