// The run waterfall — ONE timeline component for every surface that traces a run (the agent bench,
// the run detail page, the per-turn chat trace). Each span is a bar positioned by WHEN it ran and
// HOW long it took on one shared, zoomable time axis; the parent_span_id tree gives run → node →
// phase nesting with collapse; hatched bands make the gaps between steps visible; selecting ANY row
// embeds its context (compact stats + gated I/O + reasoning) inside the card — a side pane when the
// container is wide, stacked below when narrow. Zoom is the mouse wheel over the timeline (anchored
// at the cursor), not buttons. Positioned divs in the existing stack — NO trace-viewer dependency:
// the data is theygent's own /trace (+ the /trace/stream live overlay), never an external backend.

import { ChevronDown, ChevronRight } from "lucide-react";
import { useCallback, useLayoutEffect, useMemo, useRef, useState } from "react";
import { NodeIcon, defaultIconFor } from "../../lib/icons";
import { Badge, Card, Empty, Spinner } from "../ui";
import { SpanDetail } from "./SpanDetail";
import {
  attrStr,
  barColor,
  buildRows,
  computeGaps,
  isNodeSpan,
  ms,
  nowNs,
  tokensPerSec,
} from "./spans";
import { useRunSpans } from "./useRunSpans";

const MIN_ZOOM = 1;
const MAX_ZOOM = 32;

export function RunWaterfall({
  runId,
  isLive = false,
  onHoverNode,
  compact = false,
}: {
  runId: string;
  /** Poll /trace at 1s and overlay the /trace/stream SSE while the run is in flight. */
  isLive?: boolean;
  /** Node rows only (never phases) — the canvas-flash join on span.node_id. */
  onHoverNode?: (nodeId: string | null) => void;
  /** Tight chrome for embedded surfaces (chat turns): no card frame, slimmer label column. */
  compact?: boolean;
}) {
  const { spans, isLoading, isError } = useRunSpans(runId, isLive);
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [zoom, setZoom] = useState(1);
  const scrollerRef = useRef<HTMLDivElement>(null);
  const labelRef = useRef<HTMLDivElement>(null);
  // The scrollLeft that keeps the time under the cursor stationary across a zoom change — computed
  // in the wheel handler (pre-render), applied after the width actually changes.
  const pendingScroll = useRef<number | null>(null);

  const rows = useMemo(() => buildRows(spans, collapsed), [spans, collapsed]);
  const { t0, total } = useMemo(() => {
    if (spans.length === 0) return { t0: 0, total: 1 };
    const start = Math.min(...spans.map((s) => s.start_ns));
    const end = Math.max(...spans.map((s) => s.end_ns ?? nowNs()));
    return { t0: start, total: Math.max(end - start, 1) };
  }, [spans]);
  const gaps = useMemo(() => computeGaps(spans, t0, total), [spans, t0, total]);
  const workers = useMemo(
    () => [...new Set(spans.map((s) => s.executor_id).filter(Boolean))] as string[],
    [spans],
  );
  const selected = selectedId ? (spans.find((s) => s.id === selectedId) ?? null) : null;

  // Wheel-to-zoom. Native (non-passive) listener because React registers wheel passively and the
  // zoom must preventDefault to keep the page still. Only the timeline half zooms — the label
  // column (and everything outside it) keeps normal page scrolling; a mostly-horizontal wheel is
  // left to the scroller's own overflow-x pan. A callback ref (not an effect) so the listener
  // attaches whenever the scroller actually mounts — it isn't in the DOM during the loading state.
  const wheelCleanup = useRef<(() => void) | null>(null);
  const attachScroller = useCallback((el: HTMLDivElement | null) => {
    wheelCleanup.current?.();
    wheelCleanup.current = null;
    scrollerRef.current = el;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (!(e.target as HTMLElement | null)?.closest("[data-wf-track]")) return;
      if (Math.abs(e.deltaY) <= Math.abs(e.deltaX)) return;
      e.preventDefault();
      const labelW = labelRef.current?.offsetWidth ?? 0;
      const cursor = e.clientX - el.getBoundingClientRect().left;
      setZoom((prev) => {
        const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev * Math.exp(-e.deltaY * 0.002)));
        if (next !== prev && el.clientWidth > 0) {
          // Content x of the cursor, minus the un-scaled label column, rescaled to the new track
          // width, plus the label back — the instant under the cursor stays under the cursor.
          const trackX = Math.max(0, el.scrollLeft + cursor - labelW);
          const scale = (el.clientWidth * next - labelW) / (el.clientWidth * prev - labelW);
          pendingScroll.current = trackX * scale + labelW - cursor;
        }
        return next;
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    wheelCleanup.current = () => el.removeEventListener("wheel", onWheel);
  }, []);

  // The wheel handler computed the corrected scrollLeft against the NEXT zoom; it can only be
  // applied after that zoom's width actually commits — hence the zoom "dependency".
  // biome-ignore lint/correctness/useExhaustiveDependencies: zoom re-runs the deferred scroll apply
  useLayoutEffect(() => {
    if (pendingScroll.current != null && scrollerRef.current) {
      scrollerRef.current.scrollLeft = pendingScroll.current;
      pendingScroll.current = null;
    }
  }, [zoom]);

  if (isLoading && spans.length === 0) return <Spinner label="Loading trace…" />;
  if (isError || spans.length === 0)
    return (
      <Empty>
        Trace unavailable — observability may be off, or no spans were recorded for this run.
      </Empty>
    );

  const labelW = compact ? "9rem" : "13rem";
  const toggle = (id: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const frame = compact ? "overflow-hidden rounded-md border" : "";
  const body = (
    <>
      <div className="flex items-center justify-between gap-2 border-b px-2 py-1">
        <span className="text-xs font-medium text-foreground">Run waterfall</span>
        <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
          {workers.length > 1 && <Badge tone="green">{workers.length} workers</Badge>}
          <span className="tabular-nums" title="Scroll on the timeline to zoom">
            {zoom.toFixed(1)}×
          </span>
          <button
            type="button"
            aria-label="Reset zoom"
            disabled={zoom === 1}
            onClick={() => setZoom(1)}
            className="rounded border px-1.5 py-0.5 hover:bg-muted disabled:cursor-not-allowed disabled:opacity-40"
          >
            Fit
          </button>
        </div>
      </div>

      <div
        className={selected ? "grid @3xl:grid-cols-[minmax(0,1fr)_minmax(17rem,22rem)]" : undefined}
      >
        <div ref={attachScroller} className="min-w-0 overflow-x-auto">
          <div style={{ width: `${zoom * 100}%` }} className="min-w-full">
            {/* time ruler — tick labels scale with the timeline as you zoom */}
            <div className="flex border-b text-[10px] text-muted-foreground">
              <div
                ref={labelRef}
                className="sticky left-0 z-10 shrink-0"
                style={{ width: labelW }}
              />
              <div data-wf-track className="relative h-5 flex-1">
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
              const isNode = isNodeSpan(s);
              const end = s.end_ns ?? nowNs();
              const left = ((s.start_ns - t0) / total) * 100;
              // Clamp to the track: an in-flight bar reads `now` at render, slightly after the
              // memoized `total` (computed with an earlier `now`) — bound to what's left of it.
              const width = Math.max(0.5, Math.min(((end - s.start_ns) / total) * 100, 100 - left));
              const dur = end - s.start_ns;
              const isCollapsed = s.span_id ? collapsed.has(s.span_id) : false;
              const isSel = s.id === selectedId;
              const gap = gaps.get(s.id);
              const toolOk = s.phase?.startsWith("tool.") ? s.attributes?.["tool.ok"] : undefined;
              const model =
                s.phase === "model.generate" ? attrStr(s, "gen_ai.request.model") : null;
              const rate = s.phase === "model.generate" ? tokensPerSec(s, dur) : null;
              return (
                <div
                  key={s.id}
                  // biome-ignore lint/a11y/useSemanticElements: the row nests a real <button> (the fold control), which a <button> row could not
                  role="button"
                  tabIndex={0}
                  onClick={() => setSelectedId(isSel ? null : s.id)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      setSelectedId(isSel ? null : s.id);
                    }
                  }}
                  onMouseEnter={
                    isNode && s.node_id ? () => onHoverNode?.(s.node_id ?? null) : undefined
                  }
                  onMouseLeave={isNode ? () => onHoverNode?.(null) : undefined}
                  className={`flex cursor-pointer items-stretch border-b border-l-2 ${
                    isSel
                      ? "border-l-primary bg-muted/40"
                      : "border-l-transparent hover:bg-muted/25"
                  }`}
                >
                  <div
                    className={`sticky left-0 z-10 flex shrink-0 items-center gap-1 py-1.5 pr-2 ${
                      isSel ? "bg-muted" : "bg-card"
                    }`}
                    style={{ width: labelW, paddingLeft: 2 + depth * 14 }}
                  >
                    {hasChildren ? (
                      <button
                        type="button"
                        onClick={(e) => {
                          e.stopPropagation();
                          if (s.span_id) toggle(s.span_id);
                        }}
                        aria-label={`${isCollapsed ? "Expand" : "Collapse"} ${s.phase ?? s.name}`}
                        className="flex w-5 shrink-0 items-center justify-center self-stretch text-muted-foreground hover:text-foreground"
                      >
                        {isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                      </button>
                    ) : (
                      <span className="w-5 shrink-0" />
                    )}
                    <span className="flex w-3.5 shrink-0 items-center justify-center text-xs leading-none text-muted-foreground">
                      {s.phase ? (
                        "•"
                      ) : s.node_id ? (
                        <NodeIcon name={defaultIconFor(s.node_type ?? "")} size={13} />
                      ) : (
                        "◆"
                      )}
                    </span>
                    <span
                      // Show the human label (`name`) — a tool phase's `phase` carries a
                      // `#<iter>.<idx>` identity discriminator that reads as noise in the row.
                      title={s.name ?? s.phase}
                      className={`mono truncate text-left text-xs ${
                        s.phase
                          ? "text-violet-700 dark:text-violet-300"
                          : isNode
                            ? "text-foreground"
                            : "text-muted-foreground"
                      }`}
                    >
                      {s.name ?? s.phase}
                    </span>
                  </div>
                  <div data-wf-track className="relative h-7 flex-1">
                    <div className="absolute inset-x-0 top-1/2 h-px bg-border" />
                    {gap && gap.width > 0.3 && (
                      <div
                        className="absolute top-2 h-3 rounded-sm border-y border-border/60"
                        style={{
                          left: `${gap.left}%`,
                          width: `${gap.width}%`,
                          backgroundImage:
                            "repeating-linear-gradient(45deg, color-mix(in srgb, var(--color-muted-foreground) 25%, transparent) 0 3px, transparent 3px 7px)",
                        }}
                        title={`gap · ${ms(gap.ns)} (queue wait / engine or MCP spawn / transition)`}
                      />
                    )}
                    <div
                      className={`absolute top-2 h-3 rounded-sm ${barColor(s)}`}
                      style={{ left: `${left}%`, width: `${width}%` }}
                      title={`${s.name} · ${ms(dur)}${s.error ? ` · ${s.error}` : ""}`}
                    />
                    <span
                      className="absolute top-1 whitespace-nowrap text-[10px] text-muted-foreground"
                      style={{ left: `clamp(0%, calc(${left + width}% + 4px), 86%)` }}
                    >
                      {ms(dur)}
                      {toolOk === true ? " · ✓" : toolOk === false ? " · ✗" : ""}
                      {model ? ` · ${model}` : ""}
                      {rate ? ` · ${rate}` : ""}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {selected && (
          <div className="min-w-0 border-t @3xl:border-l @3xl:border-t-0">
            <SpanDetail
              key={selected.id}
              runId={runId}
              span={selected}
              spans={spans}
              onClose={() => setSelectedId(null)}
              compact={compact}
            />
          </div>
        )}
      </div>
    </>
  );

  return (
    <div className="@container space-y-1.5" data-testid="run-waterfall">
      {compact ? (
        <div className={frame}>{body}</div>
      ) : (
        <Card className="overflow-hidden p-0">{body}</Card>
      )}
      {!compact && (
        <p className="text-[11px] leading-relaxed text-muted-foreground">
          Bars share one time axis (start = position, width = duration); hatched bands are the gaps
          between steps; violet bars are instrumented phases. Scroll on the timeline to zoom; click
          any row to inspect its timing, I/O and reasoning.
        </p>
      )}
    </div>
  );
}
