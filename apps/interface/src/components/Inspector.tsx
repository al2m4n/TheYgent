// The node/edge inspector (M15 §2.3): edit the selected node's `config` (schema-driven from the
// per-type JSON Schema) or the selected edge's `channel`/`condition`, with explicit Delete /
// Duplicate controls. With nothing selected, the graph-level panel shows `models` (read-only) and
// editable `tools` / `connections` — adding a tool here makes it selectable on every llm node.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type IRDocument, type Node as IRNode, NODE_TYPES } from "@theygent/ir-types";
import {
  Check,
  ChevronDown,
  ChevronRight,
  ChevronUp,
  Diamond,
  Lock,
  Search,
  X,
} from "lucide-react";
import { Suspense, lazy, useEffect, useId, useRef, useState } from "react";
import {
  type Selection,
  type ToolBinding,
  type ToolKind,
  type ViewBlock,
  deleteEdges,
  deleteNodes,
  duplicateNode,
  setNodeIcon,
  setToolKind,
  toolKindOf,
  updateEdge,
  updateNodeConfig,
  updateNodeLabel,
} from "../adapter";
import { api } from "../lib/api";
import { NodeIcon, defaultIconFor } from "../lib/icons";
import { Badge, Button, Field, Input, Select } from "./ui";

// The full-icon search grid is lazy: its ~1,700-icon set (iconsFull) loads only when a user actually
// opens the icon picker, so the main editor bundle stays lean.
const IconPickerGrid = lazy(() => import("./IconPickerGrid"));

// node types whose config composes a saved, pinned agent (the `agent` field is an agent-id picker).
const PINNED_BODY_TYPES = new Set(["subgraph", "loop", "map"]);

interface Props {
  ir: IRDocument;
  selection: Selection;
  onChange: (ir: IRDocument) => void;
  onSelect: (s: Selection) => void;
}

const KIND_TONE: Record<string, string> = {
  boundary: "green",
  activity: "blue",
  orchestration: "amber",
};

export function Inspector({ ir, selection, onChange, onSelect }: Props) {
  if (selection?.kind === "edge") {
    return <EdgePanel ir={ir} edgeId={selection.id} onChange={onChange} onSelect={onSelect} />;
  }
  if (selection?.kind === "node") {
    const node = (ir.nodes ?? []).find((n) => n.id === selection.id);
    if (node) return <NodePanel ir={ir} nodeId={node.id} onChange={onChange} onSelect={onSelect} />;
  }
  return <GraphPanel ir={ir} />;
}

// ── node panel ────────────────────────────────────────────────────────────────

function NodePanel({
  ir,
  nodeId,
  onChange,
  onSelect,
}: {
  ir: IRDocument;
  nodeId: string;
  onChange: (ir: IRDocument) => void;
  onSelect: (s: Selection) => void;
}) {
  const node = (ir.nodes ?? []).find((n) => n.id === nodeId);
  if (!node) return <GraphPanel ir={ir} />;

  const spec = NODE_TYPES[node.type];
  const properties = (spec?.configSchema?.properties ?? {}) as Record<string, any>;
  const required = new Set<string>((spec?.configSchema?.required as string[]) ?? []);
  const config = (node.config ?? {}) as Record<string, unknown>;

  const setConfigKey = (key: string, value: unknown) => {
    onChange(updateNodeConfig(ir, node.id, { ...config, [key]: value }));
  };

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="border-b border-slate-800 px-3 py-2.5">
        <div className="flex items-center gap-2">
          <Badge tone={KIND_TONE[node.kind] ?? "slate"}>{node.kind}</Badge>
          <span className="mono text-xs text-slate-400">{node.type}</span>
        </div>
        <div className="mono mt-1 text-[11px] text-slate-600">{node.id}</div>
        <div className="mt-2 flex gap-2">
          <Button
            className="!py-1 text-xs"
            onClick={() => {
              const { ir: next, newId } = duplicateNode(ir, node.id);
              onChange(next);
              onSelect({ kind: "node", id: newId });
            }}
          >
            Duplicate
          </Button>
          <Button
            variant="danger"
            className="!py-1 text-xs"
            onClick={() => {
              onChange(deleteNodes(ir, [node.id]));
              onSelect(null);
            }}
          >
            Delete
          </Button>
        </div>
      </div>

      <div className="space-y-3 p-3">
        <Field label="Label">
          <Input
            value={node.label ?? ""}
            placeholder={node.id}
            onChange={(e) => onChange(updateNodeLabel(ir, node.id, e.target.value))}
          />
        </Field>

        <IconPicker ir={ir} nodeId={node.id} nodeType={node.type} onChange={onChange} />

        {node.type === "tool" || node.type === "mcp_tool" ? (
          // M22 D1: tool + mcp_tool are ONE node with a kind picker (builtin / REST / MCP) — the
          // kind chooses the binding shape and (for mcp) the underlying node type.
          <ToolNodePanel ir={ir} node={node} onChange={onChange} />
        ) : Object.keys(properties).length === 0 ? (
          <p className="text-xs text-slate-600">This node type has no editable config.</p>
        ) : (
          Object.entries(properties).map(([key, schema]) => {
            // M21 tool-calling: surface tools / toolChoice / maxToolIterations via the dedicated
            // Tools panel, rendered ONCE (for the `tools` key); the other two are edited inside it.
            if (node.type === "llm" && key === "tools") {
              return (
                <LlmToolsPanel
                  key={`${node.id}:${key}`}
                  ir={ir}
                  nodeId={node.id}
                  onChange={onChange}
                />
              );
            }
            if (node.type === "llm" && (key === "toolChoice" || key === "maxToolIterations")) {
              return null;
            }
            // Specialized pickers for reference fields — populated from the live registries instead
            // of free text, so you pick a real value (and the validator catches a dangling ref).
            if (node.type === "llm" && key === "model") {
              return (
                <ModelPicker
                  key={`${node.id}:${key}`}
                  ir={ir}
                  nodeId={node.id}
                  onChange={onChange}
                />
              );
            }
            // No-code chat editor: a role dropdown + a plain text box per message (with an insert-$in
            // helper) instead of raw JSON. Rich (vision) content falls back to a per-message JSON box.
            if (node.type === "llm" && key === "messages") {
              return (
                <MessagesEditor
                  key={`${node.id}:${key}`}
                  value={config.messages}
                  onChange={(v) => setConfigKey("messages", v)}
                />
              );
            }
            if (PINNED_BODY_TYPES.has(node.type) && key === "agent") {
              return (
                <AgentPicker
                  key={`${node.id}:${key}`}
                  value={config.agent as string | undefined}
                  onChange={(v) => setConfigKey("agent", v)}
                />
              );
            }
            return (
              <ConfigField
                key={`${node.id}:${key}`}
                name={key}
                schema={schema}
                required={required.has(key)}
                value={config[key]}
                onChange={(v) => setConfigKey(key, v)}
              />
            );
          })
        )}

        {node.type === "input" && <TriggerPanel agentId={ir.id} />}

        <RawConfigEditor
          key={`raw:${node.id}`}
          value={config}
          onCommit={(c) => onChange(updateNodeConfig(ir, node.id, c))}
        />
      </div>
    </div>
  );
}

// ── trigger panel (M19 §1.5 — triggers are invocation bindings on the input, never nodes) ─────────

/** The agent's M12 triggers, surfaced on the `input` node (§1.5). Triggers are NOT graph nodes —
 * "run every hour" is a `schedule` trigger, not a clock inside the durable workflow. Read-only here
 * (list what fires this saved agent); creating one is the deploy step (M12 `POST /triggers`). The
 * inert "Browse hub" (M16) affordance lives on the connections panel, not here. */
function TriggerPanel({ agentId }: { agentId: string }) {
  const { data: triggers, isLoading } = useQuery({
    queryKey: ["triggers"],
    queryFn: api.listTriggers,
    retry: false,
    staleTime: 15_000,
  });
  const mine = (triggers ?? []).filter((t) => t.agent_id === agentId);
  return (
    <Field label="Triggers (how this agent is invoked)">
      {isLoading ? (
        <p className="text-[11px] text-slate-600">loading…</p>
      ) : mine.length === 0 ? (
        <p className="text-[11px] text-slate-600">
          No triggers. Save the agent, then add a schedule/webhook/http trigger (M12) to run it
          unattended — triggers are invocation bindings, not graph nodes (§1.5).
        </p>
      ) : (
        <div className="space-y-1">
          {mine.map((t) => (
            <div
              key={t.id}
              className="mono flex items-center justify-between rounded border border-slate-800 px-2 py-1 text-[11px]"
            >
              <span className="text-slate-300">{t.kind}</span>
              <span className={t.enabled ? "text-emerald-400" : "text-slate-600"}>
                {t.enabled ? "enabled" : "disabled"}
              </span>
            </div>
          ))}
        </div>
      )}
    </Field>
  );
}

// ── icon picker (a `view`-only display change — never hashed, §1.4) ────────────

/** Pick a Lucide icon for a node, or reset to its type default. The override (a Lucide icon name)
 * lives in the `view` block, so it round-trips through save/load but never affects logic or version
 * identity. */
function IconPicker({
  ir,
  nodeId,
  nodeType,
  onChange,
}: {
  ir: IRDocument;
  nodeId: string;
  nodeType: string;
  onChange: (ir: IRDocument) => void;
}) {
  const override = (ir.view as ViewBlock | undefined)?.nodes?.[nodeId]?.icon;
  const fallback = defaultIconFor(nodeType);
  const current = override && override.length > 0 ? override : fallback;
  // The picker is collapsible and COLLAPSED by default — the header shows the current icon so it stays
  // unobtrusive until you want to change it. `query` searches the full Lucide set. UI-only state.
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");

  return (
    <Field label="Icon">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={open ? "Hide icon choices" : "Change icon"}
        className="flex w-full items-center gap-2 rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-xs text-slate-300 hover:border-slate-500"
      >
        <NodeIcon name={current} className="text-slate-300" size={16} />
        <span className="text-slate-500">Change icon</span>
        <ChevronRight
          size={13}
          aria-hidden
          className={`ml-auto text-slate-600 transition-transform ${open ? "rotate-90" : ""}`}
        />
      </button>
      {open && (
        <div className="mt-1.5 space-y-1.5">
          <div className="relative">
            <Search
              size={13}
              aria-hidden
              className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-2 text-slate-600"
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search all icons…"
              className="w-full rounded-md border border-slate-700 bg-[#0e131c] py-1 pr-2 pl-7 text-xs text-slate-100 outline-none focus:border-blue-500"
            />
          </div>
          {/* The grid (and its full-icon set) is lazy — show a tiny placeholder while it loads. */}
          <Suspense fallback={<p className="mt-1.5 text-[11px] text-slate-600">Loading icons…</p>}>
            <IconPickerGrid
              query={query}
              current={current}
              onPick={(name) => onChange(setNodeIcon(ir, nodeId, name))}
            />
          </Suspense>
          <button
            type="button"
            onClick={() => onChange(setNodeIcon(ir, nodeId, null))}
            disabled={!override}
            className="flex items-center gap-1 text-[10px] text-slate-500 hover:text-slate-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Reset to default <NodeIcon name={fallback} size={12} />
          </button>
        </div>
      )}
    </Field>
  );
}

// ── edge panel ────────────────────────────────────────────────────────────────

function EdgePanel({
  ir,
  edgeId,
  onChange,
  onSelect,
}: {
  ir: IRDocument;
  edgeId: string;
  onChange: (ir: IRDocument) => void;
  onSelect: (s: Selection) => void;
}) {
  const edge = (ir.edges ?? []).find((e) => e.id === edgeId);
  if (!edge) return <GraphPanel ir={ir} />;
  const channel = edge.channel ?? "data";

  return (
    <div className="flex h-full flex-col overflow-y-auto">
      <div className="border-b border-slate-800 px-3 py-2.5">
        <span className="text-xs font-medium text-slate-300">Edge</span>
        <div className="mono mt-1 text-[11px] text-slate-600">{edge.id}</div>
        <div className="mt-2">
          <Button
            variant="danger"
            className="!py-1 text-xs"
            onClick={() => {
              onChange(deleteEdges(ir, [edge.id]));
              onSelect(null);
            }}
          >
            Delete edge
          </Button>
        </div>
      </div>

      <div className="space-y-3 p-3">
        <div className="mono rounded border border-slate-800 px-2 py-1.5 text-[11px] text-slate-400">
          {edge.source}
          <span className="text-slate-600">.{edge.sourceHandle}</span> →&nbsp;{edge.target}
          <span className="text-slate-600">.{edge.targetHandle}</span>
        </div>

        <Field label="Channel">
          {channel === "tool" ? (
            // M22: a `tool` (capability) edge's channel is DERIVED from the handles it joins (a tool
            // node → an llm `tools` port). It must not be flipped to data/control here — that would
            // silently break the capability. Show it read-only.
            <>
              <Badge tone="blue">tool (capability)</Badge>
              <p className="mt-1 text-[10px] text-slate-600">
                a tool the model may call — delete and re-wire to change it
              </p>
            </>
          ) : (
            <>
              <div className="flex gap-2">
                {(["data", "control"] as const).map((c) => (
                  <Button
                    key={c}
                    variant={channel === c ? "primary" : "default"}
                    className="!py-1 text-xs"
                    onClick={() => onChange(updateEdge(ir, edge.id, { channel: c }))}
                  >
                    {c}
                  </Button>
                ))}
              </div>
              <p className="mt-1 text-[10px] text-slate-600">
                {channel === "data"
                  ? "passes a value along the edge"
                  : "pure sequencing — no value passed"}
              </p>
            </>
          )}
        </Field>

        <Field label="Condition (router-driven, optional)">
          <Input
            value={edge.condition ?? ""}
            placeholder="e.g. $in.in.handle"
            onChange={(e) =>
              onChange(updateEdge(ir, edge.id, { condition: e.target.value || null }))
            }
          />
        </Field>
      </div>
    </div>
  );
}

// ── a single config field, dispatched by its JSON-Schema type ─────────────────

function primitiveType(schema: any): "string" | "number" | "boolean" | "complex" {
  let t = schema?.type;
  if (Array.isArray(t)) t = t.find((x: string) => x !== "null");
  if (t === "string") return "string";
  if (t === "integer" || t === "number") return "number";
  if (t === "boolean") return "boolean";
  return "complex";
}

function ConfigField({
  name,
  schema,
  required,
  value,
  onChange,
}: {
  name: string;
  schema: any;
  required: boolean;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const label = required ? `${name} *` : name;

  // A Literal field (modality, policy, source, …) is a JSON-Schema `enum` — render a dropdown so the
  // editor offers the exact valid values (M19 §2.10 modality affordance + all enum config fields).
  const enumValues = Array.isArray(schema?.enum) ? (schema.enum as unknown[]) : null;
  if (enumValues) {
    return (
      <Field label={label}>
        <select
          className="w-full rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-xs text-slate-100 outline-none focus:border-blue-500"
          value={value === null || value === undefined ? "" : String(value)}
          onChange={(e) => onChange(e.target.value)}
        >
          {value === undefined && <option value="">—</option>}
          {enumValues.map((v) => (
            <option key={String(v)} value={String(v)}>
              {String(v)}
            </option>
          ))}
        </select>
      </Field>
    );
  }

  const kind = primitiveType(schema);

  if (kind === "string") {
    return (
      <Field label={label}>
        <Input value={(value as string) ?? ""} onChange={(e) => onChange(e.target.value)} />
      </Field>
    );
  }
  if (kind === "number") {
    return (
      <Field label={label}>
        <Input
          type="number"
          value={value === null || value === undefined ? "" : String(value)}
          onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        />
      </Field>
    );
  }
  if (kind === "boolean") {
    return (
      <label className="flex items-center gap-2 text-sm text-slate-300">
        <input
          type="checkbox"
          checked={Boolean(value)}
          onChange={(e) => onChange(e.target.checked)}
        />
        {label}
      </label>
    );
  }
  // arrays / objects / refs (e.g. llm `messages`) — a guarded JSON editor.
  return <JsonField label={label} value={value} onChange={onChange} />;
}

function JsonField({
  label,
  value,
  onChange,
}: {
  // Optional: when omitted, the guarded textarea renders bare (no <Field> label) — used by the
  // Messages "JSON" view, which already has its own section header.
  label?: string;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const [text, setText] = useState(() => JSON.stringify(value ?? null, null, 2));
  const [error, setError] = useState<string | null>(null);

  // Re-sync when the underlying value changes from elsewhere (e.g. switching nodes remounts via key).
  useEffect(() => {
    setText(JSON.stringify(value ?? null, null, 2));
    setError(null);
    // value identity is the trigger; stringifying inside is intentional.
  }, [value]);

  const editor = (
    <>
      <textarea
        spellCheck={false}
        className={`mono h-28 w-full rounded-md border bg-[#0e131c] px-2.5 py-1.5 text-xs text-slate-100 outline-none ${
          error ? "border-red-700 focus:border-red-500" : "border-slate-700 focus:border-blue-500"
        }`}
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          try {
            const parsed = JSON.parse(e.target.value);
            setError(null);
            onChange(parsed);
          } catch (err) {
            setError((err as Error).message);
          }
        }}
      />
      {error && <span className="text-[11px] text-red-400">{error}</span>}
    </>
  );
  return label ? <Field label={label}>{editor}</Field> : <div className="space-y-1">{editor}</div>;
}

// ── the llm Tools panel (M22 — the tools wired into this llm, the model may call them) ─────

/** A tool the model can call: the source node of a `tool` (capability) edge into this llm. The kind
 * is derived from the source node like the runtime's _capability_binding (mcp_tool → mcp; a tool with
 * a connection/url → http; else builtin). */
function llmCapabilityTools(
  ir: IRDocument,
  llmId: string,
): { id: string; label: string; kind: string }[] {
  const byId = new Map((ir.nodes ?? []).map((n) => [n.id, n]));
  const out: { id: string; label: string; kind: string }[] = [];
  for (const e of ir.edges ?? []) {
    if ((e.channel ?? "data") !== "tool" || e.target !== llmId) continue;
    const n = byId.get(e.source);
    if (!n) continue;
    const cfg = (n.config ?? {}) as Record<string, unknown>;
    const kind =
      n.type === "mcp_tool" ? "mcp" : cfg.connection || cfg.urlTemplate ? "http" : "builtin";
    out.push({ id: n.id, label: n.label || (cfg.tool as string) || n.id, kind });
  }
  return out;
}

/** Show the tools this llm may autonomously call (M22): the tool/mcp_tool nodes wired into its
 * `tools` port (read-only — wire/unwire on the canvas), plus the tool_choice + iteration cap. Any
 * legacy `config.tools` keys (pre-M22 ir.tools refs) are listed too. The loop runs whenever the llm
 * has ANY tool (wired or legacy). */
function LlmToolsPanel({
  ir,
  nodeId,
  onChange,
}: {
  ir: IRDocument;
  nodeId: string;
  onChange: (ir: IRDocument) => void;
}) {
  const node = (ir.nodes ?? []).find((n) => n.id === nodeId);
  const config = (node?.config ?? {}) as Record<string, unknown>;
  const choice = typeof config.toolChoice === "string" ? (config.toolChoice as string) : "auto";
  const maxIter =
    typeof config.maxToolIterations === "number" ? (config.maxToolIterations as number) : 8;
  const wired = llmCapabilityTools(ir, nodeId);
  const legacy = Array.isArray(config.tools) ? (config.tools as string[]) : [];
  const hasTools = wired.length > 0 || legacy.length > 0;

  const set = (extra: Record<string, unknown>) =>
    onChange(
      updateNodeConfig(ir, nodeId, {
        ...config,
        toolChoice: choice,
        maxToolIterations: maxIter,
        ...extra,
      }),
    );

  return (
    <div className="space-y-1.5">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
        Tools the model can call
      </span>
      {!hasTools ? (
        <p className="text-[11px] leading-relaxed text-slate-600">
          None yet — drop a <span className="mono">tool</span> node, then drag its violet capability
          handle into this node's <span className="mono">tools</span> port (the violet handle on the
          bottom). The model decides when to call it.
        </p>
      ) : (
        <div className="space-y-1">
          {wired.map((t) => (
            <div
              key={t.id}
              className="flex items-center gap-2 rounded px-1 py-0.5 text-xs"
              title={`wired from ${t.id} — unwire on the canvas to remove`}
            >
              <Diamond size={11} className="shrink-0 fill-violet-400 text-violet-400" aria-hidden />
              <span className="mono text-slate-200">{t.label}</span>
              <Badge>{t.kind}</Badge>
            </div>
          ))}
          {legacy.map((k) => (
            <div key={k} className="flex items-center gap-2 rounded px-1 py-0.5 text-xs">
              <span className="mono text-slate-200">{k}</span>
              <Badge tone="slate">legacy</Badge>
            </div>
          ))}
        </div>
      )}
      {hasTools && (
        <div className="mt-1 space-y-2 rounded-md border border-slate-800 p-2">
          <label className="block space-y-1">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">
              tool choice
            </span>
            <Select value={choice} onChange={(e) => set({ toolChoice: e.target.value })}>
              <option value="auto">auto — model decides</option>
              <option value="required">required — must call a tool</option>
              <option value="none">none — never call</option>
            </Select>
          </label>
          <label className="block space-y-1">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">
              max tool iterations
            </span>
            <Input
              type="number"
              min={1}
              value={maxIter}
              onChange={(e) => set({ maxToolIterations: Math.max(1, Number(e.target.value) || 1) })}
            />
          </label>
        </div>
      )}
    </div>
  );
}

// ── the llm chat-message editor (no-code: role dropdown + text box per message) ─

type ChatMessage = { role: string; content: unknown };
type MsgRow = { key: string; role: string; content: unknown };
const ROLE_OPTIONS = ["system", "user", "assistant"];

function toMessages(value: unknown): ChatMessage[] {
  if (!Array.isArray(value)) return [];
  return value.map((m) => {
    const msg = (m ?? {}) as { role?: unknown; content?: unknown };
    return {
      role: typeof msg.role === "string" ? msg.role : "user",
      content: msg.content ?? "",
    };
  });
}

// Keep system turn(s) first — OpenAI chat semantics steer behavior from the system message, and a
// system turn placed after the user input is weakly honored (see the math-tutor debugging). A stable
// partition: systems first in their existing order, everything else after in its existing order. So
// adding a system turn (or flipping a role to system) snaps it to the top, in the wizard AND when a
// JSON edit is committed; reordering among same-role turns is preserved.
function sortSystemFirst<T extends { role: string }>(arr: T[]): T[] {
  return [...arr.filter((m) => m.role === "system"), ...arr.filter((m) => m.role !== "system")];
}

/** Edit an llm node's `messages` as a list of chat turns: each turn has a role dropdown and a text
 * box (with an "insert input ($in)" helper). A turn whose content is rich (a vision text+image list)
 * falls back to a per-turn JSON box so the multimodal path is preserved, not clobbered. Rows carry a
 * stable client-side key (never emitted) so reordering/editing doesn't remount the wrong row; the
 * editor re-syncs if `messages` is changed from elsewhere (the Code view / raw-config escape). */
function MessagesEditor({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const idRef = useRef(0);
  const [rows, setRows] = useState<MsgRow[]>(() =>
    toMessages(value).map((m) => ({ key: `m${idRef.current++}`, ...m })),
  );
  // Re-seed only when `messages` diverges from what we last emitted (an outside edit), so typing here
  // is never clobbered by our own round-trip.
  const lastEmitted = useRef(JSON.stringify(toMessages(value)));
  useEffect(() => {
    const incoming = JSON.stringify(toMessages(value));
    if (incoming !== lastEmitted.current) {
      lastEmitted.current = incoming;
      setRows(toMessages(value).map((m) => ({ key: `m${idRef.current++}`, ...m })));
    }
  }, [value]);

  const commit = (rowsNext: MsgRow[]) => {
    const next = sortSystemFirst(rowsNext); // system turn(s) always float to the top
    setRows(next);
    const msgs = next.map((r) => ({ role: r.role, content: r.content }));
    lastEmitted.current = JSON.stringify(msgs);
    onChange(msgs);
  };
  const setRow = (i: number, patch: Partial<MsgRow>) =>
    commit(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const removeRow = (i: number) => commit(rows.filter((_, j) => j !== i));
  const moveRow = (i: number, dir: -1 | 1) => {
    const j = i + dir;
    if (j < 0 || j >= rows.length) return;
    const next = rows.slice();
    [next[i], next[j]] = [next[j], next[i]];
    commit(next);
  };
  const addRow = () => commit([...rows, { key: `m${idRef.current++}`, role: "user", content: "" }]);

  // Wizard ⇄ JSON, mirroring the whole-editor's Visual ⇄ Code toggle: build the chat with role
  // dropdowns + text boxes, or see/edit the final `messages` array as JSON. Both read `value` and
  // write via `onChange`, so a JSON edit re-seeds the wizard rows (the re-sync effect above).
  const [view, setView] = useState<"wizard" | "json">("wizard");

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
          Messages *
        </span>
        <div className="flex items-center rounded-md border border-slate-700 p-0.5">
          {(
            [
              ["wizard", "Wizard"],
              ["json", "JSON"],
            ] as const
          ).map(([m, lbl]) => (
            <button
              key={m}
              type="button"
              onClick={() => setView(m)}
              className={`rounded px-2 py-0.5 text-[11px] font-medium transition-colors ${
                view === m ? "bg-blue-600 text-white" : "text-slate-400 hover:text-slate-200"
              }`}
            >
              {lbl}
            </button>
          ))}
        </div>
      </div>

      {view === "json" ? (
        <JsonField
          value={value}
          onChange={(v) =>
            onChange(Array.isArray(v) ? sortSystemFirst(v as { role: string }[]) : v)
          }
        />
      ) : (
        <>
          {rows.length === 0 ? (
            <p className="text-[11px] text-slate-600">No messages yet.</p>
          ) : (
            <div className="space-y-2">
              {rows.map((r, i) => (
                <MessageRow
                  key={r.key}
                  role={r.role}
                  content={r.content}
                  index={i}
                  total={rows.length}
                  onRole={(role) => setRow(i, { role })}
                  onContent={(content) => setRow(i, { content })}
                  onDelete={() => removeRow(i)}
                  onMove={(dir) => moveRow(i, dir)}
                />
              ))}
            </div>
          )}
          <Button className="!py-1 text-xs" onClick={addRow}>
            ＋ Add message
          </Button>
          <p className="text-[10px] text-slate-600">
            Use <span className="mono">$in</span> for this node's input.
          </p>
        </>
      )}
    </div>
  );
}

function MessageRow({
  role,
  content,
  index,
  total,
  onRole,
  onContent,
  onDelete,
  onMove,
}: {
  role: string;
  content: unknown;
  index: number;
  total: number;
  onRole: (role: string) => void;
  onContent: (content: unknown) => void;
  onDelete: () => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const taRef = useRef<HTMLTextAreaElement | null>(null);
  const isString = typeof content === "string";
  const roles = ROLE_OPTIONS.includes(role) ? ROLE_OPTIONS : [...ROLE_OPTIONS, role];

  const insertIn = () => {
    const ta = taRef.current;
    const text = typeof content === "string" ? content : "";
    const start = ta?.selectionStart ?? text.length;
    const end = ta?.selectionEnd ?? text.length;
    onContent(`${text.slice(0, start)}$in${text.slice(end)}`);
    // Restore the caret just after the inserted token once the controlled value updates.
    requestAnimationFrame(() => {
      if (!ta) return;
      const pos = start + 3;
      ta.focus();
      ta.setSelectionRange(pos, pos);
    });
  };

  const iconBtn =
    "flex h-5 w-5 items-center justify-center rounded text-slate-500 hover:bg-[#1d2433] hover:text-slate-200 disabled:opacity-30 disabled:hover:bg-transparent";

  return (
    <div className="space-y-1.5 rounded-md border border-slate-800 bg-[#0e131c] p-2">
      <div className="flex items-center gap-1.5">
        <select
          value={role}
          onChange={(e) => onRole(e.target.value)}
          className="rounded border border-slate-700 bg-[#11161f] px-1.5 py-0.5 text-xs text-slate-200 outline-none focus:border-blue-500"
        >
          {roles.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </select>
        <div className="ml-auto flex items-center gap-0.5">
          <button
            type="button"
            className={iconBtn}
            onClick={() => onMove(-1)}
            disabled={index === 0}
            title="Move up"
          >
            <ChevronUp size={14} aria-hidden />
          </button>
          <button
            type="button"
            className={iconBtn}
            onClick={() => onMove(1)}
            disabled={index === total - 1}
            title="Move down"
          >
            <ChevronDown size={14} aria-hidden />
          </button>
          <button type="button" className={iconBtn} onClick={onDelete} title="Remove message">
            <X size={14} aria-hidden />
          </button>
        </div>
      </div>
      {isString ? (
        <>
          <textarea
            ref={taRef}
            spellCheck={false}
            value={content as string}
            onChange={(e) => onContent(e.target.value)}
            placeholder="Message text… use $in for the input"
            className="mono h-20 w-full rounded border border-slate-700 bg-[#11161f] px-2 py-1 text-xs text-slate-100 outline-none focus:border-blue-500"
          />
          <button
            type="button"
            onClick={insertIn}
            className="text-[11px] text-blue-400 hover:underline"
          >
            ＋ Insert input ($in)
          </button>
        </>
      ) : (
        // Rich (vision) content: text+image_url parts — keep it editable as JSON so it isn't lost.
        <JsonField label="content (text + image parts)" value={content} onChange={onContent} />
      )}
    </div>
  );
}

// ── reference pickers (combobox over a live registry, free text still allowed) ─

/** A click-to-open, searchable dropdown over a live registry. Pick a known value from the list (with
 * a search box for long lists) or type a custom one (the "Use …" row — so an offline/empty registry
 * never blocks editing). The validator flags a dangling ref. */
function ListPicker({
  label,
  value,
  options,
  loading,
  hint,
  placeholder,
  onChange,
}: {
  label: string;
  value: string | undefined;
  options: string[];
  loading?: boolean;
  hint?: string;
  placeholder?: string;
  onChange: (v: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const boxRef = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  // Close on outside click / Escape (the popover floats inside the scrollable inspector); focus the
  // search box on open so you can type to filter immediately.
  useEffect(() => {
    if (!open) return;
    searchRef.current?.focus();
    const onDown = (e: PointerEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const q = query.trim().toLowerCase();
  const filtered = q ? options.filter((o) => o.toLowerCase().includes(q)) : options;
  const exact = options.some((o) => o.toLowerCase() === q);
  const select = (v: string) => {
    onChange(v);
    setOpen(false);
    setQuery("");
  };

  return (
    // A plain label element (not <Field>'s <label>) — a <label> must not wrap the trigger button +
    // popover, or every inner control inherits the label/option text as its accessible name.
    <div className="space-y-1">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
        {label}
      </span>
      <div ref={boxRef} className="relative">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          className="flex w-full items-center justify-between gap-2 rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-left text-sm outline-none hover:border-slate-500 focus:border-blue-500"
        >
          <span className={value ? "mono truncate text-slate-100" : "truncate text-slate-500"}>
            {value || placeholder || (loading ? "loading…" : "select…")}
          </span>
          <ChevronDown size={14} className="shrink-0 text-slate-500" aria-hidden />
        </button>
        {open && (
          <div className="absolute z-30 mt-1 w-full overflow-hidden rounded-md border border-slate-700 bg-[#11161f] shadow-xl">
            <div className="border-b border-slate-800 p-1.5">
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Search…"
                className="w-full rounded border border-slate-700 bg-[#0e131c] px-2 py-1 text-xs text-slate-100 outline-none focus:border-blue-500"
              />
            </div>
            <ul className="max-h-52 overflow-y-auto py-1">
              {loading && options.length === 0 ? (
                <li className="px-2.5 py-1.5 text-xs text-slate-500">loading…</li>
              ) : filtered.length === 0 && !q ? (
                <li className="px-2.5 py-1.5 text-xs text-slate-500">No options.</li>
              ) : (
                filtered.map((o) => (
                  <li key={o}>
                    <button
                      type="button"
                      onClick={() => select(o)}
                      className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-sm hover:bg-[#1d2433] ${
                        o === value ? "text-blue-300" : "text-slate-200"
                      }`}
                    >
                      <span className="mono truncate">{o}</span>
                      {o === value && (
                        <Check size={14} className="ml-auto shrink-0 text-blue-400" aria-hidden />
                      )}
                    </button>
                  </li>
                ))
              )}
              {/* A value not in the registry stays allowed (offline / not-yet-registered ref). */}
              {q && !exact && (
                <li className="border-t border-slate-800">
                  <button
                    type="button"
                    onClick={() => select(query.trim())}
                    className="flex w-full items-center gap-1 px-2.5 py-1.5 text-left text-sm text-slate-300 hover:bg-[#1d2433]"
                  >
                    Use “<span className="mono truncate">{query.trim()}</span>”
                  </button>
                </li>
              )}
            </ul>
          </div>
        )}
      </div>
      {hint && <span className="block text-[10px] text-slate-600">{hint}</span>}
    </div>
  );
}

/** The llm `model` field: pick a declared graph binding OR an inference-plane logical model. Picking
 * an inference model that isn't declared yet auto-creates the graph binding (§8.4) and selects it,
 * so you go from "registered upstream model" → "usable in this node" in one step. */
function ModelPicker({
  ir,
  nodeId,
  onChange,
}: {
  ir: IRDocument;
  nodeId: string;
  onChange: (ir: IRDocument) => void;
}) {
  const node = (ir.nodes ?? []).find((n) => n.id === nodeId);
  const config = (node?.config ?? {}) as Record<string, unknown>;
  const value = config.model as string | undefined;
  const declared = Object.keys(ir.models ?? {});

  const { data: models, isError } = useQuery({
    queryKey: ["inferenceModels"],
    queryFn: api.listModels,
    retry: false,
    staleTime: 30_000,
  });
  const inferenceIds = (models ?? []).map((m) => m.logicalId);
  const options = [...new Set([...declared, ...inferenceIds])];

  const pick = (v: string) => {
    let next = updateNodeConfig(ir, nodeId, { ...config, model: v });
    // Auto-declare a binding when picking an inference model not already in the graph.
    if (v && !(v in (ir.models ?? {}))) {
      const im = models?.find((m) => m.logicalId === v);
      if (im) {
        next = {
          ...next,
          models: {
            ...(next.models ?? {}),
            [v]: { binding: im.binding.binding as never, model: v },
          },
        };
      }
    }
    onChange(next);
  };

  const declaredHere = value ? value in (ir.models ?? {}) : false;
  const hint = !value
    ? undefined
    : declaredHere
      ? "✓ declared graph binding"
      : inferenceIds.includes(value)
        ? "will declare a binding from the inference plane"
        : isError
          ? "inference plane unreachable — declare this binding manually"
          : "not a declared binding (declare it or pick another)";

  return (
    <ListPicker
      label="model *"
      value={value}
      options={options}
      hint={hint}
      placeholder="pick a binding or inference model…"
      onChange={pick}
    />
  );
}

function McpServerPicker({
  value,
  onChange,
}: {
  value: string | undefined;
  onChange: (v: string) => void;
}) {
  const { data: servers, isLoading } = useQuery({
    queryKey: ["mcpServers"],
    queryFn: api.listMcpServers,
    retry: false,
    staleTime: 30_000,
  });
  return (
    <ListPicker
      label="server *"
      value={value}
      loading={isLoading}
      options={(servers ?? []).map((s) => s.name)}
      onChange={onChange}
    />
  );
}

function AgentPicker({
  value,
  onChange,
}: {
  value: string | undefined;
  onChange: (v: string) => void;
}) {
  const { data: agents, isLoading } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.listAgents({ limit: 200 }),
    retry: false,
    staleTime: 30_000,
  });
  return (
    <ListPicker
      label="agent *"
      value={value}
      loading={isLoading}
      options={(agents ?? []).map((a) => a.id)}
      hint="the saved agent this node composes (pin a version/contentHash too)"
      onChange={onChange}
    />
  );
}

// ── the unified Tool node panel (M22 D1 — one node, the kind is a picker) ──────────────────────────

const TOOL_KINDS: { kind: ToolKind; label: string; hint: string }[] = [
  { kind: "builtin", label: "Builtin", hint: "A tool theygent ships (echo, http_fetch, …)." },
  {
    kind: "rest",
    label: "REST",
    hint: "Call an HTTP API — optionally with a saved connection for server-side auth.",
  },
  { kind: "mcp", label: "MCP", hint: "A tool exposed by a connected MCP server." },
];

/** The one editor for tool + mcp_tool nodes (M22 D1). A kind picker (Builtin / REST / MCP) chooses
 * the binding shape; switching kind reseeds the node via `setToolKind` (which, for MCP, swaps the
 * underlying node type). The binding LIVES on the node config; `ir.tools` is the derived registry.
 * Advanced REST fields (headers/body/responseMap/…) stay in the Raw config editor below. */
function ToolNodePanel({
  ir,
  node,
  onChange,
}: {
  ir: IRDocument;
  node: IRNode;
  onChange: (ir: IRDocument) => void;
}) {
  const kind = toolKindOf(node);
  const config = (node.config ?? {}) as Record<string, unknown>;
  const builtinListId = useId();
  const setKey = (key: string, value: unknown) =>
    onChange(updateNodeConfig(ir, node.id, { ...config, [key]: value }));

  return (
    <>
      <Field label="Kind">
        <div className="grid grid-cols-3 gap-1">
          {TOOL_KINDS.map((k) => (
            <button
              key={k.kind}
              type="button"
              onClick={() => onChange(setToolKind(ir, node.id, k.kind))}
              className={`rounded-md border px-2 py-1.5 text-xs ${
                kind === k.kind
                  ? "border-blue-500 bg-blue-500/10 text-blue-200"
                  : "border-slate-700 text-slate-300 hover:border-slate-500"
              }`}
            >
              {k.label}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-slate-600">
          {TOOL_KINDS.find((k) => k.kind === kind)?.hint}
        </span>
      </Field>

      {kind === "builtin" && (
        <Field label="builtin *">
          <Input
            list={builtinListId}
            value={(config.tool as string) ?? ""}
            placeholder="echo / http_fetch / a registered tool"
            onChange={(e) => setKey("tool", e.target.value)}
          />
          <datalist id={builtinListId}>
            {["echo", "http_fetch"].map((o) => (
              <option key={o} value={o} />
            ))}
          </datalist>
        </Field>
      )}

      {kind === "rest" && (
        <>
          <Field label="URL template *">
            <Input
              value={(config.urlTemplate as string) ?? ""}
              placeholder="https://api.example.com/{path}"
              onChange={(e) => setKey("urlTemplate", e.target.value)}
            />
          </Field>
          <Field label="method">
            <Select
              value={(config.method as string) ?? "GET"}
              onChange={(e) => setKey("method", e.target.value)}
            >
              {["GET", "POST", "PUT", "PATCH", "DELETE"].map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </Select>
          </Field>
          <ConnectionSelect
            value={config.connection as string | undefined}
            onChange={(v) => setKey("connection", v)}
          />
          <JsonField
            label="parameterSchema (what the model fills in)"
            value={config.parameterSchema}
            onChange={(v) => setKey("parameterSchema", v)}
          />
        </>
      )}

      {kind === "mcp" && (
        <>
          <McpServerPicker
            value={config.server as string | undefined}
            onChange={(v) => setKey("server", v)}
          />
          <Field label="tool *">
            <Input
              value={(config.tool as string) ?? ""}
              placeholder="the tool name exposed by that server"
              onChange={(e) => setKey("tool", e.target.value)}
            />
          </Field>
        </>
      )}

      <Field label="description (what the model sees)">
        <Input
          value={(config.description as string) ?? ""}
          placeholder="one line describing when to call this tool"
          onChange={(e) => setKey("description", e.target.value)}
        />
      </Field>

      <p className="text-[10px] text-slate-600">
        Wire this node's violet <span className="text-violet-300">use</span> handle (top) into an
        llm's <span className="text-violet-300">tools</span> port to let the model call it. Advanced
        REST fields (headers, body, response map…) are in the Raw config below.
      </p>
    </>
  );
}

/** A dropdown of `http_auth` connections (M19 §1.1) for a REST tool — the IR stores the connection
 * **id**, never the secret (resolved server-side at step time). "none" leaves it auth-less. */
function ConnectionSelect({
  value,
  onChange,
}: {
  value: string | undefined;
  onChange: (v: string) => void;
}) {
  const { data: connections, isError } = useQuery({
    queryKey: ["connections"],
    queryFn: api.listConnections,
    retry: false,
    staleTime: 30_000,
  });
  const httpConns = (connections ?? []).filter((c) => c.kind === "http_auth");
  return (
    <Field label="connection (http auth)">
      <Select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">— none (no server-side auth) —</option>
        {httpConns.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </Select>
      {value ? (
        <span className="text-[10px] text-emerald-500">
          ✓ auth via {value} — resolved server-side; no secret in the IR
        </span>
      ) : isError ? (
        <span className="text-[10px] text-slate-600">connections unreachable</span>
      ) : null}
    </Field>
  );
}

function RawConfigEditor({
  value,
  onCommit,
}: {
  value: Record<string, unknown>;
  onCommit: (c: Record<string, unknown>) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details
      open={open}
      onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
      className="rounded-md border border-slate-800"
    >
      <summary className="cursor-pointer px-2.5 py-1.5 text-[11px] uppercase tracking-wide text-slate-500">
        Raw config (escape hatch)
      </summary>
      <div className="p-2">
        {open && (
          <JsonField
            label="config"
            value={value}
            onChange={(v) => {
              if (v && typeof v === "object" && !Array.isArray(v)) {
                onCommit(v as Record<string, unknown>);
              }
            }}
          />
        )}
      </div>
    </details>
  );
}

// ── graph-level panel (read-only models · editable tools/connections) ──────────

function GraphPanel({ ir }: { ir: IRDocument }) {
  const models = Object.entries(ir.models ?? {});
  return (
    <div className="flex h-full flex-col overflow-y-auto p-3 text-sm">
      <p className="mb-3 text-xs text-slate-600">
        Select a node or edge to edit it. Model bindings and tools are derived from the canvas
        (added as you place + wire nodes); connections are managed below.
      </p>
      <Section title="Model bindings">
        {models.length === 0 ? (
          <Empty>No models declared.</Empty>
        ) : (
          models.map(([key, m]) => (
            <div key={key} className="rounded border border-slate-800 px-2 py-1.5">
              <span className="mono text-xs text-slate-200">{key}</span>
              <div className="mono mt-0.5 text-[11px] text-slate-500">
                {(m as any).binding} · {(m as any).model}
              </div>
            </div>
          ))
        )}
      </Section>
      <ToolsRegistry ir={ir} />
      <ConnectionsPanel />
    </div>
  );
}

// ── tools registry (a READ-ONLY view of `ir.tools` — derived from the canvas tool nodes) ──────────

/** A one-line summary of a tool binding for the list row. */
function summarizeTool(t: ToolBinding): string {
  if (t.kind === "builtin") return t.ref;
  if (t.kind === "mcp") return [t.server ?? t.connection, t.tool].filter(Boolean).join(" · ");
  return t.connection ? `connection ${t.connection}` : "http";
}

/** The graph's tool registry (M22): `ir.tools` is DERIVED from the tool nodes on the canvas — like
 * `ir.models` fills in as you bind models — so this lists, read-only, every tool currently in the
 * agent with its kind. To add or remove a tool, drop/delete a tool node and wire it to an llm. */
function ToolsRegistry({ ir }: { ir: IRDocument }) {
  const tools = Object.entries((ir.tools ?? {}) as Record<string, ToolBinding>);
  return (
    <Section title="Tools">
      {tools.length === 0 ? (
        <Empty>No tools yet — drop a tool node and wire it to an llm.</Empty>
      ) : (
        tools.map(([key, t]) => (
          <div
            key={key}
            className="flex items-center gap-2 rounded border border-slate-800 px-2 py-1.5"
          >
            <span className="mono text-xs text-slate-200">{key}</span>
            <Badge>{t.kind}</Badge>
            <span className="mono ml-auto truncate text-[10px] text-slate-600">
              {summarizeTool(t)}
            </span>
          </div>
        ))
      )}
    </Section>
  );
}

// ── connections panel (M19 §2.10 — the graph-level tool/MCP auth surface) ──────────────────────────

/** List + create server-side `connection`s (the tool/MCP auth seam, §1.1). The ``secret`` is
 * WRITE-ONLY — typed here, sent to the encrypted store, and NEVER rendered back (a record carries
 * only ``hasSecret``). The inert "Browse hub" affordance is the M16 plug-point (registers the SAME
 * connection later — §2.4); it does nothing until M16. */
function ConnectionsPanel() {
  const qc = useQueryClient();
  const { data: connections, isError } = useQuery({
    queryKey: ["connections"],
    queryFn: api.listConnections,
    retry: false,
    staleTime: 30_000,
  });
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"http_auth" | "mcp_server">("http_auth");
  const [secret, setSecret] = useState("");
  const [configText, setConfigText] = useState("{}");
  const [err, setErr] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => {
      let config: Record<string, unknown> = {};
      try {
        config = JSON.parse(configText || "{}");
      } catch {
        throw new Error("config is not valid JSON");
      }
      return api.createConnection({ name, kind, config, secret: secret || undefined });
    },
    onSuccess: () => {
      setName("");
      setSecret(""); // the secret is write-only — clear it, never read it back
      setConfigText("{}");
      setErr(null);
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: (e) => setErr((e as Error).message),
  });

  return (
    <Section title="Connections (tool / MCP auth)">
      {isError ? (
        <Empty>Control plane unreachable.</Empty>
      ) : (connections ?? []).length === 0 ? (
        <Empty>No connections. Create one to give an http tool / MCP its auth.</Empty>
      ) : (
        (connections ?? []).map((c) => (
          <div key={c.id} className="rounded border border-slate-800 px-2 py-1.5">
            <div className="flex items-center justify-between">
              <span className="mono text-xs text-slate-200">{c.name}</span>
              <span className="mono text-[10px] text-slate-500">{c.kind}</span>
            </div>
            <div className="mono mt-0.5 flex items-center gap-2 text-[10px] text-slate-600">
              <span>{c.id}</span>
              {c.hasSecret && (
                <span className="flex items-center gap-1 text-emerald-600">
                  <Lock size={10} aria-hidden /> secret set
                </span>
              )}
            </div>
          </div>
        ))
      )}

      <div className="mt-1.5 flex gap-2">
        <Button className="!py-1 text-xs" onClick={() => setOpen((o) => !o)}>
          {open ? "Cancel" : "+ New connection"}
        </Button>
        <button
          type="button"
          disabled
          title="Coming with M16 — browse the MCP/model hub and install with one click"
          className="cursor-not-allowed rounded-md border border-slate-800 px-2 py-1 text-xs text-slate-600"
        >
          Browse hub (M16)
        </button>
      </div>

      {open && (
        <div className="mt-2 space-y-2 rounded border border-slate-800 p-2">
          <Field label="Name">
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Kind">
            <select
              className="w-full rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-xs text-slate-100"
              value={kind}
              onChange={(e) => setKind(e.target.value as "http_auth" | "mcp_server")}
            >
              <option value="http_auth">http_auth</option>
              <option value="mcp_server">mcp_server</option>
            </select>
          </Field>
          <Field label="Config (non-secret JSON)">
            <textarea
              spellCheck={false}
              className="mono h-20 w-full rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-xs text-slate-100"
              value={configText}
              onChange={(e) => setConfigText(e.target.value)}
            />
          </Field>
          <Field label="Secret (write-only — never shown again)">
            <Input
              type="password"
              value={secret}
              placeholder="api key / token (encrypted server-side)"
              onChange={(e) => setSecret(e.target.value)}
            />
          </Field>
          {err && <p className="text-[11px] text-red-400">{err}</p>}
          <Button
            variant="primary"
            className="!py-1 text-xs"
            onClick={() => create.mutate()}
            disabled={!name || create.isPending}
          >
            {create.isPending ? "Creating…" : "Create connection"}
          </Button>
        </div>
      )}
    </Section>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-4 space-y-1.5">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-500">
        {title}
      </div>
      {children}
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-xs text-slate-600">{children}</p>;
}
