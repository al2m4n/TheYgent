// A time-window filter for list pages: either "all time", a rolling relative window (last N), or an
// absolute from/to window. Kept UI-agnostic here — the picker component renders it, and each page
// filters its own rows with `inRange` over whichever timestamp field it cares about (a run's
// created_at, a session's last_activity). Client-side, over the already-loaded rows, matching how
// search and the status chips already filter.

export type TimeRange =
  | { type: "all" }
  | { type: "relative"; ms: number }
  | { type: "absolute"; fromMs: number | null; toMs: number | null };

export const ALL_TIME: TimeRange = { type: "all" };

export interface QuickRange {
  label: string;
  ms: number;
}

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

// The rolling windows the picker offers, shortest first (the lifecycle-of-attention order operators
// reach for). Add one here and it shows up in the picker with no other change.
export const QUICK_RANGES: QuickRange[] = [
  { label: "Last 5 minutes", ms: 5 * MIN },
  { label: "Last 15 minutes", ms: 15 * MIN },
  { label: "Last 30 minutes", ms: 30 * MIN },
  { label: "Last 1 hour", ms: HOUR },
  { label: "Last 3 hours", ms: 3 * HOUR },
  { label: "Last 6 hours", ms: 6 * HOUR },
  { label: "Last 12 hours", ms: 12 * HOUR },
  { label: "Last 24 hours", ms: DAY },
  { label: "Last 2 days", ms: 2 * DAY },
  { label: "Last 7 days", ms: 7 * DAY },
  { label: "Last 30 days", ms: 30 * DAY },
  { label: "Last 90 days", ms: 90 * DAY },
];

export function isActive(r: TimeRange): boolean {
  return r.type !== "all";
}

// Resolve to concrete inclusive bounds at a given "now". A relative window has an open upper bound
// (our timestamps are always in the past, so pinning `to` to now would only add rounding surprises).
export function resolveBounds(
  r: TimeRange,
  nowMs: number,
): { fromMs: number | null; toMs: number | null } {
  switch (r.type) {
    case "all":
      return { fromMs: null, toMs: null };
    case "relative":
      return { fromMs: nowMs - r.ms, toMs: null };
    case "absolute":
      return { fromMs: r.fromMs, toMs: r.toMs };
  }
}

// Does an ISO timestamp fall inside the window? A missing/unparseable timestamp is excluded whenever
// a window is active (it can't be placed on the timeline), and always included under "all time".
export function inRange(iso: string | null | undefined, r: TimeRange, nowMs: number): boolean {
  if (r.type === "all") return true;
  if (!iso) return false;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return false;
  const { fromMs, toMs } = resolveBounds(r, nowMs);
  if (fromMs != null && t < fromMs) return false;
  if (toMs != null && t > toMs) return false;
  return true;
}

// datetime-local <input> values are wall-clock strings in the viewer's own zone — round-trip them
// through the local Date so the window means what the operator typed.
export function toLocalInput(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function fromLocalInput(s: string): number | null {
  if (!s) return null;
  const ms = new Date(s).getTime();
  return Number.isNaN(ms) ? null : ms;
}

function formatMoment(ms: number): string {
  return new Date(ms).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

// The short label shown on the picker trigger.
export function rangeLabel(r: TimeRange): string {
  switch (r.type) {
    case "all":
      return "All time";
    case "relative": {
      const q = QUICK_RANGES.find((x) => x.ms === r.ms);
      return q ? q.label : `Last ${Math.round(r.ms / MIN)}m`;
    }
    case "absolute": {
      if (r.fromMs == null && r.toMs == null) return "All time";
      const from = r.fromMs != null ? formatMoment(r.fromMs) : "Earliest";
      const to = r.toMs != null ? formatMoment(r.toMs) : "Now";
      return `${from} → ${to}`;
    }
  }
}
