// The embedded inspector for a selected waterfall row — lives INSIDE the waterfall card (a side
// pane on wide containers, stacked below on narrow ones), never a separate modal. Header = the
// span's identity + a compact stat strip (status, timing, model, tokens, sizes); body = the gated
// per-node I/O as stacked, all-visible sections in the order the step actually happened:
// Input → Reasoning → Tool calls → Output. Reasoning and Tool calls are reserved entries the
// server records in an llm node's captured outputs — they show on the llm node and its phase rows
// alike. Tool calls carry each autonomous call's args + RESULT (what the tool returned, otherwise
// buried in the transient conversation); a `tool.<name>` phase row scopes down to its one call.

import { Brain, Wrench, X } from "lucide-react";
import { useMemo } from "react";
import { NodeIcon, defaultIconFor } from "../../lib/icons";
import { useNodeIo } from "../../queries";
import { Badge, Spinner } from "../ui";
import {
  type WaterfallSpan,
  attrNum,
  attrStr,
  bytes,
  ms,
  nowNs,
  sumAttr,
  tokensPerSec,
} from "./spans";

// One captured autonomous tool call + its result — the reserved `tool_calls` output entry the
// server records on an llm node. `iteration`/`index` mirror the `tool.<name>#<iter>.<idx>` phase id
// so the inspector can scope a single tool row to its own record.
interface ToolCallRecord {
  name?: string;
  arguments?: unknown;
  ok?: boolean;
  result?: unknown;
  iteration?: number;
  index?: number;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1 whitespace-nowrap">
      <span className="text-[9px] uppercase tracking-wide opacity-60">{label}</span>
      <span className="mono text-foreground">{value}</span>
    </span>
  );
}

// One payload block — a string renders verbatim, anything else pretty-prints as JSON. Scrolls
// internally past `maxH` so stacked sections all stay on screen.
function Payload({ value, maxH }: { value: unknown; maxH: string }) {
  return (
    <pre
      className={`mono ${maxH} overflow-auto whitespace-pre-wrap break-words rounded-md border bg-background px-2 py-1 text-xs text-foreground`}
    >
      {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
    </pre>
  );
}

function IoSection({
  label,
  data,
  sizeLabel,
  maxH,
}: {
  label: string;
  data: Record<string, unknown> | null;
  sizeLabel: string | null;
  maxH: string;
}) {
  return (
    <div className="space-y-1">
      <div className="flex items-baseline gap-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
        <span>{label}</span>
        {sizeLabel && <span className="font-normal normal-case opacity-70">{sizeLabel}</span>}
      </div>
      {data && Object.keys(data).length > 0 ? (
        Object.entries(data).map(([port, value]) => (
          <div key={port} className="space-y-0.5">
            <div className="mono text-[10px] text-blue-700 dark:text-blue-400">{port}</div>
            <Payload value={value} maxH={maxH} />
          </div>
        ))
      ) : (
        <p className="text-xs text-muted-foreground">—</p>
      )}
    </div>
  );
}

// The autonomous tool calls an llm node made, each with its args and — the point of this section —
// the RESULT the tool returned. `scoped` is true when a single `tool.<name>` phase row is selected
// (one call), false on the node / model.generate rows (the whole loop).
function ToolCalls({
  calls,
  scoped,
  maxH,
}: {
  calls: ToolCallRecord[];
  scoped: boolean;
  maxH: string;
}) {
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-amber-700 dark:text-amber-300">
        <Wrench size={11} />
        <span>{scoped ? "Tool result" : "Tool calls"}</span>
      </div>
      {calls.map((call, i) => (
        <div
          // The loop position (iteration/index) is the stable identity; fall back to the array slot.
          key={`${call.iteration ?? "i"}.${call.index ?? i}`}
          className="space-y-1 rounded-md border bg-muted/30 px-2 py-1.5"
        >
          <div className="flex items-center gap-1.5">
            <span className="mono truncate text-[11px] font-medium text-foreground">
              {call.name ?? "tool"}
            </span>
            <Badge tone={call.ok === false ? "red" : "green"}>
              {call.ok === false ? "err" : "ok"}
            </Badge>
          </div>
          {call.arguments !== undefined && call.arguments !== null && (
            <div className="space-y-0.5">
              <div className="mono text-[10px] text-blue-700 dark:text-blue-400">args</div>
              <Payload value={call.arguments} maxH={maxH} />
            </div>
          )}
          <div className="space-y-0.5">
            <div className="mono text-[10px] text-blue-700 dark:text-blue-400">result</div>
            <Payload value={call.result} maxH={maxH} />
          </div>
        </div>
      ))}
    </div>
  );
}

export function SpanDetail({
  runId,
  span,
  spans,
  onClose,
  compact = false,
}: {
  runId: string;
  span: WaterfallSpan;
  spans: WaterfallSpan[];
  onClose: () => void;
  compact?: boolean;
}) {
  const nodeId = span.node_id ?? null;
  const isGenerate = span.phase === "model.generate";
  // A phase row inspects through its owning node: the phase carries the node_id, the node span
  // carries the mirrored model attr, and the node_io row (payloads) exists only for the node.
  const nodeSpan = nodeId ? (spans.find((s) => s.node_id === nodeId && !s.phase) ?? null) : null;
  const io = useNodeIo(runId, nodeId);

  const dur = (span.end_ns ?? nowNs()) - span.start_ns;
  // Usage lands only on model.generate phase spans — a node/run total is the sum over them.
  const usageScope = useMemo(
    () =>
      spans.filter((s) => s.phase === "model.generate" && (nodeId == null || s.node_id === nodeId)),
    [spans, nodeId],
  );
  const tokensIn = isGenerate
    ? attrNum(span, "gen_ai.usage.input_tokens")
    : sumAttr(usageScope, "gen_ai.usage.input_tokens");
  const tokensOut = isGenerate
    ? attrNum(span, "gen_ai.usage.output_tokens")
    : sumAttr(usageScope, "gen_ai.usage.output_tokens");
  const model = attrStr(span, "gen_ai.request.model") ?? attrStr(nodeSpan, "gen_ai.request.model");
  const ttft = attrNum(span, "ttft_ms") ?? attrNum(nodeSpan, "ttft_ms");
  const finish = attrStr(span, "gen_ai.response.finish_reason");
  const rate = isGenerate ? tokensPerSec(span, dur) : null;
  const executor = span.executor_id ?? nodeSpan?.executor_id;

  const data = io.data;
  // The reserved reasoning entry is presentation, not a dataflow port — pull it out of the
  // captured outputs so the Output section shows only real ports.
  const reasoning =
    data?.outputs && typeof data.outputs.reasoning === "string" ? data.outputs.reasoning : null;
  // The reserved tool_calls entry (autonomous calls + results). On a `tool.<name>#<iter>.<idx>`
  // phase row, scope down to that one call; on the node / model.generate rows, show the whole loop.
  const toolCalls = useMemo<{ calls: ToolCallRecord[]; scoped: boolean } | null>(() => {
    const raw = data?.outputs?.tool_calls;
    if (!Array.isArray(raw) || raw.length === 0) return null;
    const all = raw as ToolCallRecord[];
    const m = span.phase?.match(/#(\d+)\.(\d+)$/);
    if (m) {
      const one = all.find((c) => c.iteration === Number(m[1]) && c.index === Number(m[2]));
      if (one) return { calls: [one], scoped: true };
    }
    return { calls: all, scoped: false };
  }, [data, span.phase]);
  // Both reserved entries are presentation, not dataflow ports — strip them from the Output section
  // (only when they carry the reserved SHAPE; an unexpectedly-typed value stays a real port).
  const outputs = useMemo(() => {
    const o = data?.outputs;
    if (!o) return o ?? null;
    const rest: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(o)) {
      if (k === "reasoning" && typeof v === "string") continue;
      if (k === "tool_calls" && Array.isArray(v)) continue;
      rest[k] = v;
    }
    return rest;
  }, [data]);

  // Three stacked sections share the vertical budget — keep each pane short enough that Input,
  // Reasoning and Output are all on screen together (each scrolls internally past the cap).
  const maxH = compact ? "max-h-28" : "max-h-44";
  const tone = span.status === "err" ? "red" : span.status === "skipped" ? "slate" : "green";

  return (
    <div className="flex min-w-0 flex-col gap-2 p-2.5 text-xs" data-testid="span-detail">
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-1.5">
          {!span.phase && (
            <NodeIcon
              name={defaultIconFor(span.node_type ?? "")}
              size={13}
              className="shrink-0 text-muted-foreground"
            />
          )}
          <h3
            className="mono truncate text-sm font-semibold text-foreground"
            title={span.name ?? span.phase}
          >
            {span.name ?? span.phase}
          </h3>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close details"
          className="rounded p-0.5 text-muted-foreground hover:text-foreground"
        >
          <X size={14} />
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        <Badge tone={span.end_ns == null ? "amber" : tone}>
          {span.end_ns == null ? "running" : span.status}
        </Badge>
        <Stat label="took" value={ms(dur)} />
        {model && <Stat label="model" value={model} />}
        {typeof ttft === "number" && <Stat label="ttft" value={`${Math.round(ttft)}ms`} />}
        {(tokensIn != null || tokensOut != null) && (
          <Stat label="tok" value={`${tokensIn ?? "?"}→${tokensOut ?? "?"}`} />
        )}
        {rate && <Stat label="rate" value={rate} />}
        {finish && <Stat label="finish" value={finish} />}
        {data && <Stat label="io" value={`${bytes(data.bytes_in)}→${bytes(data.bytes_out)}`} />}
        {executor && executor !== "inproc" && <Stat label="worker" value={executor} />}
      </div>

      {span.error && (
        <p className="break-words text-xs text-rose-700 dark:text-rose-300">{span.error}</p>
      )}

      {nodeId == null ? (
        <p className="text-muted-foreground">
          Whole-run totals. Select a node or phase row for its input, output, reasoning and tool
          results.
        </p>
      ) : (
        <>
          {io.isLoading && <Spinner label="Loading I/O…" />}
          {data && (
            <>
              {data.reason && (
                <div className="rounded-md border bg-muted/40 px-2 py-1.5 text-muted-foreground">
                  {data.reason} (capture: {data.capture_level})
                </div>
              )}
              {data.truncated && (
                <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-amber-700 dark:text-amber-300">
                  Payload truncated to the capture cap (the byte counts are the true sizes).
                </div>
              )}
              <IoSection
                label="Input"
                data={data.inputs}
                sizeLabel={bytes(data.bytes_in)}
                maxH={maxH}
              />
              {reasoning && (
                <div className="space-y-1">
                  <div className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-violet-700 dark:text-violet-300">
                    <Brain size={11} />
                    <span>Reasoning</span>
                  </div>
                  <pre
                    className={`mono ${maxH} overflow-auto whitespace-pre-wrap break-words rounded-md border border-violet-500/25 bg-background px-2 py-1 text-xs leading-relaxed text-muted-foreground`}
                  >
                    {reasoning}
                  </pre>
                </div>
              )}
              {toolCalls && (
                <ToolCalls calls={toolCalls.calls} scoped={toolCalls.scoped} maxH={maxH} />
              )}
              <IoSection
                label="Output"
                data={outputs}
                sizeLabel={bytes(data.bytes_out)}
                maxH={maxH}
              />
            </>
          )}
        </>
      )}
    </div>
  );
}
