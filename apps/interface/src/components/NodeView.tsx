// The custom React Flow node renderer. It draws ONE handle per declared port (in-ports left,
// out-ports right), keyed by the IR port id so edges connect by `sourceHandle`/`targetHandle` =
// port id. Colour is keyed by the determinism `kind`, looked up from the registry via the
// node `type` — `kind` is NEVER stored on the RF node `data` (the one rule); it's derived here for
// display only. This component lives behind the adapter boundary (it is the canvas's view of a
// node), so its knowledge of React Flow is allowed.

import { kindForType } from "@theygent/ir-types";
import { Handle, type NodeProps, Position } from "@xyflow/react";
import type { TheygentRFNode } from "../adapter";
import { NodeIcon, resolveIcon } from "../lib/icons";

// Label colours are semantic (not on the inverted slate ramp), so each pairs a light + dark shade —
// otherwise the type label vanishes on the white node card in light mode.
const KIND_STYLE: Record<string, { ring: string; dot: string; label: string }> = {
  boundary: {
    ring: "border-emerald-600/70",
    dot: "bg-emerald-400",
    label: "text-emerald-700 dark:text-emerald-300",
  },
  activity: {
    ring: "border-blue-600/70",
    dot: "bg-blue-400",
    label: "text-blue-700 dark:text-blue-300",
  },
  orchestration: {
    ring: "border-amber-600/70",
    dot: "bg-amber-400",
    label: "text-amber-700 dark:text-amber-300",
  },
};

function handlePos(index: number, count: number): string {
  return `${((index + 1) / (count + 1)) * 100}%`;
}

export function TheygentNode({ data, selected }: NodeProps<TheygentRFNode>) {
  const kind = kindForType(data.nodeType) ?? "activity";
  const style = KIND_STYLE[kind] ?? KIND_STYLE.activity;
  // Data handles sit on the node SIDES (round), control handles on TOP/BOTTOM (square), so the two
  // channels read at a glance and a connection drag lands on the right kind. The role is per-port
  // (defaults to `data`, so a graph saved before roles existed is unchanged).
  const ins = data.ports.in.filter((p) => (p.role ?? "data") === "data");
  const outs = data.ports.out.filter((p) => (p.role ?? "data") === "data");
  const ctrlIns = data.ports.in.filter((p) => p.role === "control");
  const ctrlOuts = data.ports.out.filter((p) => p.role === "control");
  // `tool`-role handles — a third style (violet, on top/bottom), distinct from data (round,
  // sides) and control (amber square). The llm's `tools` IN-port (bottom) RECEIVES; a tool/mcp_tool
  // node's `use` OUT-port (top) OFFERS the tool. Wire `use` → `tools` to make the model able to call
  // it (one edge declares the capability; the request→run→response is the runtime loop).
  const toolIns = data.ports.in.filter((p) => p.role === "tool");
  const toolOuts = data.ports.out.filter((p) => p.role === "tool");
  // The displayed icon: the user's override (a `view`-sourced display field) or the type default —
  // resolved here to a Lucide icon name, never stored on the IR's hashed content.
  const iconName = resolveIcon(data.nodeType, data.icon);

  return (
    <div
      className={`relative min-w-[150px] rounded-lg border bg-[var(--c-elev)] px-3 py-2 shadow-md ${
        selected ? "border-blue-400 ring-1 ring-blue-400/50" : style.ring
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5">
          <NodeIcon name={iconName} className="shrink-0 text-slate-300" size={16} />
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
          style={{ top: handlePos(i, ins.length), background: p.required ? "#60a5fa" : "#475569" }}
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
            top: handlePos(i, outs.length),
            // Error handles are red by port TYPE — the runtime's error semantics key on
            // `type == "error"`, not on the conventional id "err".
            background: p.type === "error" ? "#f87171" : "#34d399",
          }}
          title={`out · ${p.id}`}
        />
      ))}
      {/* Control handles — squared, amber, on top (in) / bottom (out). */}
      {ctrlIns.map((p, i) => (
        <Handle
          key={`cin-${p.id}`}
          id={p.id}
          type="target"
          position={Position.Top}
          style={{ left: handlePos(i, ctrlIns.length), background: "#f59e0b", borderRadius: 2 }}
          title={`control in · ${p.id}`}
        />
      ))}
      {ctrlOuts.map((p, i) => (
        <Handle
          key={`cout-${p.id}`}
          id={p.id}
          type="source"
          position={Position.Bottom}
          style={{ left: handlePos(i, ctrlOuts.length), background: "#f59e0b", borderRadius: 2 }}
          title={`control out · ${p.id}`}
        />
      ))}
      {/* The llm's `tools` IN-port (violet, bottom) — a tool node's `use` handle wires here so
          the model may CALL it. Distinct from data (sides) and control (amber). */}
      {toolIns.map((p, i) => (
        <Handle
          key={`tin-${p.id}`}
          id={p.id}
          type="target"
          position={Position.Bottom}
          style={{
            left: handlePos(i, toolIns.length),
            background: "#a78bfa",
            borderRadius: 2,
            width: 11,
            height: 11,
          }}
          title={`tools · ${p.id} — wire a tool node's "use" handle here (the model may call it)`}
        />
      ))}
      {/* A tool/mcp_tool node's `use` OUT-port (violet, top) — drag it into an llm's `tools`
          handle to make the tool callable by the model. */}
      {toolOuts.map((p, i) => (
        <Handle
          key={`tout-${p.id}`}
          id={p.id}
          type="source"
          position={Position.Top}
          style={{
            left: handlePos(i, toolOuts.length),
            background: "#a78bfa",
            borderRadius: 2,
            width: 11,
            height: 11,
          }}
          title={`${p.id} — wire into an llm's tools port to let the model call this tool`}
        />
      ))}
    </div>
  );
}
