import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

/** One headline number on the AI Gateway page: a named region, so it reads as one unit. */
export function StatTile({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: LucideIcon;
  label: string;
  value: ReactNode;
  hint: ReactNode;
}) {
  return (
    <section aria-label={label} className="rounded-lg border bg-card p-3">
      <div className="flex items-center justify-between text-muted-foreground">
        <span className="text-[11px] font-medium uppercase tracking-wide">{label}</span>
        <Icon size={15} className="opacity-70" />
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-foreground">{value}</div>
      <div className="text-[11px] text-muted-foreground">{hint}</div>
    </section>
  );
}
