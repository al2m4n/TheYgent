// Small display formatters shared by the Registries page and the global download toaster
// (lib/notify). Kept dependency-free so any surface can format a byte count / duration the same way.

import type { RunStatus } from "./runtypes";

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

// Status color: completed green, failed red, streaming amber, waiting violet (paused), created neutral.
const STATUS_CLASS: Record<RunStatus, string> = {
  completed: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/30",
  failed: "bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/30",
  streaming: "bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/30",
  waiting: "bg-violet-500/15 text-violet-700 dark:text-violet-300 border-violet-500/30",
  created: "bg-slate-500/15 text-slate-300 border-slate-500/30",
};

export function statusClass(status: string): string {
  return STATUS_CLASS[status as RunStatus] ?? STATUS_CLASS.created;
}

export function formatBytes(n: number | null | undefined): string {
  if (!n || n <= 0) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1000 && i < units.length - 1) {
    v /= 1000;
    i++;
  }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
}

// Compact a count for a tight tile: exact below 1,000, then K/M with one trimmed decimal
// (1234 → "1.2K", 12345 → "12K", 1_500_000 → "1.5M"). The full value is shown on hover via
// `exactCount` — so the abbreviation never hides the real number.
export function formatCount(n: number): string {
  if (!Number.isFinite(n)) return "—";
  const abs = Math.abs(n);
  if (abs < 1000) return String(n);
  const [value, suffix] = abs < 1_000_000 ? [n / 1000, "K"] : [n / 1_000_000, "M"];
  // One decimal below 10 (1.2K), whole numbers above (12K) — and drop a trailing ".0".
  const shown =
    Math.abs(value) < 10 ? value.toFixed(1).replace(/\.0$/, "") : String(Math.round(value));
  return `${shown}${suffix}`;
}

// The full value with locale thousands separators (1234 → "1,234"), for the hover title next to a
// `formatCount` abbreviation.
export function exactCount(n: number): string {
  return Number.isFinite(n) ? n.toLocaleString() : "—";
}

export function formatDuration(sec: number): string {
  if (sec < 60) return `${Math.round(sec)}s`;
  const m = Math.floor(sec / 60);
  return m < 60 ? `${m}m ${Math.round(sec % 60)}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}
