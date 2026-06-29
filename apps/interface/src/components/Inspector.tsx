// The node/edge inspector (M15 §2.3): edit the selected node's `config` (schema-driven from the
// per-type JSON Schema) or the selected edge's `channel`/`condition`, with explicit Delete /
// Duplicate controls. Graph-level `models`/`tools` are exposed READ-ONLY (full binding/tool editors
// are out of scope, §2.3). When nothing is selected, the panel shows the graph's bindings.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type IRDocument, NODE_TYPES } from "@theygent/ir-types";
import { useEffect, useId, useRef, useState } from "react";
import {
  type Selection,
  type ViewBlock,
  deleteEdges,
  deleteNodes,
  duplicateNode,
  setHttpToolBinding,
  setNodeIcon,
  updateEdge,
  updateNodeConfig,
  updateNodeLabel,
} from "../adapter";
import { api } from "../lib/api";
import { ICON_CHOICES, defaultIconFor } from "../lib/icons";
import { Badge, Button, Field, Input, Select } from "./ui";

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

        {Object.keys(properties).length === 0 ? (
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
            if (node.type === "mcp_tool" && key === "server") {
              return (
                <McpServerPicker
                  key={`${node.id}:${key}`}
                  value={config.server as string | undefined}
                  onChange={(v) => setConfigKey("server", v)}
                />
              );
            }
            // M19 §2.10: the http tool's `tool` key + the connection that supplies its auth (resolved
            // server-side; the IR stores only the connection id, never the secret — §1.1).
            if (node.type === "tool" && key === "tool") {
              return (
                <ConnectionPicker
                  key={`${node.id}:${key}`}
                  ir={ir}
                  nodeId={node.id}
                  onChange={onChange}
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

/** Pick an emoji icon for a node, or reset to its type default. The override lives in the `view`
 * block, so it round-trips through save/load but never affects logic or version identity. */
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
  // The icon grid is collapsible and COLLAPSED by default — the header shows the current icon so the
  // picker stays unobtrusive until you want to change it. UI-only state, not persisted.
  const [open, setOpen] = useState(false);

  return (
    <Field label="Icon">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        title={open ? "Hide icon choices" : "Change icon"}
        className="flex w-full items-center gap-2 rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-xs text-slate-300 hover:border-slate-500"
      >
        <span className="text-base leading-none">{current}</span>
        <span className="text-slate-500">Change icon</span>
        <span
          className={`ml-auto text-slate-600 transition-transform ${open ? "rotate-90" : ""}`}
          aria-hidden
        >
          ▸
        </span>
      </button>
      {open && (
        <div className="mt-1.5">
          <div className="flex flex-wrap gap-1">
            {ICON_CHOICES.map((emoji) => {
              const active = current === emoji;
              return (
                <button
                  key={emoji}
                  type="button"
                  title={override === emoji ? "current icon" : `use ${emoji}`}
                  onClick={() => onChange(setNodeIcon(ir, nodeId, emoji))}
                  className={`flex h-7 w-7 items-center justify-center rounded border text-base leading-none transition-colors ${
                    active
                      ? "border-blue-500 bg-blue-950"
                      : "border-slate-700 bg-[#0e131c] hover:border-slate-500"
                  }`}
                >
                  {emoji}
                </button>
              );
            })}
          </div>
          <button
            type="button"
            onClick={() => onChange(setNodeIcon(ir, nodeId, null))}
            disabled={!override}
            className="mt-1 text-[10px] text-slate-500 hover:text-slate-300 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Reset to default ({fallback})
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

// ── the llm Tools panel (M21 — attach tools the model may autonomously call) ─────

/** Attach tools to an llm node (M21): pick which of the graph's ``ir.tools`` the model may call, the
 * tool_choice, and the iteration cap. The bindings (http/mcp/builtin) live in the graph's ``tools``;
 * this panel selects from them and writes config.tools/toolChoice/maxToolIterations. With nothing
 * selected the loop config is dropped, so a tools-less llm stays a clean single-shot node. */
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
  const selected = Array.isArray(config.tools) ? (config.tools as string[]) : [];
  const choice = typeof config.toolChoice === "string" ? (config.toolChoice as string) : "auto";
  const maxIter =
    typeof config.maxToolIterations === "number" ? (config.maxToolIterations as number) : 8;
  const toolKeys = Object.keys((ir.tools ?? {}) as Record<string, unknown>);

  const commit = (tools: string[], extra: Record<string, unknown> = {}) => {
    if (tools.length === 0) {
      const rest = { ...config }; // no tools → drop the loop config (clean single-shot llm)
      for (const k of ["tools", "toolChoice", "maxToolIterations"]) delete rest[k];
      onChange(updateNodeConfig(ir, nodeId, rest));
      return;
    }
    onChange(
      updateNodeConfig(ir, nodeId, {
        ...config,
        tools,
        toolChoice: choice,
        maxToolIterations: maxIter,
        ...extra,
      }),
    );
  };
  const toggle = (k: string) =>
    commit(selected.includes(k) ? selected.filter((x) => x !== k) : [...selected, k]);

  return (
    <div className="space-y-1.5">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
        Tools (the model may call these)
      </span>
      {toolKeys.length === 0 ? (
        <p className="text-[11px] text-slate-600">
          No tools defined in this graph. Add an http / mcp / builtin tool to the graph's{" "}
          <span className="mono">tools</span> (the Code view, for now), then select it here.
        </p>
      ) : (
        <div className="space-y-1">
          {toolKeys.map((k) => {
            const binding = (ir.tools as Record<string, { kind?: string }>)[k];
            return (
              <label
                key={k}
                className="flex cursor-pointer items-center gap-2 rounded px-1 py-0.5 text-xs hover:bg-[#1d2433]"
              >
                <input type="checkbox" checked={selected.includes(k)} onChange={() => toggle(k)} />
                <span className="mono text-slate-200">{k}</span>
                {binding?.kind && <Badge>{binding.kind}</Badge>}
              </label>
            );
          })}
        </div>
      )}
      {selected.length > 0 && (
        <div className="mt-1 space-y-2 rounded-md border border-slate-800 p-2">
          <label className="block space-y-1">
            <span className="block text-[10px] uppercase tracking-wide text-slate-500">
              tool choice
            </span>
            <Select
              value={choice}
              onChange={(e) => commit(selected, { toolChoice: e.target.value })}
            >
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
              onChange={(e) =>
                commit(selected, { maxToolIterations: Math.max(1, Number(e.target.value) || 1) })
              }
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
            ↑
          </button>
          <button
            type="button"
            className={iconBtn}
            onClick={() => onMove(1)}
            disabled={index === total - 1}
            title="Move down"
          >
            ↓
          </button>
          <button type="button" className={iconBtn} onClick={onDelete} title="Remove message">
            ✕
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
          <span className="shrink-0 text-slate-500" aria-hidden>
            ▾
          </span>
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
                      {o === value && <span className="ml-auto text-blue-400">✓</span>}
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

/** The http tool's `tool` key + the connection that supplies its auth (M19 §2.10/§1.1). Picking a
 * connection declares `ir.tools[key] = {kind:"http", connection}` — the IR stores the connection
 * **id**, NEVER the secret (resolved server-side at step time). Leaving it as a builtin (echo/
 * http_fetch) keeps the M6 path. */
function ConnectionPicker({
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
  const toolKey = (config.tool as string) ?? "";
  const listId = useId();
  const { data: connections, isError } = useQuery({
    queryKey: ["connections"],
    queryFn: api.listConnections,
    retry: false,
    staleTime: 30_000,
  });
  const httpConns = (connections ?? []).filter((c) => c.kind === "http_auth");
  const binding = ir.tools?.[toolKey] as { kind?: string; connection?: string } | undefined;
  const boundConn = binding?.kind === "http" ? binding.connection : undefined;
  const builtins = ["echo", "http_fetch"];

  return (
    <>
      <Field label="tool *">
        <Input
          list={listId}
          value={toolKey}
          placeholder="a builtin (echo/http_fetch) or a connection-backed key"
          onChange={(e) =>
            onChange(updateNodeConfig(ir, nodeId, { ...config, tool: e.target.value }))
          }
        />
        <datalist id={listId}>
          {[...new Set([...builtins, ...Object.keys(ir.tools ?? {})])].map((o) => (
            <option key={o} value={o} />
          ))}
        </datalist>
      </Field>
      <Field label="Connection (http auth)">
        <select
          className="w-full rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-xs text-slate-100 outline-none focus:border-blue-500"
          value={boundConn ?? ""}
          onChange={(e) => {
            const cid = e.target.value;
            if (cid) onChange(setHttpToolBinding(ir, nodeId, toolKey || "http_tool", cid));
          }}
        >
          <option value="">— none (builtin tool) —</option>
          {httpConns.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
        {boundConn ? (
          <span className="text-[10px] text-emerald-500">
            ✓ auth via {boundConn} — resolved server-side; no secret in the IR
          </span>
        ) : isError ? (
          <span className="text-[10px] text-slate-600">
            connections unreachable — this is a builtin tool
          </span>
        ) : (
          <span className="text-[10px] text-slate-600">
            pick a connection to make this an http tool with server-side auth
          </span>
        )}
      </Field>
    </>
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

// ── graph-level panel (read-only models/tools) ────────────────────────────────

function GraphPanel({ ir }: { ir: IRDocument }) {
  const models = Object.entries(ir.models ?? {});
  const tools = Object.entries(ir.tools ?? {});
  return (
    <div className="flex h-full flex-col overflow-y-auto p-3 text-sm">
      <p className="mb-3 text-xs text-slate-600">
        Select a node or edge to edit it. Graph-level bindings are shown read-only.
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
      <Section title="Tools">
        {tools.length === 0 ? (
          <Empty>No tools declared.</Empty>
        ) : (
          tools.map(([key, t]) => (
            <div key={key} className="rounded border border-slate-800 px-2 py-1.5">
              <span className="mono text-xs text-slate-200">{key}</span>
              <div className="mono mt-0.5 text-[11px] text-slate-500">{JSON.stringify(t)}</div>
            </div>
          ))
        )}
      </Section>
      <ConnectionsPanel />
    </div>
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
              {c.hasSecret && <span className="text-emerald-600">🔒 secret set</span>}
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
