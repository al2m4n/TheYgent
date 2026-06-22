import type { RunStatus } from "./types";

export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const secs = Math.round((Date.now() - then) / 1000);
  if (secs < 0) return "just now";
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days}d ago`;
}

export function shortId(id: string, head = 8): string {
  return id.length <= head ? id : `${id.slice(0, head)}…`;
}

// Status color (M8 §1.1): completed green, failed red, streaming amber, created neutral.
const STATUS_CLASS: Record<RunStatus, string> = {
  completed: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  failed: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  streaming: "bg-amber-500/15 text-amber-300 border-amber-500/30",
  created: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};

export function statusClass(status: string): string {
  return STATUS_CLASS[status as RunStatus] ?? STATUS_CLASS.created;
}
