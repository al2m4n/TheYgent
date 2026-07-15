// The embedded inspector for a selected waterfall row — lives INSIDE the waterfall card (a side
// pane on wide containers, stacked below on narrow ones), never a separate modal. Header = the
// span's identity + a compact stat strip (status, timing, model, tokens, sizes); body = the gated
// per-node I/O as stacked, all-visible sections in the order the step actually happened:
// Input → Reasoning → Output. Reasoning is the reserved `reasoning` entry the server records in
// an llm node's captured outputs — it shows on the llm node and its model.generate row alike.

import { Brain, X } from "lucide-react";
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

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <span className="inline-flex items-baseline gap-1 whitespace-nowrap">
      <span className="text-[9px] uppercase tracking-wide opacity-60">{label}</span>
      <span className="mono text-foreground">{value}</span>
    </span>
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
            <pre
              className={`mono ${maxH} overflow-auto whitespace-pre-wrap break-words rounded-md border bg-background px-2 py-1 text-xs text-foreground`}
            >
              {typeof value === "string" ? value : JSON.stringify(value, null, 2)}
            </pre>
          </div>
        ))
      ) : (
        <p className="text-xs text-muted-foreground">—</p>
      )}
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
  // captured outputs so the Output tab shows only real ports.
  const reasoning =
    data?.outputs && typeof data.outputs.reasoning === "string" ? data.outputs.reasoning : null;
  const outputs = useMemo(() => {
    if (!data?.outputs) return data?.outputs ?? null;
    if (typeof data.outputs.reasoning !== "string") return data.outputs;
    const { reasoning: _omitted, ...rest } = data.outputs;
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
            title={span.phase ?? span.name}
          >
            {span.phase ?? span.name}
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
          Whole-run totals. Select a node or phase row for its input, output and reasoning.
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
