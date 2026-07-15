// A two-way segmented switch between the grid and list renderings of a list page, meant to sit in a
// FilterBar's trailing cluster. Plain <button>s (not toggle-group triggers) so it matches the other
// segmented switches in the app and stays addressable as buttons in tests. The pressed side is
// exposed via aria-pressed, not just colour.

import { LayoutGrid, List } from "lucide-react";
import { cn } from "../lib/utils";
import type { ViewMode } from "../lib/viewMode";

const OPTIONS: { value: ViewMode; label: string; Icon: typeof LayoutGrid }[] = [
  { value: "grid", label: "Grid", Icon: LayoutGrid },
  { value: "list", label: "List", Icon: List },
];

export function ViewToggle({
  value,
  onChange,
  className,
}: {
  value: ViewMode;
  onChange: (v: ViewMode) => void;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "inline-flex h-7 items-center justify-center rounded-lg bg-muted p-[3px] text-muted-foreground",
        className,
      )}
    >
      {OPTIONS.map(({ value: v, label, Icon }) => {
        const active = value === v;
        return (
          <button
            key={v}
            type="button"
            aria-pressed={active}
            title={`${label} view`}
            onClick={() => onChange(v)}
            className={cn(
              "inline-flex h-full items-center justify-center gap-1.5 rounded-md border border-transparent px-2 text-xs font-medium whitespace-nowrap transition-all",
              active
                ? "bg-background text-foreground shadow-sm dark:border-input dark:bg-input/30"
                : "text-foreground/60 hover:text-foreground dark:text-muted-foreground dark:hover:text-foreground",
            )}
          >
            <Icon size={14} />
            <span className="sr-only sm:not-sr-only">{label}</span>
          </button>
        );
      })}
    </div>
  );
}
