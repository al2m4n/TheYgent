// Pure span math for the run waterfall: the parent/child tree walk, the live-overlay merge, the
// sibling-gap detection, and the tiny formatters (durations, byte sizes, token rates). No React,
// no fetching — every waterfall surface (bench modal, run detail, chat trace) shares these.

// The tolerant span shape the waterfall renders. Structurally a subset of the /runs/{id}/trace
// wire span (lib/runtypes Span is assignable), with only the fields a row actually needs required —
// fixtures and partial live frames stay light.
export interface WaterfallSpan {
  id: string;
  span_id?: string;
  parent_span_id?: string | null;
  node_id?: string | null;
  node_type?: string | null;
  name: string;
  phase?: string | null;
  status: string;
  start_ns: number;
  end_ns?: number | null;
  attributes?: Record<string, unknown> | null;
  error?: string | null;
  executor_id?: string | null;
  seq?: number;
  bytes_in?: number | null;
  bytes_out?: number | null;
}

export function nowNs(): number {
  return Date.now() * 1e6;
}

export function ms(ns: number): string {
  const m = ns / 1e6;
  if (m < 1) return `${(ns / 1e3).toFixed(0)}µs`;
  if (m < 1000) return `${m.toFixed(m < 10 ? 1 : 0)}ms`;
  return `${(m / 1000).toFixed(2)}s`;
}

export function bytes(n: number | null | undefined): string | null {
  if (n == null) return null;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function attrStr(s: WaterfallSpan | null | undefined, key: string): string | null {
  const v = s?.attributes?.[key];
  return typeof v === "string" ? v : null;
}

export function attrNum(s: WaterfallSpan | null | undefined, key: string): number | null {
  const v = s?.attributes?.[key];
  return typeof v === "number" ? v : null;
}

export function tokensPerSec(s: WaterfallSpan, durNs: number): string | null {
  const out = attrNum(s, "gen_ai.usage.output_tokens");
  if (out != null && durNs > 0) return `${(out / (durNs / 1e9)).toFixed(0)} tok/s`;
  return null;
}

// A node row is the click-to-inspect / canvas-flash unit; phase rows (model.generate, tool.*)
// carry the node_id of their parent but are not nodes themselves.
export function isNodeSpan(s: WaterfallSpan): boolean {
  return Boolean(s.node_id) && !s.phase;
}

// Status → bar colour. An in-flight bar (no end yet) pulses amber; a phase band is violet; a
// closed node bar takes its outcome tone (ok/err/skipped).
export function barColor(s: WaterfallSpan): string {
  if (s.end_ns == null) return "bg-amber-500/70 animate-pulse";
  if (s.phase) return "bg-violet-500/60";
  switch (s.status) {
    case "ok":
      return "bg-emerald-500/70";
    case "err":
      return "bg-rose-500/70";
    case "skipped":
      return "bg-muted-foreground/40";
    default:
      return "bg-amber-500/70";
  }
}

// Merge the persisted spans (polled /trace) with the live ones (the /trace/stream overlay). A
// persisted span that has closed (end_ns set) is authoritative; otherwise the live one (in-flight)
// shows. Keyed by the deterministic span id, so a replay/re-emit never double-counts.
export function mergeSpans(
  persisted: WaterfallSpan[],
  live: Map<string, WaterfallSpan>,
): WaterfallSpan[] {
  const byId = new Map<string, WaterfallSpan>();
  for (const s of live.values()) byId.set(s.id, s);
  for (const s of persisted) {
    const existing = byId.get(s.id);
    if (!existing || s.end_ns != null) byId.set(s.id, s);
  }
  return [...byId.values()];
}

export interface SpanRow {
  span: WaterfallSpan;
  depth: number;
  hasChildren: boolean;
}

// Pre-order DFS over the parent_span_id tree (children sorted by start time, seq as tiebreak) →
// the visible row list. Any span whose span_id is in `collapsed` keeps its own row but prunes its
// subtree. Spans with no in-set parent are roots (normally just the run-root span). Parent always
// precedes its children, so indentation reads as real nesting regardless of arrival order.
export function buildRows(spans: WaterfallSpan[], collapsed: Set<string>): SpanRow[] {
  const byId = new Map<string, WaterfallSpan>();
  for (const s of spans) if (s.span_id) byId.set(s.span_id, s);
  const children = new Map<string, WaterfallSpan[]>();
  const roots: WaterfallSpan[] = [];
  for (const s of spans) {
    const parent = s.parent_span_id && byId.has(s.parent_span_id) ? s.parent_span_id : null;
    if (parent) {
      const g = children.get(parent);
      if (g) g.push(s);
      else children.set(parent, [s]);
    } else {
      roots.push(s);
    }
  }
  const byStart = (a: WaterfallSpan, b: WaterfallSpan) =>
    a.start_ns - b.start_ns || (a.seq ?? 0) - (b.seq ?? 0);
  const rows: SpanRow[] = [];
  const visit = (s: WaterfallSpan, depth: number) => {
    const kids = (s.span_id ? children.get(s.span_id) : undefined) ?? [];
    rows.push({ span: s, depth, hasChildren: kids.length > 0 });
    if (s.span_id && collapsed.has(s.span_id)) return;
    for (const k of [...kids].sort(byStart)) visit(k, depth + 1);
  };
  for (const r of [...roots].sort(byStart)) visit(r, 0);
  return rows;
}

// Gap bands: within each parent, the empty wall-clock between one child ending and the next
// starting (queue wait, MCP/engine spawn, transition). Rendered as a hatched band leading into the
// next bar so the timeline reads as ONE continuous flow. Keyed by the span the gap precedes;
// left/width are percentages of the [t0, t0+total] axis.
export function computeGaps(
  spans: WaterfallSpan[],
  t0: number,
  total: number,
): Map<string, { left: number; width: number; ns: number }> {
  const groups = new Map<string, WaterfallSpan[]>();
  for (const s of spans) {
    const key = s.parent_span_id ?? "__root__";
    const g = groups.get(key);
    if (g) g.push(s);
    else groups.set(key, [s]);
  }
  const out = new Map<string, { left: number; width: number; ns: number }>();
  for (const group of groups.values()) {
    const sorted = [...group].sort((a, b) => a.start_ns - b.start_ns);
    let maxEnd = Number.NEGATIVE_INFINITY;
    for (const s of sorted) {
      if (maxEnd !== Number.NEGATIVE_INFINITY && s.start_ns > maxEnd) {
        out.set(s.id, {
          left: ((maxEnd - t0) / total) * 100,
          width: ((s.start_ns - maxEnd) / total) * 100,
          ns: s.start_ns - maxEnd,
        });
      }
      maxEnd = Math.max(maxEnd, s.end_ns ?? s.start_ns);
    }
  }
  return out;
}

// Sum a numeric usage attribute over a set of spans (the per-node / per-run token totals — usage
// lands only on model.generate phase spans, so aggregates must sum, never read the node span).
export function sumAttr(spans: WaterfallSpan[], key: string): number | null {
  let sum = 0;
  let seen = false;
  for (const s of spans) {
    const v = attrNum(s, key);
    if (v != null) {
      sum += v;
      seen = true;
    }
  }
  return seen ? sum : null;
}
