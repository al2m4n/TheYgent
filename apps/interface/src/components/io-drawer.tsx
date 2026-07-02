// The per-step context drawer shared by the run waterfalls: the gated input/output a node received
// and sent, honouring the capture-policy gating (a `reason` + level when payloads are withheld; a
// truncation note when over the byte cap). Fetching stays with the caller — each waterfall owns its
// own I/O query and derives the small node summary from its span type, so the drawer itself renders
// identically everywhere.

import { Badge, Card, Spinner } from "./ui";

function bytes(n: number | null | undefined): string | null {
  if (n == null) return null;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// The scalar summary shown in the drawer header, derived from the clicked node's span.
export interface IoDrawerNode {
  status: string;
  executor_id?: string | null;
  model?: unknown; // gen_ai.request.model attribute (unknown until typeof-checked)
  ttft?: unknown; // ttft_ms attribute
}

// The per-node I/O payload shape (inputs/outputs are null when capture is gated off).
export interface IoDrawerData {
  capture_level: string;
  inputs: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  bytes_in: number;
  bytes_out: number;
  truncated: boolean;
  reason: string | null;
}

export function IoDrawer({
  nodeId,
  onClose,
  node,
  io,
}: {
  nodeId: string;
  onClose: () => void;
  node: IoDrawerNode | null;
  io: { data: IoDrawerData | null | undefined; isLoading: boolean };
}) {
  const data = io.data;
  return (
    <Card className="space-y-3 border-blue-500/30 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h3 className="mono text-sm font-semibold text-slate-100">{nodeId}</h3>
          <div className="flex flex-wrap items-center gap-2 pt-1 text-xs text-slate-400">
            {node && (
              <Badge
                tone={node.status === "err" ? "red" : node.status === "skipped" ? "slate" : "green"}
              >
                {node.status}
              </Badge>
            )}
            {typeof node?.model === "string" && <span className="mono">{node.model}</span>}
            {typeof node?.ttft === "number" && <span>ttft {node.ttft}ms</span>}
            {/* The in-process executor is the unremarkable default — only a real worker id is
                worth a line. */}
            {node?.executor_id && node.executor_id !== "inproc" && (
              <span className="mono">worker {node.executor_id}</span>
            )}
          </div>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close I/O"
          className="rounded px-2 text-lg leading-none text-slate-500 hover:text-slate-300"
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
            <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-1.5 text-xs text-amber-700 dark:text-amber-300">
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

export function IoPane({
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
            <div className="mono text-[10px] text-blue-700 dark:text-blue-400">{port}</div>
            <pre className="mono max-h-60 overflow-auto whitespace-pre-wrap break-words rounded-md border border-slate-800 bg-[var(--c-bg)] px-2 py-1 text-xs text-slate-200">
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
