// Rich run waterfall (interface bench) — replaces the flat per-node bar list with the real span
// TREE: run → node → phase (model.generate, tool.<name>), built from `parent_span_id`. It adds the
// two things the cockpit's M17 waterfall lacks — collapse/expand a node's phases, and zoom the time
// axis — plus a click-to-inspect I/O drawer (the per-step CONTEXT: input received, output sent,
// capture state) ported from apps/web. Hovering a node row still flashes the matching canvas node
// (the span.node_id == node.id join). Reads theygent's own /trace + /nodes/{id}/io — NO external
// trace-viewer dependency; the bars are positioned divs on one shared, zoomable time axis.

import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useMemo, useState } from "react";
import type { Selection } from "../adapter";
import { Badge, Card, Spinner } from "../components/ui";
import { type TraceSpan, api } from "../lib/api";
import { defaultIconFor } from "../lib/icons";

const LABEL_W = "13rem";
const MIN_ZOOM = 1;
const MAX_ZOOM = 16;

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

// Status → bar colour. An in-flight bar (no end yet) pulses amber; a phase band is violet; a closed
// node bar takes its outcome tone (ok/err/skipped). Mirrors the cockpit's statusColor.
function barColor(s: TraceSpan): string {
  if (s.end_ns == null) return "bg-amber-500/70 animate-pulse";
  if (s.phase) return "bg-violet-500/60";
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

interface Row {
  span: TraceSpan;
  depth: number;
  hasChildren: boolean;
}

// Pre-order DFS over the parent_span_id tree (children sorted by start time) → the visible row list.
// Any node whose span_id is in `collapsed` keeps its own row but prunes its subtree. Spans with no
// in-set parent are roots (normally just the run-root span). Parent always precedes its children, so
// indentation reads as real nesting regardless of the API's `seq` ordering.
function buildRows(spans: TraceSpan[], collapsed: Set<string>): Row[] {
  const byId = new Map<string, TraceSpan>();
  for (const s of spans) if (s.span_id) byId.set(s.span_id, s);
  const children = new Map<string, TraceSpan[]>();
  const roots: TraceSpan[] = [];
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
  const byStart = (a: TraceSpan, b: TraceSpan) => a.start_ns - b.start_ns;
  const rows: Row[] = [];
  const visit = (s: TraceSpan, depth: number) => {
    const kids = (s.span_id ? children.get(s.span_id) : undefined) ?? [];
    rows.push({ span: s, depth, hasChildren: kids.length > 0 });
    if (s.span_id && collapsed.has(s.span_id)) return;
    for (const k of [...kids].sort(byStart)) visit(k, depth + 1);
  };
  for (const r of [...roots].sort(byStart)) visit(r, 0);
  return rows;
}

export function TraceWaterfall({
  runId,
  onHover,
}: {
  runId: string;
  onHover?: (s: Selection) => void;
}) {
  const trace = useQuery({
    queryKey: ["trace", runId],
    queryFn: () => api.getRunTrace(runId),
    enabled: Boolean(runId),
    retry: false, // observability may be absent → degrade, don't hammer
  });
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [selected, setSelected] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);

  const spans = useMemo(() => trace.data?.spans ?? [], [trace.data]);
  const rows = useMemo(() => buildRows(spans, collapsed), [spans, collapsed]);
  const { t0, total } = useMemo(() => {
    if (spans.length === 0) return { t0: 0, total: 1 };
    const start = Math.min(...spans.map((s) => s.start_ns));
    const end = Math.max(...spans.map((s) => s.end_ns ?? nowNs()));
    return { t0: start, total: Math.max(end - start, 1) };
  }, [spans]);

  if (trace.isLoading) return <Spinner label="Loading trace…" />;
  if (trace.isError || spans.length === 0)
    return (
      <p className="text-xs text-slate-500">
        Trace unavailable (observability not enabled) — showing persisted output only.
      </p>
    );

  const workers = [...new Set(spans.map((s) => s.executor_id).filter(Boolean))] as string[];
  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="space-y-2" data-testid="agent-waterfall">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-slate-200">Run waterfall</span>
        <div className="flex items-center gap-2">
          {workers.length > 1 && <Badge tone="green">{workers.length} workers</Badge>}
          <div className="flex items-center gap-1">
            <ZoomButton
              label="Zoom out"
              onClick={() => setZoom((z) => Math.max(MIN_ZOOM, z / 1.5))}
            >
              −
            </ZoomButton>
            <span className="mono w-9 text-center text-[11px] tabular-nums text-slate-400">
              {zoom.toFixed(1)}×
            </span>
            <ZoomButton label="Zoom in" onClick={() => setZoom((z) => Math.min(MAX_ZOOM, z * 1.5))}>
              +
            </ZoomButton>
            <ZoomButton label="Reset zoom" onClick={() => setZoom(1)} disabled={zoom === 1}>
              fit
            </ZoomButton>
          </div>
        </div>
      </div>

      <Card className="overflow-hidden p-0">
        <div className="overflow-x-auto">
          <div style={{ width: `${zoom * 100}%` }} className="min-w-full">
            {/* time ruler — tick labels scale with the timeline as you zoom */}
            <div className="flex border-b border-slate-800/60 text-[10px] text-slate-500">
              <div
                className="sticky left-0 z-10 shrink-0 bg-[#11161f]"
                style={{ width: LABEL_W }}
              />
              <div className="relative h-5 flex-1">
                {[0, 0.25, 0.5, 0.75, 1].map((f) => (
                  <span
                    key={f}
                    className="absolute top-1 -translate-x-1/2 whitespace-nowrap"
                    style={{ left: `clamp(1.5rem, ${f * 100}%, calc(100% - 1.5rem))` }}
                  >
                    {ms(total * f)}
                  </span>
                ))}
              </div>
            </div>

            {rows.map(({ span: s, depth, hasChildren }) => {
              const isNode = Boolean(s.node_id) && !s.phase;
              const end = s.end_ns ?? nowNs();
              const left = ((s.start_ns - t0) / total) * 100;
              // Clamp to the track: an in-flight bar reads `now` at render, slightly after the
              // memoized `total` (computed with an earlier `now`), which would push it past 100%
              // and overflow the row. Bound width to what's left of the track.
              const width = Math.max(0.5, Math.min(((end - s.start_ns) / total) * 100, 100 - left));
              const dur = end - s.start_ns;
              const isCollapsed = s.span_id ? collapsed.has(s.span_id) : false;
              const isSel = isNode && s.node_id === selected;
              const toolOk = s.phase?.startsWith("tool.") ? s.attributes?.["tool.ok"] : undefined;
              const model = s.attributes?.["gen_ai.request.model"];
              return (
                <div
                  key={s.id}
                  className={`flex items-stretch border-b border-l-2 border-slate-800/40 ${
                    isSel ? "border-l-blue-500" : "border-l-transparent"
                  }`}
                >
                  <div
                    className={`sticky left-0 z-10 flex shrink-0 items-center gap-1 py-1.5 pr-2 ${
                      isSel ? "bg-[#16202e]" : "bg-[#11161f]"
                    }`}
                    style={{ width: LABEL_W, paddingLeft: 6 + depth * 14 }}
                  >
                    {hasChildren ? (
                      <button
                        type="button"
                        onClick={() => s.span_id && toggle(s.span_id)}
                        className="w-3 shrink-0 text-[10px] text-slate-500 hover:text-slate-200"
                        aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${s.phase ?? s.name}`}
                      >
                        {isCollapsed ? "▸" : "▾"}
                      </button>
                    ) : (
                      <span className="w-3 shrink-0" />
                    )}
                    <span className="shrink-0 text-xs leading-none">
                      {s.phase ? "•" : s.node_id ? defaultIconFor(s.node_type ?? "") : "◆"}
                    </span>
                    <button
                      type="button"
                      disabled={!isNode}
                      onClick={() => isNode && s.node_id && setSelected(s.node_id)}
                      onMouseEnter={() =>
                        isNode && s.node_id && onHover?.({ kind: "node", id: s.node_id })
                      }
                      onMouseLeave={() => onHover?.(null)}
                      className={`mono truncate text-left text-xs ${
                        s.phase
                          ? "text-violet-300"
                          : isNode
                            ? "cursor-pointer text-slate-200 hover:text-white"
                            : "cursor-default text-slate-400"
                      }`}
                      title={isNode ? "Click to inspect input / output" : s.name}
                    >
                      {s.phase ?? s.name}
                    </button>
                  </div>
                  <div className="relative h-7 flex-1">
                    <div className="absolute inset-x-0 top-1/2 h-px bg-slate-800/60" />
                    <div
                      className={`absolute top-2 h-3 rounded-sm ${barColor(s)}`}
                      style={{ left: `${left}%`, width: `${width}%` }}
                      title={`${s.name} · ${ms(dur)}${s.error ? ` · ${s.error}` : ""}`}
                    />
                    <span
                      className="absolute top-1 whitespace-nowrap text-[10px] text-slate-500"
                      style={{ left: `clamp(0%, calc(${left + width}% + 4px), 86%)` }}
                    >
                      {ms(dur)}
                      {toolOk === true ? " · ✓" : toolOk === false ? " · ✗" : ""}
                      {typeof model === "string" ? ` · ${model}` : ""}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </Card>

      <p className="text-[11px] leading-relaxed text-slate-600">
        Bars sit on one shared time axis (start = position, width = duration). Violet = an
        instrumented phase (model.generate, tool calls); <span className="text-slate-400">▾</span>{" "}
        collapses a node's phases; click a node to inspect the input it received and the output it
        sent.
      </p>

      {selected && (
        <IoDrawer runId={runId} nodeId={selected} spans={spans} onClose={() => setSelected(null)} />
      )}
    </div>
  );
}

function ZoomButton({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      className="rounded border border-slate-700 px-1.5 text-[12px] text-slate-300 hover:bg-[#1d2433] disabled:cursor-not-allowed disabled:opacity-40"
    >
      {children}
    </button>
  );
}

// The per-step context drawer: the gated input/output a node received + sent. Lazy — fetched only
// when a node row is clicked. Ported from the cockpit's IoDrawer; honours the capture-policy gating
// (a `reason` + level when payloads are withheld; a truncation note when over the byte cap).
function IoDrawer({
  runId,
  nodeId,
  spans,
  onClose,
}: {
  runId: string;
  nodeId: string;
  spans: TraceSpan[];
  onClose: () => void;
}) {
  const io = useQuery({
    queryKey: ["io", runId, nodeId],
    queryFn: () => api.getRunIo(runId, nodeId),
    enabled: Boolean(nodeId),
    retry: false,
  });
  const node = spans.find((s) => s.node_id === nodeId && !s.phase);
  const model = node?.attributes?.["gen_ai.request.model"];
  const ttft = node?.attributes?.ttft_ms;
  const data = io.data;

  return (
    <Card className="space-y-3 border-blue-500/30 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="mono text-sm font-semibold text-slate-100">{nodeId}</h3>
          <div className="flex flex-wrap items-center gap-2 pt-1 text-xs text-slate-400">
            {node && <Badge tone={node.status === "err" ? "red" : "green"}>{node.status}</Badge>}
            {typeof model === "string" && <span className="mono">{model}</span>}
            {typeof ttft === "number" && <span>ttft {ttft}ms</span>}
            {node?.executor_id && node.executor_id !== "inproc" && (
              <span className="mono">worker {node.executor_id}</span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close I/O"
          className="text-slate-500 hover:text-slate-300"
        >
          ✕
        </button>
      </div>

      {io.isLoading && <Spinner label="Loading I/O…" />}
      {data && (
        <>
          {data.reason && (
            <div className="rounded-md border border-slate-700 bg-slate-800/40 px-3 py-2 text-xs text-slate-400">
              {data.reason} (capture: {data.capture_level})
            </div>
          )}
          {data.truncated && (
            <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-300">
              Payload truncated to the capture cap (the byte counts are the true sizes).
            </div>
          )}
          <IoPane label="Input" data={data.inputs} sizeLabel={bytes(data.bytes_in)} />
          <IoPane label="Output" data={data.outputs} sizeLabel={bytes(data.bytes_out)} />
        </>
      )}
    </Card>
  );
}

function IoPane({
  label,
  data,
  sizeLabel,
}: {
  label: string;
  data: Record<string, unknown> | null;
  sizeLabel: string | null;
}) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
        <span>{label}</span>
        {sizeLabel && <span className="font-normal normal-case text-slate-600">{sizeLabel}</span>}
      </div>
      {data && Object.keys(data).length > 0 ? (
        Object.entries(data).map(([port, value]) => (
          <div key={port} className="space-y-0.5">
            <div className="mono text-[10px] text-blue-400">{port}</div>
            <pre className="mono max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border border-slate-800 bg-[#0b0e14] px-2 py-1 text-xs text-slate-200">
              {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
            </pre>
          </div>
        ))
      ) : (
        <p className="text-xs text-slate-600">—</p>
      )}
    </div>
  );
}
