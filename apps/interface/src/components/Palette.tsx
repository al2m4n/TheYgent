// The node palette — derived ENTIRELY from the node-type registry (M15 §2.2 / Do-NOT: never
// hardcode the type list). An M14-style type added in `packages/ir` shows up here for free after a
// `generate`. Drag an item onto the canvas to drop a new node of that type.
//
// Nodes are grouped by their determinism `kind` (the registry's own taxonomy — boundary/activity/
// orchestration), with category filter chips + a text search so a long type list stays navigable.
// Both the category set and the per-category items are derived from the registry; nothing is
// hardcoded, so a new kind/type just appears.

import { NODE_TYPE_LIST, type NodeTypeSpec } from "@theygent/ir-types";
import { useMemo, useState } from "react";
import { defaultIconFor } from "../lib/icons";
import { Badge } from "./ui";

const KIND_TONE: Record<string, string> = {
  boundary: "green",
  activity: "blue",
  orchestration: "amber",
};

// Friendlier category labels (the chip + group header text). A kind without an entry falls back to
// its raw name, so an unknown future kind still groups correctly.
const KIND_LABEL: Record<string, string> = {
  boundary: "I/O & Human",
  activity: "Compute & Tools",
  orchestration: "Control flow",
};
const KIND_ORDER = ["boundary", "activity", "orchestration"];

function kindLabel(kind: string): string {
  return KIND_LABEL[kind] ?? kind;
}

export function Palette() {
  const [query, setQuery] = useState("");
  const [activeKind, setActiveKind] = useState<string | null>(null);
  // Per-category collapse — categories are collapsible but EXPANDED by default (a kind is collapsed
  // only once the user explicitly closes it). UI-only state, not persisted.
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const toggleCollapsed = (kind: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(kind)) next.delete(kind);
      else next.add(kind);
      return next;
    });

  // The kinds present in the registry, in a stable order (known kinds first, then any extras).
  const kinds = useMemo(() => {
    const present = [...new Set(NODE_TYPE_LIST.map((s) => s.kind))];
    return present.sort((a, b) => {
      const ia = KIND_ORDER.indexOf(a);
      const ib = KIND_ORDER.indexOf(b);
      return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    });
  }, []);

  const q = query.trim().toLowerCase();
  const matches = (spec: NodeTypeSpec) =>
    (!activeKind || spec.kind === activeKind) &&
    (q === "" ||
      spec.type.toLowerCase().includes(q) ||
      kindLabel(spec.kind).toLowerCase().includes(q));

  const groups = kinds
    .map((kind) => ({
      kind,
      items: NODE_TYPE_LIST.filter((s) => s.kind === kind && matches(s)),
    }))
    .filter((g) => g.items.length > 0);

  const total = groups.reduce((n, g) => n + g.items.length, 0);

  return (
    <div className="flex h-full flex-col">
      <div className="border-b border-slate-800 px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        Nodes
      </div>

      {/* search */}
      <div className="border-b border-slate-800 p-2">
        <div className="relative">
          <span className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-2 text-xs text-slate-600">
            🔍
          </span>
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search nodes…"
            className="w-full rounded-md border border-slate-700 bg-[#0e131c] py-1 pr-6 pl-7 text-xs text-slate-100 outline-none focus:border-blue-500"
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery("")}
              title="Clear search"
              className="-translate-y-1/2 absolute top-1/2 right-1.5 text-xs text-slate-500 hover:text-slate-300"
            >
              ✕
            </button>
          )}
        </div>

        {/* category filter chips */}
        <div className="mt-2 flex flex-wrap gap-1">
          <Chip active={activeKind === null} onClick={() => setActiveKind(null)}>
            All
          </Chip>
          {kinds.map((kind) => (
            <Chip
              key={kind}
              active={activeKind === kind}
              tone={KIND_TONE[kind]}
              onClick={() => setActiveKind((k) => (k === kind ? null : kind))}
            >
              {kindLabel(kind)}
            </Chip>
          ))}
        </div>
      </div>

      {/* grouped, filtered list */}
      <div className="flex-1 overflow-y-auto p-2">
        {total === 0 ? (
          <p className="px-1 py-6 text-center text-xs text-slate-600">No nodes match “{query}”.</p>
        ) : (
          groups.map((g) => {
            const isCollapsed = collapsed.has(g.kind);
            return (
              <div key={g.kind} className="mb-3 last:mb-0">
                <button
                  type="button"
                  onClick={() => toggleCollapsed(g.kind)}
                  title={isCollapsed ? "Expand category" : "Collapse category"}
                  className="flex w-full items-center justify-between rounded px-1 pb-1 hover:bg-[#11161f]"
                >
                  <span className="flex items-center gap-1 text-[10px] font-semibold uppercase tracking-wide text-slate-500">
                    <span
                      className={`text-slate-600 transition-transform ${isCollapsed ? "" : "rotate-90"}`}
                      aria-hidden
                    >
                      ▸
                    </span>
                    {kindLabel(g.kind)}
                  </span>
                  <span className="text-[10px] text-slate-600">{g.items.length}</span>
                </button>
                {!isCollapsed && (
                  <div className="space-y-1.5">
                    {g.items.map((spec) => (
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
                        <span className="flex items-center gap-1.5">
                          <span className="text-base leading-none" aria-hidden>
                            {defaultIconFor(spec.type)}
                          </span>
                          <span className="mono text-xs text-slate-200">{spec.type}</span>
                        </span>
                        <Badge tone={KIND_TONE[spec.kind] ?? "slate"}>{spec.kind}</Badge>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function Chip({
  active,
  tone,
  onClick,
  children,
}: {
  active: boolean;
  tone?: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  const dot: Record<string, string> = {
    green: "bg-emerald-400",
    blue: "bg-blue-400",
    amber: "bg-amber-400",
  };
  return (
    <button
      type="button"
      onClick={onClick}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] transition-colors ${
        active
          ? "border-blue-500 bg-blue-950 text-blue-200"
          : "border-slate-700 bg-[#11161f] text-slate-400 hover:border-slate-500 hover:text-slate-200"
      }`}
    >
      {tone && <span className={`h-1.5 w-1.5 rounded-full ${dot[tone] ?? "bg-slate-400"}`} />}
      {children}
    </button>
  );
}
