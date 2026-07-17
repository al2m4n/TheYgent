// The editor's test console — run the graph AS IT IS ON THE CANVAS (draft, unsaved, unpublished:
// the inline-IR run path takes the document straight from memory) without leaving the editor.
// While the run streams, the trace stream's per-node spans drive live execution state ONTO the
// canvas (each node pulses while its step runs, then wears its outcome), the output streams into
// the panel, and the Trace tab holds the same waterfall every other run surface uses — hovering a
// row flashes the node it came from.

import { useQueryClient } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { ExternalLink, Play, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Markdown } from "../chat/Markdown";
import { ThinkingBlock } from "../chat/ThinkingBlock";
import { api, streamGet, streamRun } from "../lib/api";
import { isDurableOnly } from "../lib/durable";
import { shortId } from "../lib/format";
import type { DeltaFrame, ReasoningFrame, RunFrame, Span } from "../lib/runtypes";
import { keys } from "../queries";
import { parseTyped } from "./ResumePanel";
import { Button, ErrorBanner, Input, Select } from "./ui";
import { Bubble, BubbleContent } from "./ui/bubble";
import { RunWaterfall } from "./waterfall";

/** Per-node execution state, joined from trace spans: running | ok | err | skipped. */
export type RunStateMap = Record<string, string>;

interface Result {
  runId: string;
  output?: string;
  reasoning?: string;
  error?: string;
  streaming?: boolean;
}

interface Props {
  ir: IRDocument;
  /** Validation errors gate the Run button — the server would 400 the same graph anyway. */
  errorCount: number;
  /** The code view holds unparsed JSON — `ir` is the LAST VALID parse, so running it would test
   * a document the author isn't looking at. Gates Run like the Publish button. */
  codeInvalid?: boolean;
  onClose: () => void;
  /** Live per-node execution state for the canvas ({} clears it). */
  onRunState: (state: RunStateMap) => void;
  /** Waterfall row hover → canvas flash (null clears). */
  onHoverNode: (nodeId: string | null) => void;
}

const MIN_HEIGHT = 160;
const MAX_HEIGHT = 560;
const DEFAULT_HEIGHT = 300;

export function TestPanel({
  ir,
  errorCount,
  codeInvalid = false,
  onClose,
  onRunState,
  onHoverNode,
}: Props) {
  const [height, setHeight] = useState(DEFAULT_HEIGHT);
  const [input, setInput] = useState("");
  // A graph that drills `$in.in.<field>` takes an OBJECT input: JSON mode parses client-side
  // (loudly) so an unparsable payload never leaves the tab as a look-alike string.
  const [inputMode, setInputMode] = useState<"text" | "json">("text");
  const [tab, setTab] = useState<"output" | "trace">("output");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<Result | null>(null);
  const queryClient = useQueryClient();

  const typedInput = parseTyped(inputMode, input);
  const durableOnly = isDurableOnly(ir);

  // Read-latest ref so a run always executes the CURRENT canvas document, not the one from the
  // render that created the callback.
  const irRef = useRef(ir);
  irRef.current = ir;

  // Closing the panel (or leaving the editor) mid-stream must abort both streams — the server
  // cancels the run on disconnect; an orphaned reader keeps a local engine generating.
  const abortRef = useRef<(() => void) | null>(null);
  const traceAbortRef = useRef<(() => void) | null>(null);
  const stoppedRef = useRef(false);
  // Ties each trace follower to the run that spawned it: a re-run bumps the generation, so a
  // stale follower (even one whose streamGet was still connecting when it was superseded) can
  // neither claim the abort ref nor paint statuses over the new run's.
  const traceGenRef = useRef(0);
  useEffect(
    () => () => {
      stoppedRef.current = true;
      traceGenRef.current += 1;
      abortRef.current?.();
      traceAbortRef.current?.();
    },
    [],
  );

  // Follow the run's trace stream and mirror node span open/close onto the canvas. Phase spans
  // (model.generate, tool.*) carry their parent's node_id — only node spans (no phase) count,
  // or one node would flip running/ok per phase.
  async function followTrace(runId: string) {
    const gen = ++traceGenRef.current;
    const statuses: RunStateMap = {};
    try {
      const handle = await streamGet(`/runs/${encodeURIComponent(runId)}/trace/stream`);
      if (gen !== traceGenRef.current) {
        handle.abort(); // superseded while connecting — never adopt the ref
        return;
      }
      traceAbortRef.current = handle.abort;
      for await (const ev of handle.events) {
        if (gen !== traceGenRef.current) return;
        if (ev.event === "done") break;
        if (ev.event !== "span.open" && ev.event !== "span.close") continue;
        let span: Span;
        try {
          span = JSON.parse(ev.data) as Span;
        } catch {
          continue;
        }
        if (!span.node_id || span.phase) continue;
        statuses[span.node_id] = ev.event === "span.open" ? "running" : span.status;
        onRunState({ ...statuses });
      }
    } catch {
      // Observability may be absent — the run output still streams; the canvas just stays unlit.
    } finally {
      if (gen === traceGenRef.current) traceAbortRef.current = null;
    }
  }

  // Supersede + abort whatever trace follower is live (or still connecting).
  function cancelTrace() {
    traceGenRef.current += 1;
    traceAbortRef.current?.();
    traceAbortRef.current = null;
  }

  async function run() {
    if (!typedInput.ok || running || errorCount > 0 || durableOnly || codeInvalid) return;
    setRunning(true);
    setResult(null);
    onRunState({});
    stoppedRef.current = false;
    // A lingering follower from the previous run must die before its replacement spawns.
    cancelTrace();
    // Hoisted so a mid-stream transport failure still keeps the run link + Trace tab usable.
    let runId = "";
    try {
      const handle = await streamRun("/graphs/runs", {
        ir: irRef.current,
        input: typedInput.value,
        stream: true,
      });
      abortRef.current = handle.abort;
      let content = "";
      let reasoning = "";
      let failed: string | undefined;
      let stopped = false;
      try {
        for await (const ev of handle.events) {
          if (ev.data === "[DONE]") continue;
          let payload: RunFrame | DeltaFrame | ReasoningFrame;
          try {
            payload = JSON.parse(ev.data);
          } catch {
            continue;
          }
          if (!runId && payload.runId) {
            runId = payload.runId;
            void followTrace(runId); // needs the run id — attachable only from the first frame on
          }
          if (ev.event === "delta") {
            content += (payload as DeltaFrame).delta;
          } else if (ev.event === "reasoning") {
            reasoning += (payload as ReasoningFrame).reasoning;
          } else if (ev.event === "run") {
            const frame = payload as RunFrame;
            if (frame.status === "failed") failed = frame.error ?? "run failed";
          }
          setResult({
            runId,
            output: content || undefined,
            reasoning: reasoning || undefined,
            streaming: true,
          });
        }
      } catch (e) {
        if (stoppedRef.current) stopped = true;
        else throw e;
      }
      // The persisted run row carries the CANONICAL output — a tool/router-terminal graph emits
      // no deltas at all. It also carries the honest empty-output note (error on `completed`).
      if (!stopped && runId) {
        try {
          const run = await api.getRun(runId);
          if (run.output) content = run.output;
          if (run.status === "failed") failed = failed ?? run.error ?? "run failed";
          else if (!content && run.error) failed = failed ?? run.error;
        } catch {
          /* keep the streamed view */
        }
        // Reconcile the canvas from the PERSISTED spans: nodes that executed during stream
        // priming (the input boundary runs before a client can attach the trace stream) or on
        // another process never reached the live overlay — the terminal trace has them all.
        try {
          const spans = await api.getTrace(runId);
          const statuses: RunStateMap = {};
          for (const s of spans) {
            if (s.node_id && !s.phase) {
              statuses[s.node_id] = s.end_ns == null ? "running" : s.status;
            }
          }
          if (Object.keys(statuses).length > 0) onRunState(statuses);
        } catch {
          /* observability absent — the live overlay (if any) stands */
        }
      }
      setResult({
        runId,
        output: content || undefined,
        reasoning: reasoning || undefined,
        error: failed ?? (stopped ? "stopped" : undefined),
        streaming: false,
      });
    } catch (e) {
      // A pre-stream 400 (invalid_ir & friends) arrives before any run exists; a mid-stream
      // transport failure arrives after — keep whatever runId exists so the run link/Trace
      // tab still point at the honest server-side record.
      setResult({ runId, error: e instanceof Error ? e.message : String(e) });
    } finally {
      if (runId) queryClient.invalidateQueries({ queryKey: keys.trace(runId) });
      abortRef.current = null;
      cancelTrace();
      setRunning(false);
    }
  }

  const runDisabled = running || !typedInput.ok || errorCount > 0 || durableOnly || codeInvalid;
  const runTitle = durableOnly
    ? "This graph contains durable-only nodes (human/subgraph/loop/map) — publish it and use Run durably"
    : codeInvalid
      ? "Fix the invalid JSON in the code view before running"
      : errorCount > 0
        ? `Fix ${errorCount} validation error${errorCount === 1 ? "" : "s"} before running`
        : "Run the current canvas graph (input → output), streaming (Enter)";

  return (
    <section
      className="relative flex shrink-0 flex-col border-t border-slate-800 bg-[var(--c-surface)]"
      style={{ height }}
      aria-label="Test run panel"
    >
      <HeightHandle height={height} onResize={setHeight} />
      {/* header: run controls + tabs */}
      <div className="flex flex-wrap items-center gap-2 border-b border-slate-800 px-3 py-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-400">
          <Play size={13} aria-hidden /> Test
        </span>
        <Select
          value={inputMode}
          onChange={(e) => setInputMode(e.target.value as "text" | "json")}
          className="w-20"
          aria-label="Input mode"
        >
          <option value="text">Text</option>
          <option value="json">JSON</option>
        </Select>
        <Input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder={inputMode === "json" ? '{"field": "value"}' : "Test input…"}
          className="w-64 flex-1"
          aria-label="Test input"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing && !runDisabled) {
              e.preventDefault();
              void run();
            }
          }}
        />
        <Button
          variant="primary"
          onClick={() => void run()}
          disabled={runDisabled}
          title={runTitle}
        >
          {running ? "Running…" : "Run"}
        </Button>
        {running && (
          <Button
            variant="ghost"
            onClick={() => {
              stoppedRef.current = true;
              abortRef.current?.();
            }}
          >
            Stop
          </Button>
        )}
        <div className="ml-auto flex items-center gap-2">
          {result?.runId && (
            <>
              <a
                href={`/runs/${encodeURIComponent(result.runId)}`}
                target="_blank"
                rel="noreferrer"
                className="mono inline-flex items-center gap-1 text-[11px] text-slate-500 hover:text-slate-300"
                title="Open the full run detail in a new tab"
              >
                {shortId(result.runId, 10)} <ExternalLink size={11} aria-hidden />
              </a>
              <TabButton active={tab === "output"} onClick={() => setTab("output")}>
                Output
              </TabButton>
              <TabButton active={tab === "trace"} onClick={() => setTab("trace")}>
                Trace
              </TabButton>
            </>
          )}
          <button
            type="button"
            onClick={onClose}
            title="Close the test panel"
            aria-label="Close the test panel"
            className="text-slate-500 hover:text-slate-300"
          >
            <X size={14} />
          </button>
        </div>
      </div>

      {/* body */}
      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {!typedInput.ok && <p className="mb-2 text-xs text-amber-400">{typedInput.error}</p>}
        {durableOnly && (
          <p className="text-sm text-slate-500">
            This graph contains durable-only nodes (human / subgraph / loop / map) — publish it and
            run it durably from the Agents page.
          </p>
        )}
        {!durableOnly && !result && !running && (
          <p className="text-sm text-slate-500">
            Run the graph exactly as it is on the canvas — no publish needed. Nodes light up as they
            execute; the Trace tab shows the per-node waterfall.
          </p>
        )}
        {tab === "trace" && result?.runId ? (
          <RunWaterfall
            runId={result.runId}
            isLive={Boolean(result.streaming)}
            compact
            onHoverNode={onHoverNode}
          />
        ) : (
          <div className="space-y-2">
            {result?.error && <ErrorBanner error={result.error} />}
            {result?.reasoning && (
              <ThinkingBlock
                reasoning={result.reasoning}
                streaming={Boolean(result.streaming) && !result.output}
              />
            )}
            {result?.output && (
              <Bubble variant="secondary" className="max-w-full">
                <BubbleContent>
                  <Markdown text={result.output} />
                  {result.streaming && (
                    <span className="animate-pulse text-muted-foreground">▍</span>
                  )}
                </BubbleContent>
              </Bubble>
            )}
            {running && !result?.output && !result?.reasoning && (
              <p className="text-sm text-slate-400">Running…</p>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function TabButton({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={`rounded px-2 py-0.5 text-[11px] font-medium ${
        active
          ? "bg-primary/10 text-primary"
          : "text-slate-500 hover:bg-[var(--c-hover)] hover:text-slate-300"
      }`}
    >
      {children}
    </button>
  );
}

// The panel's top edge doubles as a height splitter (the horizontal sibling of ResizeHandle —
// same pointer-capture drag, same keyboard pattern, vertical axis). Double-click resets.
function HeightHandle({ height, onResize }: { height: number; onResize: (h: number) => void }) {
  const drag = useRef<{ startY: number; startH: number } | null>(null);
  const clamp = (h: number) => Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, h));
  // Shared by pointerup AND pointercancel (touch scroll takeover, capture loss) — a drag that
  // ends any way must restore the page-wide cursor/select styles, or they stick.
  const end = (e: React.PointerEvent) => {
    if (!drag.current) return;
    drag.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  };
  return (
    <div
      role="separator"
      aria-orientation="horizontal"
      aria-label="Resize the test panel"
      aria-valuenow={height}
      aria-valuemin={MIN_HEIGHT}
      aria-valuemax={MAX_HEIGHT}
      tabIndex={0}
      className="absolute -top-1 left-0 z-10 h-2 w-full touch-none cursor-row-resize hover:bg-blue-500/30 focus-visible:bg-blue-500/40 focus-visible:outline-none"
      onPointerDown={(e) => {
        e.preventDefault();
        drag.current = { startY: e.clientY, startH: height };
        e.currentTarget.setPointerCapture(e.pointerId);
        document.body.style.cursor = "row-resize";
        document.body.style.userSelect = "none";
      }}
      onPointerMove={(e) => {
        if (!drag.current) return;
        onResize(clamp(drag.current.startH - (e.clientY - drag.current.startY)));
      }}
      onPointerUp={end}
      onPointerCancel={end}
      onDoubleClick={() => onResize(DEFAULT_HEIGHT)}
      onKeyDown={(e) => {
        if (e.key === "ArrowUp") onResize(clamp(height + 16));
        else if (e.key === "ArrowDown") onResize(clamp(height - 16));
        else if (e.key === "Home") onResize(MIN_HEIGHT);
        else if (e.key === "End") onResize(MAX_HEIGHT);
      }}
    />
  );
}
