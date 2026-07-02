// The run waterfall — a real timing timeline for a run: each span a bar positioned by WHEN it ran
// and HOW long it took, the gaps between bars visible, worker attribution per span, and
// click-a-node → the I/O it received + sent. A positioned-div Gantt in the existing stack
// (React + Tailwind) — NO trace-viewer dependency (the UI reads theygent's own span store via
// /trace, never an external backend). Live bars grow from the /trace/stream SSE; the persisted
// /trace poll settles them.

import { useEffect, useMemo, useState } from "react";
import { streamGet } from "../lib/api";
import { NodeIcon, defaultIconFor } from "../lib/icons";
import type { Span } from "../lib/runtypes";
import { useNodeIo, useTrace } from "../queries";
import { IoDrawer } from "./io-drawer";
import { Badge, Card, Empty, SectionHeading, Spinner } from "./ui";

function nowNs(): number {
  return Date.now() * 1e6;
}

function ms(ns: number): string {
  const m = ns / 1e6;
  if (m < 1) return `${(ns / 1e3).toFixed(0)}µs`;
  if (m < 1000) return `${m.toFixed(m < 10 ? 1 : 0)}ms`;
  return `${(m / 1000).toFixed(2)}s`;
}

function bytes(n: number | null | undefined): string | null {
  if (n == null) return null;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// Merge the persisted spans (polled /trace) with the live ones (the /trace/stream overlay). A
// persisted span that has closed (end_ns set) is authoritative; otherwise the live one (in-flight)
// shows. Keyed by the deterministic span id, so a replay/re-emit never double-counts.
function mergeSpans(persisted: Span[], live: Map<string, Span>): Span[] {
  const byId = new Map<string, Span>();
  for (const s of live.values()) byId.set(s.id, s);
  for (const s of persisted) {
    const existing = byId.get(s.id);
    // Prefer the persisted row once it has closed; keep a live in-flight bar until then.
    if (!existing || s.end_ns != null) byId.set(s.id, s);
  }
  return [...byId.values()].sort((a, b) => a.seq - b.seq || a.start_ns - b.start_ns);
}

function statusColor(s: Span): string {
  if (s.end_ns == null) return "bg-amber-500/70 animate-pulse"; // in-flight
  if (s.phase) return "bg-violet-500/60"; // a phase band (queue.wait / model.generate)
  switch (s.status) {
    case "ok":
      return "bg-emerald-500/70";
    case "err":
      return "bg-rose-500/70";
    case "skipped":
      return "bg-slate-600/50";
    default:
      return "bg-amber-500/70";
  }
}

export function Waterfall({ runId, isLive }: { runId: string; isLive: boolean }) {
  const { data: persisted, isLoading } = useTrace(runId, { live: isLive });
  const [live, setLive] = useState<Map<string, Span>>(new Map());
  const [selected, setSelected] = useState<string | null>(null);

  // Live overlay: subscribe to /trace/stream while the run is live; merge span open/close events.
  useEffect(() => {
    if (!isLive) return;
    let cancelled = false;
    let abort: (() => void) | null = null;
    (async () => {
      try {
        const handle = await streamGet(`/runs/${runId}/trace/stream`);
        abort = handle.abort;
        for await (const ev of handle.events) {
          if (cancelled) return;
          if (ev.event === "done") return;
          if (ev.event !== "span.open" && ev.event !== "span.close") continue;
          try {
            const span = JSON.parse(ev.data) as Span;
            setLive((prev) => new Map(prev).set(span.id, span));
          } catch {
            /* ignore a malformed frame */
          }
        }
      } catch {
        /* stream unavailable (run already done, etc.) — the polled /trace covers it */
      }
    })();
    return () => {
      cancelled = true;
      abort?.();
    };
  }, [runId, isLive]);

  const spans = useMemo(() => mergeSpans(persisted ?? [], live), [persisted, live]);

  const { t0, span: total } = useMemo(() => {
    if (spans.length === 0) return { t0: 0, span: 1 };
    const start = Math.min(...spans.map((s) => s.start_ns));
    const end = Math.max(...spans.map((s) => s.end_ns ?? nowNs()));
    return { t0: start, span: Math.max(end - start, 1) };
  }, [spans]);

  const depthOf = useMemo(() => {
    const byId = new Map(spans.map((s) => [s.span_id, s]));
    const cache = new Map<string, number>();
    const walk = (s: Span): number => {
      if (cache.has(s.id)) return cache.get(s.id) as number;
      const parent = s.parent_span_id ? byId.get(s.parent_span_id) : undefined;
      const d = parent ? walk(parent) + 1 : 0;
      cache.set(s.id, d);
      return d;
    };
    return (s: Span) => walk(s);
  }, [spans]);

  // Worker attribution: how many distinct workers handled this run (a crash-resumed run hops).
  const workers = useMemo(
    () => [...new Set(spans.map((s) => s.executor_id).filter(Boolean))] as string[],
    [spans],
  );

  // Gap bands: within each parent, the empty wall-clock between one child ending and the next
  // starting (queue wait, MCP/engine spawn, transition). Rendered as a hatched band leading into the
  // next bar so the timeline reads as ONE continuous flow instead of scattered bars. Keyed by the
  // span the gap precedes.
  const gapBefore = useMemo(() => {
    const groups = new Map<string, Span[]>();
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
  }, [spans, t0, total]);

  if (isLoading && spans.length === 0) return <Spinner label="Loading trace…" />;
  if (spans.length === 0) return <Empty>No trace recorded for this run.</Empty>;

  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <SectionHeading>Run waterfall</SectionHeading>
        {workers.length > 0 && (
          <div className="flex items-center gap-1 text-xs text-slate-500">
            <span>handled by</span>
            {workers.map((w) => (
              <Badge key={w} tone={workers.length > 1 ? "green" : "slate"}>
                {w}
              </Badge>
            ))}
          </div>
        )}
      </div>
      <Card className="divide-y divide-slate-800/60">
        {spans.map((s) => {
          const end = s.end_ns ?? nowNs();
          const left = ((s.start_ns - t0) / total) * 100;
          const width = Math.max(((end - s.start_ns) / total) * 100, 0.6);
          const dur = end - s.start_ns;
          const depth = depthOf(s);
          const isNode = !!s.node_id && !s.phase;
          const tps = tokensPerSec(s, dur);
          const gap = gapBefore.get(s.id);
          const rowContent = (
            <>
              <div
                key="label"
                className="flex items-center gap-1.5 truncate"
                style={{ paddingLeft: depth * 12 }}
              >
                {s.phase ? (
                  <span>•</span>
                ) : (
                  <NodeIcon
                    name={defaultIconFor(s.node_type ?? "")}
                    size={13}
                    className="shrink-0 text-slate-400"
                  />
                )}
                <span
                  className={`mono truncate ${
                    s.phase ? "text-violet-700 dark:text-violet-300" : "text-slate-200"
                  }`}
                >
                  {s.name}
                </span>
                {s.executor_id && s.executor_id !== "inproc" && (
                  <span className="mono shrink-0 text-[10px] text-slate-500">@{s.executor_id}</span>
                )}
              </div>
              <div key="track" className="relative h-4">
                {/* The shared time-axis rail — every row sits on it, so the bars read as one
                    continuous timeline rather than scattered. */}
                <div className="absolute inset-x-0 top-[7px] h-px bg-slate-800/70" />
                {/* The gap leading into this bar (queue wait / engine or MCP spawn / transition),
                    hatched, bridging the previous step's end to this one's start. The hatch rides
                    the slate CSS variable so it inverts with the theme like the rail around it. */}
                {gap && gap.width > 0.3 && (
                  <div
                    className="absolute top-1 h-2 rounded-sm border-y border-slate-600/30"
                    style={{
                      left: `${gap.left}%`,
                      width: `${gap.width}%`,
                      backgroundImage:
                        "repeating-linear-gradient(45deg, color-mix(in srgb, var(--color-slate-400) 22%, transparent) 0 3px, transparent 3px 7px)",
                    }}
                    title={`gap · ${ms(gap.ns)} (queue wait / engine or MCP spawn / transition)`}
                  />
                )}
                <div
                  className={`absolute top-0.5 h-3 rounded-sm ${statusColor(s)}`}
                  style={{ left: `${left}%`, width: `${width}%` }}
                  title={`${s.name} · ${ms(dur)}${s.error ? ` · ${s.error}` : ""}`}
                />
                <span
                  className="absolute top-0 whitespace-nowrap text-[10px] text-slate-500"
                  style={{ left: `clamp(0%, ${left + width}% + 4px, 88%)` }}
                >
                  {ms(dur)}
                  {tps ? ` · ${tps}` : ""}
                  {s.bytes_out != null ? ` · → ${bytes(s.bytes_out)}` : ""}
                </span>
              </div>
            </>
          );
          const rowClass =
            "grid w-full grid-cols-[minmax(11rem,18rem)_1fr] items-center gap-2 px-3 py-1.5 text-left text-xs";
          // Only node rows are clickable — phase/gap rows render as plain divs so they stay out of
          // the tab order and never announce as actionable to assistive tech.
          return isNode ? (
            <button
              type="button"
              key={s.id}
              onClick={() => setSelected(s.node_id)}
              className={`${rowClass} hover:bg-slate-800/40 ${
                selected && s.node_id === selected ? "bg-slate-800/60" : ""
              }`}
            >
              {rowContent}
            </button>
          ) : (
            <div key={s.id} className={`${rowClass} cursor-default`}>
              {rowContent}
            </div>
          );
        })}
      </Card>
      <p className="text-xs text-slate-600">
        Bars positioned by start time; width = duration; sitting on one shared time axis. Hatched
        bands are gaps between steps (queue wait, engine/MCP spawn, transition); violet bars are
        instrumented phases. Click a node to see the input it received and the output it sent.
      </p>
      {selected && (
        <RunIoDrawer
          runId={runId}
          nodeId={selected}
          onClose={() => setSelected(null)}
          spans={spans}
        />
      )}
    </section>
  );
}

function tokensPerSec(s: Span, durNs: number): string | null {
  const out = s.attributes?.["gen_ai.usage.output_tokens"];
  if (typeof out === "number" && durNs > 0) {
    const tps = out / (durNs / 1e9);
    return `${tps.toFixed(0)} tok/s`;
  }
  return null;
}

// Owns this waterfall's lazy per-node I/O fetch and derives the drawer's node summary from the
// span shape; the drawer itself (layout + capture-gating states) is shared with the bench waterfall.
function RunIoDrawer({
  runId,
  nodeId,
  onClose,
  spans,
}: {
  runId: string;
  nodeId: string;
  onClose: () => void;
  spans: Span[];
}) {
  const { data, isLoading } = useNodeIo(runId, nodeId);
  const node = spans.find((s) => s.node_id === nodeId && !s.phase);
  return (
    <IoDrawer
      nodeId={nodeId}
      onClose={onClose}
      node={
        node
          ? {
              status: node.status,
              executor_id: node.executor_id,
              model: node.attributes?.["gen_ai.request.model"],
              ttft: node.attributes?.ttft_ms,
            }
          : null
      }
      io={{ data, isLoading }}
    />
  );
}
