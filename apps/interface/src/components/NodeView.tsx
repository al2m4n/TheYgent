// The custom React Flow node renderer. It draws ONE handle per declared port (in-ports left,
// out-ports right), keyed by the IR port id so edges connect by `sourceHandle`/`targetHandle` =
// port id (§8.3). Colour is keyed by the determinism `kind`, looked up from the registry via the
// node `type` — `kind` is NEVER stored on the RF node `data` (the one rule); it's derived here for
// display only. This component lives behind the adapter boundary (it is the canvas's view of a
// node), so its knowledge of React Flow is allowed.

import { kindForType } from "@theygent/ir-types";
import { Handle, type NodeProps, Position } from "@xyflow/react";
import type { TheygentRFNode } from "../adapter";
import { resolveIcon } from "../lib/icons";

const KIND_STYLE: Record<string, { ring: string; dot: string; label: string }> = {
  boundary: { ring: "border-emerald-600/70", dot: "bg-emerald-400", label: "text-emerald-300" },
  activity: { ring: "border-blue-600/70", dot: "bg-blue-400", label: "text-blue-300" },
  orchestration: { ring: "border-amber-600/70", dot: "bg-amber-400", label: "text-amber-300" },
};

function handleTop(index: number, count: number): string {
  return `${((index + 1) / (count + 1)) * 100}%`;
}

export function TheygentNode({ data, selected }: NodeProps<TheygentRFNode>) {
  const kind = kindForType(data.nodeType) ?? "activity";
  const style = KIND_STYLE[kind] ?? KIND_STYLE.activity;
  const ins = data.ports.in;
  const outs = data.ports.out;
  // The displayed icon: the user's override (a `view`-sourced display field) or the type default —
  // derived here, never stored on the IR's hashed content.
  const icon = resolveIcon(data.nodeType, data.icon);

  return (
    <div
      className={`relative min-w-[150px] rounded-lg border bg-[#161b26] px-3 py-2 shadow-md ${
        selected ? "border-blue-400 ring-1 ring-blue-400/50" : style.ring
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5">
          <span className="shrink-0 text-base leading-none" aria-hidden>
            {icon}
          </span>
          <span className="truncate text-sm font-medium text-slate-100">{data.label}</span>
        </span>
        <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} />
      </div>
      <div className={`mono mt-0.5 text-[10px] uppercase tracking-wide ${style.label}`}>
        {data.nodeType}
      </div>

      {ins.map((p, i) => (
        <Handle
          key={`in-${p.id}`}
          id={p.id}
          type="target"
          position={Position.Left}
          style={{ top: handleTop(i, ins.length), background: p.required ? "#60a5fa" : "#475569" }}
          title={`in · ${p.id}${p.required ? "" : " (optional)"}`}
        />
      ))}
      {outs.map((p, i) => (
        <Handle
          key={`out-${p.id}`}
          id={p.id}
          type="source"
          position={Position.Right}
          style={{
            top: handleTop(i, outs.length),
            background: p.id === "err" ? "#f87171" : "#34d399",
          }}
          title={`out · ${p.id}`}
        />
      ))}
    </div>
  );
}
