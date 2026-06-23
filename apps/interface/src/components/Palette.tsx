// The node palette — derived ENTIRELY from the node-type registry (M15 §2.2 / Do-NOT: never
// hardcode the type list). An M14-style type added in `packages/ir` shows up here for free after a
// `generate`. Drag an item onto the canvas to drop a new node of that type.

import { NODE_TYPE_LIST } from "@theygent/ir-types";
import { Badge } from "./ui";

const KIND_TONE: Record<string, string> = {
  boundary: "green",
  activity: "blue",
  orchestration: "amber",
};

export function Palette() {
  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-slate-800 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        Nodes
      </div>
      <div className="flex-1 space-y-1.5 overflow-y-auto p-2">
        {NODE_TYPE_LIST.map((spec) => (
          <div
            key={spec.type}
            draggable
            onDragStart={(e) => {
              e.dataTransfer.setData("application/theygent-node-type", spec.type);
              e.dataTransfer.effectAllowed = "move";
            }}
            className="flex cursor-grab items-center justify-between rounded-md border border-slate-800 bg-[#11161f] px-2.5 py-2 hover:border-slate-600 active:cursor-grabbing"
            title={`Drag to add a ${spec.type} node (${spec.kind})`}
          >
            <span className="mono text-xs text-slate-200">{spec.type}</span>
            <Badge tone={KIND_TONE[spec.kind] ?? "slate"}>{spec.kind}</Badge>
          </div>
        ))}
      </div>
      <div className="border-t border-slate-800 p-2 text-[10px] leading-relaxed text-slate-600">
        Drag a node onto the canvas. Connect ports by dragging handle&nbsp;→&nbsp;handle. Select a
        node to edit its config.
      </div>
    </div>
  );
}
