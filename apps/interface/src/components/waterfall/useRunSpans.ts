// One data path for every waterfall surface: poll the persisted spans (1s while the run is live)
// and overlay the /trace/stream SSE while live, merged by deterministic span id (a persisted span
// is authoritative once closed; a live in-flight span keeps its growing bar until the close lands).
// retry is off because observability may be absent — the caller degrades to a note, never hammers.

import { useEffect, useMemo, useRef, useState } from "react";
import { streamGet } from "../../lib/api";
import type { Span } from "../../lib/runtypes";
import { useTrace } from "../../queries";
import { type WaterfallSpan, mergeSpans } from "./spans";

export function useRunSpans(runId: string, isLive: boolean) {
  const trace = useTrace(runId, { live: isLive });
  const [live, setLive] = useState<Map<string, WaterfallSpan>>(new Map());

  // A new run id starts from a clean overlay — the map is keyed per span id, and a reused
  // component (the bench re-runs in place) must not bleed a previous run's in-flight bars.
  // Render-phase reset (not an effect) so the stale bars never reach the screen.
  const lastRunId = useRef(runId);
  if (lastRunId.current !== runId) {
    lastRunId.current = runId;
    setLive(new Map());
  }

  useEffect(() => {
    if (!isLive) return;
    let cancelled = false;
    let abort: (() => void) | null = null;
    (async () => {
      try {
        const handle = await streamGet(`/runs/${encodeURIComponent(runId)}/trace/stream`);
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

  const spans = useMemo(() => mergeSpans(trace.data ?? [], live), [trace.data, live]);
  return { spans, isLoading: trace.isLoading, isError: trace.isError };
}
