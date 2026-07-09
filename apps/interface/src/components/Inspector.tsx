// The node/edge inspector: edit the selected node's `config` (schema-driven from the per-type JSON
// Schema) or the selected edge's `channel`/`condition`. Delete/duplicate live on the canvas (right-
// click menu + the Delete key), not here. With nothing selected, the graph-level panel shows `models`
// (read-only) and editable `tools` / `connections` — adding a tool here makes it selectable on every
// llm node.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type IRDocument, type Node as IRNode, NODE_TYPES } from "@theygent/ir-types";
import { Check, ChevronDown, ChevronUp, Diamond, Lock, Plus, Search, X } from "lucide-react";
import { Suspense, lazy, useEffect, useId, useRef, useState } from "react";
import {
  type GuardrailCheck,
  type Selection,
  type ToolBinding,
  type ToolKind,
  type ViewBlock,
  deleteEdges,
  setGuardrailCheck,
  setNodeIcon,
  setToolKind,
  toolKindOf,
  updateEdge,
  updateNodeConfig,
  updateNodeLabel,
} from "../adapter";
import { api } from "../lib/api";
import { DURABLE_ONLY } from "../lib/durable";
import { NodeIcon, defaultIconFor } from "../lib/icons";
import { NodeCodeEditor } from "./NodeCodeEditor";
import { Badge, Button, ErrorBanner, Field, Input, Select, Textarea } from "./ui";
import { Checkbox } from "./ui/checkbox";

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
  // Global per-node view switch, mirroring the whole-graph Visual ⇄ Code: the form ("Wizard") or this
  // exact node's JSON ("Code"). Defaults to Wizard; kept across node selections (UI-only state).
  const [view, setView] = useState<"wizard" | "code">("wizard");
  const node = (ir.nodes ?? []).find((n) => n.id === nodeId);
  if (!node) return <GraphPanel ir={ir} />;

  const spec = NODE_TYPES[node.type];
  const properties = (spec?.configSchema?.properties ?? {}) as Record<string, any>;
  const required = new Set<string>((spec?.configSchema?.required as string[]) ?? []);
  const config = (node.config ?? {}) as Record<string, unknown>;

  const setConfigKey = (key: string, value: unknown) => {
    onChange(updateNodeConfig(ir, node.id, { ...config, [key]: value }));
  };

  // Commit a whole-node JSON edit (the Code view): replace this node in place, and follow an id
  // rename so the panel stays on it. A rename that dangles edges surfaces in the graph's validation,
  // exactly as it would in the whole-graph Code view.
  const commitNode = (next: IRNode) => {
    onChange({ ...ir, nodes: (ir.nodes ?? []).map((n) => (n.id === node.id ? next : n)) });
    if (next?.id && next.id !== node.id) onSelect({ kind: "node", id: next.id });
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Extra left padding keeps this header clear of the panel's collapse chevron, which docks in
          the top-left corner (this panel is right-edge docked, so its control faces the canvas). */}
      <div className="shrink-0 border-b border-slate-800 py-2.5 pr-3 pl-9">
        <div className="flex items-center justify-between gap-2">
          <div className="flex min-w-0 items-center gap-2">
            <Badge tone={KIND_TONE[node.kind] ?? "slate"}>{node.kind}</Badge>
            <span className="mono truncate text-xs text-slate-400">{node.type}</span>
          </div>
          {/* Wizard ⇄ Code, per node — the form, or this exact node's JSON. */}
          <div className="flex shrink-0 items-center rounded-md border border-slate-700 p-0.5">
            {(
              [
                ["wizard", "Wizard"],
                ["code", "Code"],
              ] as const
            ).map(([m, lbl]) => (
              <button
                key={m}
                type="button"
                aria-pressed={view === m}
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
        <div className="mono mt-1 text-[11px] text-slate-600">{node.id}</div>
      </div>

      {view === "code" ? (
        <div className="min-h-0 flex-1">
          <NodeCodeEditor node={node} onCommit={commitNode} />
        </div>
      ) : (
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-3">
          <LabelIconField
            ir={ir}
            nodeId={node.id}
            nodeType={node.type}
            nodeLabel={node.label}
            onChange={onChange}
          />

          {DURABLE_ONLY.has(node.type) && (
            <div className="rounded-md border border-amber-200 bg-amber-50 px-2.5 py-2 text-[11px] leading-relaxed text-amber-800 dark:border-amber-700/50 dark:bg-amber-950/30 dark:text-amber-200/90">
              <span className="font-semibold">Durable-only.</span> This runs on the durable runtime
              — save the agent, then use “Run durably” (requires the server’s durable mode) or
              deploy it behind a schedule/webhook trigger. The plain interactive Run path can’t
              execute it.
            </div>
          )}

          {node.type === "tool" || node.type === "mcp_tool" ? (
            // tool + mcp_tool are ONE node with a kind picker (builtin / REST / MCP) — the
            // kind chooses the binding shape and (for mcp) the underlying node type.
            <ToolNodePanel ir={ir} node={node} onChange={onChange} />
          ) : node.type === "guardrail" ? (
            // A guardrail is a check picker: rule (inline) or model (an LLM judge). The choice sets the
            // node's kind, so it's edited through a dedicated panel rather than raw JSON.
            <GuardrailPanel ir={ir} node={node} onChange={onChange} />
          ) : Object.keys(properties).length === 0 ? (
            <p className="text-xs text-slate-600">This node type has no editable config.</p>
          ) : (
            Object.entries(properties).map(([key, schema]) => {
              // Autonomous tool-calling: surface tools / toolChoice / maxToolIterations via the dedicated
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
                    // Picking a different body clears the pin — versions/hashes are body-specific.
                    onChange={(v) =>
                      onChange(
                        updateNodeConfig(ir, node.id, {
                          ...config,
                          agent: v,
                          version: null,
                          contentHash: null,
                        }),
                      )
                    }
                  />
                );
              }
              // Pin the body by VERSION via a picker over the chosen agent's versions (sets `version`,
              // clears `contentHash`). Content-hash pinning stays available via the Code view.
              if (PINNED_BODY_TYPES.has(node.type) && key === "version") {
                return (
                  <BodyVersionPicker
                    key={`${node.id}:${key}`}
                    agentId={config.agent as string | undefined}
                    value={config.version as string | undefined}
                    onPick={(v) =>
                      onChange(
                        updateNodeConfig(ir, node.id, { ...config, version: v, contentHash: null }),
                      )
                    }
                  />
                );
              }
              // `contentHash` is the advanced alternate pin — hidden from the form, editable in the Code view.
              if (PINNED_BODY_TYPES.has(node.type) && key === "contentHash") {
                return null;
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
        </div>
      )}
    </div>
  );
}

// ── trigger panel (triggers are invocation bindings on the input node, never graph nodes) ─────────

/** The agent's triggers, surfaced on the `input` node. Triggers are NOT graph nodes — "run every
 * hour" is a `schedule` trigger, not a clock inside the durable workflow. Read-only here (list what
 * fires this saved agent); creating one is the deploy step (`POST /triggers`). */
function TriggerPanel({ agentId }: { agentId: string }) {
  const {
    data: triggers,
    isLoading,
    isError,
  } = useQuery({
    queryKey: ["triggers"],
    queryFn: api.listTriggers,
    retry: false,
    staleTime: 15_000,
  });
  const mine = (triggers ?? []).filter((t) => t.agent_id === agentId);
  return (
    <Field label="Triggers (how this agent is invoked)">
      {isLoading ? (
        <p className="text-[11px] text-slate-600">Loading…</p>
      ) : isError ? (
        <p className="text-[11px] text-slate-600">
          Couldn’t load triggers — control plane unreachable.
        </p>
      ) : mine.length === 0 ? (
        <p className="text-[11px] text-slate-600">
          No triggers. Save the agent, then add a schedule, webhook, or HTTP trigger to run it
          unattended — triggers are how a saved agent is invoked, not graph nodes.
        </p>
      ) : (
        <div className="space-y-1">
          {mine.map((t) => (
            <div
              key={t.id}
              className="mono flex items-center justify-between rounded border border-slate-800 px-2 py-1 text-[11px]"
            >
              <span className="text-slate-300">{t.kind}</span>
              <span
                className={t.enabled ? "text-emerald-600 dark:text-emerald-400" : "text-slate-600"}
              >
                {t.enabled ? "enabled" : "disabled"}
              </span>
            </div>
          ))}
        </div>
      )}
    </Field>
  );
}

// ── label + icon field (the icon is a `view`-only display change — never hashed) ─────────────────

/** The node's label and its icon, edited together on one row: the icon button on the left opens an
 * inline picker (search the full Lucide set) and the label input fills the rest. The icon override is
 * a Lucide icon NAME stored in the `view` block, so it round-trips through save/load but never affects
 * logic or version identity. */
function LabelIconField({
  ir,
  nodeId,
  nodeType,
  nodeLabel,
  onChange,
}: {
  ir: IRDocument;
  nodeId: string;
  nodeType: string;
  nodeLabel?: string | null;
  onChange: (ir: IRDocument) => void;
}) {
  const override = (ir.view as ViewBlock | undefined)?.nodes?.[nodeId]?.icon;
  const fallback = defaultIconFor(nodeType);
  const current = override && override.length > 0 ? override : fallback;
  // Clicking the icon opens the picker inline, below the row — no separate "change icon" control.
  // `query` searches the full Lucide set. UI-only state.
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  // The picker opens from the icon and has no explicit close button, so close it on an outside click
  // or Escape (clicking the icon itself still toggles it).
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <FieldGroup label="Label">
      <div ref={ref}>
        {/* The icon and the label are ONE control: a single bordered box with the icon as a leading
            button (its own divider) and the label input filling the rest — so they read as connected,
            not as two separate fields. Clicking the icon opens the picker; typing edits the label. */}
        <div className="flex items-stretch overflow-hidden rounded-md border border-slate-700 bg-[var(--c-surface)] transition-colors focus-within:border-blue-500">
          <button
            type="button"
            onClick={() => setOpen((o) => !o)}
            title={open ? "Hide icon choices" : "Change icon"}
            aria-label="Change icon"
            aria-expanded={open}
            className="flex w-9 shrink-0 items-center justify-center border-r border-slate-700 text-slate-300 transition-colors hover:bg-[var(--c-hover)] hover:text-slate-100"
          >
            <NodeIcon name={current} size={16} />
          </button>
          <input
            value={nodeLabel ?? ""}
            placeholder={nodeId}
            aria-label="Node label"
            onChange={(e) => onChange(updateNodeLabel(ir, nodeId, e.target.value))}
            className="min-w-0 flex-1 bg-transparent px-2.5 py-1.5 text-sm text-slate-100 outline-none placeholder:text-slate-600"
          />
        </div>
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
                aria-label="Search icons"
                className="w-full rounded-md border border-slate-700 bg-[var(--c-surface)] py-1 pr-2 pl-7 text-xs text-slate-100 outline-none focus:border-blue-500"
              />
            </div>
            {/* The grid (and its full-icon set) is lazy — show a tiny placeholder while it loads. */}
            <Suspense
              fallback={<p className="mt-1.5 text-[11px] text-slate-600">Loading icons…</p>}
            >
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
      </div>
    </FieldGroup>
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
      <div className="border-b border-slate-800 py-2.5 pr-3 pl-9">
        <span className="text-xs font-medium text-slate-300">Edge</span>
        <div className="mono mt-1 text-[11px] text-slate-600">{edge.id}</div>
        <div className="mt-2">
          <Button
            variant="danger"
            className="h-7 text-xs"
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

        <FieldGroup label="Channel">
          {channel === "tool" ? (
            // A `tool` (capability) edge's channel is DERIVED from the handles it joins (a tool
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
                    className="h-7 text-xs"
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
        </FieldGroup>

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
  // Pydantic emits an Optional primitive as `anyOf: [{type: X}, {type: "null"}]` — unwrap it so a
  // nullable string/number (e.g. a human node's prompt/timeout) gets a real input, not a raw JSON
  // textarea. Only a single non-null primitive variant unwraps; real unions stay complex.
  if (t === undefined && Array.isArray(schema?.anyOf)) {
    const variants = (schema.anyOf as any[]).filter((v) => v?.type !== "null");
    if (variants.length === 1) return primitiveType(variants[0]);
  }
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
  // Humanize camelCase schema keys before the Field's uppercase styling flattens the word
  // boundaries ("maxToolIterations" → "max tool iterations", not "MAXTOOLITERATIONS").
  const pretty = name.replace(/([a-z0-9])([A-Z])/g, "$1 $2").toLowerCase();
  const label = required ? `${pretty} *` : pretty;

  // A Literal field (modality, policy, source, …) is a JSON-Schema `enum` — render a dropdown so the
  // editor offers the exact valid values.
  const enumValues = Array.isArray(schema?.enum) ? (schema.enum as unknown[]) : null;
  if (enumValues) {
    return (
      <Field label={label}>
        <Select
          className="!text-xs"
          value={value === null || value === undefined ? "" : String(value)}
          onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
        >
          {(value === undefined || value === null) && <option value="">—</option>}
          {enumValues.map((v) => (
            <option key={String(v)} value={String(v)}>
              {String(v)}
            </option>
          ))}
        </Select>
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
      <label className="flex items-center gap-2 text-sm text-foreground">
        <Checkbox checked={Boolean(value)} onCheckedChange={(next) => onChange(next === true)} />
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
        className={`mono h-28 w-full rounded-md border bg-[var(--c-surface)] px-2.5 py-1.5 text-xs text-slate-100 outline-none ${
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
      {error && <span className="text-[11px] text-red-600 dark:text-red-400">{error}</span>}
    </>
  );
  return label ? <Field label={label}>{editor}</Field> : <div className="space-y-1">{editor}</div>;
}

// ── the llm Tools panel (the tools wired into this llm, the model may call them) ─────

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

/** Show the tools this llm may autonomously call: the tool/mcp_tool nodes wired into its
 * `tools` port (read-only — wire/unwire on the canvas), plus the tool_choice + iteration cap. Any
 * legacy `config.tools` keys (ir.tools refs predating capability wiring) are listed too. The loop
 * runs whenever the llm has ANY tool (wired or legacy). */
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
            <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
              tool choice
            </span>
            <Select value={choice} onChange={(e) => set({ toolChoice: e.target.value })}>
              <option value="auto">auto — model decides</option>
              <option value="required">required — must call a tool</option>
              <option value="none">none — never call</option>
            </Select>
          </label>
          <label className="block space-y-1">
            <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
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
 * editor re-syncs if `messages` is changed from elsewhere (the node's Code view). */
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

  // The chat builder: a role dropdown + text box per turn. Editing the raw `messages` array as JSON
  // now lives in the node-level Code view, so there's no per-field JSON toggle here. A rich (vision)
  // turn still falls back to a per-turn JSON box inside its own row.
  return (
    <div className="space-y-1.5">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
        Messages *
      </span>

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
      <Button className="h-7 text-xs" onClick={addRow}>
        <Plus size={14} aria-hidden /> Add message
      </Button>
      <p className="text-[10px] text-slate-600">
        Use <span className="mono">$in</span> for this node's input.
      </p>
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
    "flex h-5 w-5 items-center justify-center rounded text-slate-500 hover:bg-[var(--c-hover)] hover:text-slate-200 disabled:opacity-30 disabled:hover:bg-transparent";

  return (
    <div className="space-y-1.5 rounded-md border border-slate-800 bg-[var(--c-surface)] p-2">
      <div className="flex items-center gap-1.5">
        <Select
          value={role}
          aria-label="Message role"
          onChange={(e) => onRole(e.target.value)}
          className="!w-auto rounded !bg-[var(--c-surface-2)] !px-1.5 !py-0.5 !text-xs"
        >
          {roles.map((r) => (
            <option key={r} value={r}>
              {r}
            </option>
          ))}
        </Select>
        <div className="ml-auto flex items-center gap-0.5">
          <button
            type="button"
            className={iconBtn}
            onClick={() => onMove(-1)}
            disabled={index === 0}
            title="Move up"
            aria-label="Move message up"
          >
            <ChevronUp size={14} aria-hidden />
          </button>
          <button
            type="button"
            className={iconBtn}
            onClick={() => onMove(1)}
            disabled={index === total - 1}
            title="Move down"
            aria-label="Move message down"
          >
            <ChevronDown size={14} aria-hidden />
          </button>
          <button
            type="button"
            className={iconBtn}
            onClick={onDelete}
            title="Remove message"
            aria-label="Remove message"
          >
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
            className="mono h-20 w-full rounded border border-slate-700 bg-[var(--c-surface-2)] px-2 py-1 text-xs text-slate-100 outline-none focus:border-blue-500"
          />
          <button
            type="button"
            onClick={insertIn}
            className="inline-flex items-center gap-1 text-[11px] text-blue-600 hover:underline dark:text-blue-400"
          >
            <Plus size={12} aria-hidden /> Insert input ($in)
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
  // Arrow-key highlight over the filtered options (-1 = nothing highlighted yet).
  const [active, setActive] = useState(-1);
  const listboxId = useId();
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
    setActive(-1);
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
          aria-expanded={open}
          aria-haspopup="listbox"
          onClick={() => {
            setActive(-1);
            setOpen((o) => !o);
          }}
          className="flex w-full items-center justify-between gap-2 rounded-md border border-slate-700 bg-[var(--c-surface)] px-2.5 py-1.5 text-left text-sm outline-none hover:border-slate-500 focus:border-blue-500"
        >
          <span className={value ? "mono truncate text-slate-100" : "truncate text-slate-500"}>
            {value || placeholder || (loading ? "Loading…" : "Select…")}
          </span>
          <ChevronDown size={14} className="shrink-0 text-slate-500" aria-hidden />
        </button>
        {open && (
          <div className="absolute z-30 mt-1 w-full overflow-hidden rounded-md border border-slate-700 bg-[var(--c-surface-2)] shadow-xl">
            <div className="border-b border-slate-800 p-1.5">
              <input
                ref={searchRef}
                value={query}
                onChange={(e) => {
                  setQuery(e.target.value);
                  setActive(-1);
                }}
                onKeyDown={(e) => {
                  if (e.key === "ArrowDown") {
                    e.preventDefault();
                    setActive((a) => Math.min(a + 1, filtered.length - 1));
                  } else if (e.key === "ArrowUp") {
                    e.preventDefault();
                    setActive((a) => Math.max(a - 1, 0));
                  } else if (e.key === "Enter") {
                    e.preventDefault();
                    if (active >= 0 && active < filtered.length) select(filtered[active]);
                    else if (q && !exact) select(query.trim());
                  }
                }}
                placeholder="Search…"
                aria-label={label}
                aria-activedescendant={active >= 0 ? `${listboxId}-${active}` : undefined}
                className="w-full rounded border border-slate-700 bg-[var(--c-surface)] px-2 py-1 text-xs text-slate-100 outline-none focus:border-blue-500"
              />
            </div>
            {/* biome-ignore lint/a11y/useFocusableInteractive: focus stays on the search box — aria-activedescendant drives the highlight */}
            <ul
              id={listboxId}
              // biome-ignore lint/a11y/noNoninteractiveElementToInteractiveRole: the list IS the popover's listbox
              // biome-ignore lint/a11y/useSemanticElements: a native <select> can't host the search box + custom-value row
              role="listbox"
              aria-label={label}
              className="max-h-52 overflow-y-auto py-1"
            >
              {loading && options.length === 0 ? (
                <li className="px-2.5 py-1.5 text-xs text-slate-500">Loading…</li>
              ) : filtered.length === 0 && !q ? (
                <li className="px-2.5 py-1.5 text-xs text-slate-500">No options.</li>
              ) : (
                filtered.map((o, i) => (
                  <li key={o}>
                    <button
                      type="button"
                      id={`${listboxId}-${i}`}
                      // biome-ignore lint/a11y/useSemanticElements: a combobox option row, not a native <option>
                      role="option"
                      aria-selected={o === value}
                      tabIndex={-1}
                      onClick={() => select(o)}
                      className={`flex w-full items-center gap-2 px-2.5 py-1.5 text-left text-sm hover:bg-[var(--c-hover)] ${
                        i === active ? "bg-[var(--c-hover)]" : ""
                      } ${o === value ? "text-blue-700 dark:text-blue-300" : "text-slate-200"}`}
                    >
                      <span className="mono truncate">{o}</span>
                      {o === value && (
                        <Check
                          size={14}
                          className="ml-auto shrink-0 text-blue-600 dark:text-blue-400"
                          aria-hidden
                        />
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
                    className="flex w-full items-center gap-1 px-2.5 py-1.5 text-left text-sm text-slate-300 hover:bg-[var(--c-hover)]"
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
 * an inference model that isn't declared yet auto-creates the graph binding and selects it,
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
      hint="the saved agent this node composes — then pin a version below"
      onChange={onChange}
    />
  );
}

/** Pin the body by version: lists the chosen agent's versions (newest first) and writes the picked
 * one into `config.version`. Disabled until a body agent is chosen; degrades to free text if the
 * registry is unreachable so an offline author can still type a version. */
function BodyVersionPicker({
  agentId,
  value,
  onPick,
}: {
  agentId: string | undefined;
  value: string | undefined;
  onPick: (v: string) => void;
}) {
  const { data: detail, isLoading } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => api.getAgent(agentId as string),
    enabled: Boolean(agentId),
    retry: false,
    staleTime: 30_000,
  });
  const versions = (detail?.versions ?? []).map((v) => v.version);
  return (
    <ListPicker
      label="body version *"
      value={value}
      loading={Boolean(agentId) && isLoading}
      options={versions}
      placeholder={agentId ? "pick a version…" : "pick a body agent first"}
      hint={
        agentId
          ? "the immutable version of the body this node runs"
          : "select the body agent above, then pin a version"
      }
      onChange={onPick}
    />
  );
}

// ── the unified Tool node panel (one node, the kind is a picker) ──────────────────────────────────

const TOOL_KINDS: { kind: ToolKind; label: string; hint: string }[] = [
  { kind: "builtin", label: "Builtin", hint: "A tool theygent ships (echo, http_fetch, …)." },
  {
    kind: "rest",
    label: "REST",
    hint: "Call an HTTP API — optionally with a saved connection for server-side auth.",
  },
  { kind: "mcp", label: "MCP", hint: "A tool exposed by a connected MCP server." },
];

/** The one editor for tool + mcp_tool nodes. A kind picker (Builtin / REST / MCP) chooses
 * the binding shape; switching kind reseeds the node via `setToolKind` (which, for MCP, swaps the
 * underlying node type). The binding LIVES on the node config; `ir.tools` is the derived registry.
 * Advanced REST fields (headers/body/responseMap/…) stay in the node's Code view. */
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
      <FieldGroup label="Kind">
        <div className="grid grid-cols-3 gap-1">
          {TOOL_KINDS.map((k) => (
            <button
              key={k.kind}
              type="button"
              aria-pressed={kind === k.kind}
              onClick={() => onChange(setToolKind(ir, node.id, k.kind))}
              className={`rounded-md border px-2 py-1.5 text-xs ${
                kind === k.kind
                  ? "border-blue-500 bg-blue-500/10 text-blue-700 dark:text-blue-200"
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
      </FieldGroup>

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
          {/* An mcp_tool reaches its server by EXACTLY ONE of a registered server (stdio, from the
              MCP page) or an mcp_server connection (stdio/http, with server-side auth). Picking one
              clears the other so the "exactly one" rule can't be violated from the UI. */}
          <McpServerPicker
            value={config.server as string | undefined}
            onChange={(v) =>
              onChange(updateNodeConfig(ir, node.id, { ...config, server: v, connection: null }))
            }
          />
          <ConnectionSelect
            connectionKind="mcp_server"
            label="or a connection (mcp server)"
            emptyLabel="— use the registered server above —"
            value={config.connection as string | undefined}
            onChange={(v) =>
              onChange(updateNodeConfig(ir, node.id, { ...config, connection: v, server: null }))
            }
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
        Wire this node's violet <span className="text-violet-600 dark:text-violet-300">use</span>{" "}
        handle (top) into an llm's{" "}
        <span className="text-violet-600 dark:text-violet-300">tools</span> port to let the model
        call it. Advanced REST fields (headers, body, response map…) are in the Code view.
      </p>
    </>
  );
}

/** A dropdown of `http_auth` connections for a REST tool — the IR stores the connection
 * **id**, never the secret (resolved server-side at step time). "none" leaves it auth-less. */
function ConnectionSelect({
  value,
  onChange,
  connectionKind = "http_auth",
  label = "connection (http auth)",
  emptyLabel = "— none (no server-side auth) —",
}: {
  value: string | undefined;
  onChange: (v: string) => void;
  connectionKind?: "http_auth" | "mcp_server";
  label?: string;
  emptyLabel?: string;
}) {
  const { data: connections, isError } = useQuery({
    queryKey: ["connections"],
    queryFn: api.listConnections,
    retry: false,
    staleTime: 30_000,
  });
  const conns = (connections ?? []).filter((c) => c.kind === connectionKind);
  return (
    <Field label={label}>
      <Select value={value ?? ""} onChange={(e) => onChange(e.target.value)}>
        <option value="">{emptyLabel}</option>
        {conns.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
          </option>
        ))}
      </Select>
      {value ? (
        <span className="text-[10px] text-emerald-600 dark:text-emerald-400">
          ✓ via {value} — resolved server-side; no secret in the IR
        </span>
      ) : isError ? (
        <span className="text-[10px] text-slate-600">connections unreachable</span>
      ) : null}
    </Field>
  );
}

// ── the guardrail panel (rule vs model check — the node's kind follows the choice) ─────────────────

// The check-type + rule-kind option lists are DERIVED from the generated schema, never hardcoded, so
// a rule kind added in the IR shows up here after a `generate`.
const GUARDRAIL_DEFS = (
  NODE_TYPES.guardrail?.configSchema as { $defs?: Record<string, any> } | undefined
)?.$defs;
const CHECK_TYPES: string[] = GUARDRAIL_DEFS?.GuardrailCheck?.properties?.type?.enum ?? [
  "rule",
  "model",
];
const RULE_KINDS: string[] = GUARDRAIL_DEFS?.GuardrailRule?.properties?.kind?.enum ?? [
  "regex",
  "length",
  "json_schema",
  "allow",
  "deny",
  "pii",
];

// What each rule PASSES on (a guardrail blocks by taking its `block` port; `pass` carries the input
// through). Shown as a hint under the rule picker.
const RULE_HINTS: Record<string, string> = {
  regex: "matches (or, in deny mode, does NOT match) a regular expression",
  length: "the text length is within min/max",
  allow: "the value is one of an allowed list",
  deny: "the value is NOT in a blocked list",
  json_schema: "the value is an object carrying all the required keys",
  pii: "no PII pattern matches (blocks emails / SSNs / card-like numbers)",
};

function numOr(v: unknown): string {
  return typeof v === "number" ? String(v) : "";
}

/** The one editor for a `guardrail` node. A check-type picker (rule / model) chooses the backend and
 * — through `setGuardrailCheck` — STAMPS the node's kind (rule ⇒ orchestration, model ⇒ activity),
 * so the author never touches kind directly. Rule kinds get structured spec fields; a model check
 * gets a judge-model picker + prompt + pass-on. Unusual spec keys stay reachable via the Code view. */
function GuardrailPanel({
  ir,
  node,
  onChange,
}: {
  ir: IRDocument;
  node: IRNode;
  onChange: (ir: IRDocument) => void;
}) {
  const config = (node.config ?? {}) as Record<string, unknown>;
  const check = (config.check ?? null) as GuardrailCheck | null;
  const onBlock = (config.onBlock ?? {}) as Record<string, unknown>;
  const checkType = check?.type;

  // Preserve the last-entered rule/model sub-config across a type toggle within the session, so
  // flipping rule↔model and back doesn't wipe what you typed. Only the ACTIVE sub-config is written
  // into the IR (the backend reads only the one matching `check.type`).
  const stash = useRef<{
    rule: NonNullable<GuardrailCheck["rule"]>;
    model: NonNullable<GuardrailCheck["model"]>;
  }>({ rule: { kind: "pii", spec: {} }, model: { model: "", prompt: "", passOn: "yes" } });
  if (check?.type === "rule" && check.rule) stash.current.rule = check.rule;
  if (check?.type === "model" && check.model) stash.current.model = check.model;

  const write = (next: GuardrailCheck) => onChange(setGuardrailCheck(ir, node.id, next, onBlock));

  const pickType = (t: string) => {
    if (t === checkType) return;
    write(
      t === "model"
        ? { type: "model", model: stash.current.model }
        : { type: "rule", rule: stash.current.rule },
    );
  };

  const rule = check?.type === "rule" ? (check.rule ?? { kind: "pii", spec: {} }) : null;
  const spec = (rule?.spec ?? {}) as Record<string, unknown>;
  const setRule = (patch: Partial<NonNullable<GuardrailCheck["rule"]>>) =>
    write({ type: "rule", rule: { kind: rule?.kind ?? "pii", spec, ...patch } });
  const setSpec = (patch: Record<string, unknown>) => {
    const merged: Record<string, unknown> = { ...spec, ...patch };
    for (const k of Object.keys(merged)) if (merged[k] === undefined) delete merged[k];
    write({ type: "rule", rule: { kind: rule?.kind ?? "pii", spec: merged } });
  };

  const model = check?.type === "model" ? (check.model ?? stash.current.model) : null;
  const setModel = (patch: Partial<NonNullable<GuardrailCheck["model"]>>) =>
    write({
      type: "model",
      model: {
        model: model?.model ?? "",
        prompt: model?.prompt ?? "",
        passOn: model?.passOn ?? "yes",
        ...patch,
      },
    });

  return (
    <>
      <FieldGroup label="Check">
        <div className="grid grid-cols-2 gap-1">
          {CHECK_TYPES.map((t) => (
            <button
              key={t}
              type="button"
              aria-pressed={checkType === t}
              onClick={() => pickType(t)}
              className={`rounded-md border px-2 py-1.5 text-xs capitalize ${
                checkType === t
                  ? "border-blue-500 bg-blue-500/10 text-blue-700 dark:text-blue-200"
                  : "border-slate-700 text-slate-300 hover:border-slate-500"
              }`}
            >
              {t}
            </button>
          ))}
        </div>
        <span className="text-[10px] text-slate-600">
          Rule = an inline check, no model call. Model = an LLM judge decides (a real inference
          call).
        </span>
      </FieldGroup>

      {checkType === undefined && (
        <p className="text-xs text-amber-700 dark:text-amber-300/80">
          Pick a check type to configure this guardrail.
        </p>
      )}

      {checkType === "rule" && (
        <>
          <Field label="Rule">
            <Select value={rule?.kind ?? "pii"} onChange={(e) => setRule({ kind: e.target.value })}>
              {RULE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </Select>
            <span className="text-[10px] text-slate-600">
              passes when {RULE_HINTS[rule?.kind ?? "pii"] ?? "the check holds"}
            </span>
          </Field>
          <RuleSpecFields kind={rule?.kind ?? "pii"} spec={spec} setSpec={setSpec} />
        </>
      )}

      {checkType === "model" && (
        <>
          <GuardrailModelField
            ir={ir}
            nodeId={node.id}
            value={model?.model}
            model={model}
            onBlock={onBlock}
            onChange={onChange}
          />
          <Field label="prompt *">
            <textarea
              spellCheck={false}
              value={model?.prompt ?? ""}
              placeholder="e.g. Is this request about booking a table? Answer yes or no."
              onChange={(e) => setModel({ prompt: e.target.value })}
              className="mono h-20 w-full rounded-md border border-slate-700 bg-[var(--c-surface)] px-2.5 py-1.5 text-xs text-slate-100 outline-none focus:border-blue-500"
            />
            <span className="text-[10px] text-slate-600">
              The judge question — keep it a terse yes/no. The check passes when the answer starts
              with “pass on”.
            </span>
          </Field>
          <Field label="pass on">
            <Input
              value={model?.passOn ?? "yes"}
              placeholder="yes"
              onChange={(e) => setModel({ passOn: e.target.value })}
            />
          </Field>
        </>
      )}

      <JsonField
        label="onBlock (payload returned when the check blocks)"
        value={config.onBlock}
        onChange={(v) =>
          onChange(
            updateNodeConfig(ir, node.id, {
              ...config,
              onBlock: v && typeof v === "object" && !Array.isArray(v) ? v : {},
            }),
          )
        }
      />

      <p className="text-[10px] text-slate-600">
        The <span className="text-slate-300">pass</span> port carries the input through;{" "}
        <span className="text-slate-300">block</span> carries the onBlock payload. Unusual spec keys
        are editable in the Code view.
      </p>
    </>
  );
}

/** The structured spec fields for a rule check, keyed by the rule kind — the exact knobs the runtime
 * reads (pattern+mode, min/max, an allow/deny list, required keys, optional PII patterns). */
function RuleSpecFields({
  kind,
  spec,
  setSpec,
}: {
  kind: string;
  spec: Record<string, unknown>;
  setSpec: (patch: Record<string, unknown>) => void;
}) {
  if (kind === "regex") {
    return (
      <>
        <Field label="pattern *">
          <Input
            value={(spec.pattern as string) ?? ""}
            placeholder="e.g. (?i)ignore previous"
            onChange={(e) => setSpec({ pattern: e.target.value })}
          />
        </Field>
        <Field label="mode">
          <Select
            value={(spec.mode as string) ?? "allow"}
            onChange={(e) => setSpec({ mode: e.target.value })}
          >
            <option value="allow">allow — pass when it matches</option>
            <option value="deny">deny — pass when it does NOT match</option>
          </Select>
        </Field>
      </>
    );
  }
  if (kind === "length") {
    return (
      <div className="grid grid-cols-2 gap-2">
        <Field label="min">
          <Input
            type="number"
            value={numOr(spec.min)}
            onChange={(e) =>
              setSpec({ min: e.target.value === "" ? undefined : Number(e.target.value) })
            }
          />
        </Field>
        <Field label="max">
          <Input
            type="number"
            value={numOr(spec.max)}
            onChange={(e) =>
              setSpec({ max: e.target.value === "" ? undefined : Number(e.target.value) })
            }
          />
        </Field>
      </div>
    );
  }
  if (kind === "allow" || kind === "deny") {
    return (
      <StringListField
        label={kind === "allow" ? "allowed values *" : "blocked values *"}
        value={spec.values}
        placeholder="one value per line"
        onChange={(v) => setSpec({ values: v })}
      />
    );
  }
  if (kind === "json_schema") {
    return (
      <StringListField
        label="required keys"
        value={spec.required}
        placeholder="one key per line"
        onChange={(v) => setSpec({ required: v })}
      />
    );
  }
  if (kind === "pii") {
    return (
      <StringListField
        label="patterns (optional)"
        value={spec.patterns}
        placeholder="leave empty for built-in email / SSN / card patterns"
        onChange={(v) => setSpec({ patterns: v })}
      />
    );
  }
  return null;
}

/** A newline-separated list edited as a string[]. Keeps a local raw buffer (so blank lines / spaces
 * type naturally) and re-syncs only when the value changes from elsewhere, mirroring the message
 * editor's buffer discipline. */
function StringListField({
  label,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  value: unknown;
  placeholder?: string;
  onChange: (v: string[]) => void;
}) {
  const initial = Array.isArray(value) ? (value as string[]).join("\n") : "";
  const [text, setText] = useState(initial);
  const lastEmitted = useRef(initial);
  useEffect(() => {
    const incoming = Array.isArray(value) ? (value as string[]).join("\n") : "";
    if (incoming !== lastEmitted.current) {
      lastEmitted.current = incoming;
      setText(incoming);
    }
  }, [value]);
  const emit = (raw: string) => {
    setText(raw);
    const arr = raw
      .split("\n")
      .map((s) => s.trim())
      .filter(Boolean);
    lastEmitted.current = arr.join("\n");
    onChange(arr);
  };
  return (
    <Field label={label}>
      <textarea
        spellCheck={false}
        value={text}
        placeholder={placeholder}
        onChange={(e) => emit(e.target.value)}
        className="mono h-20 w-full rounded-md border border-slate-700 bg-[var(--c-surface)] px-2.5 py-1.5 text-xs text-slate-100 outline-none focus:border-blue-500"
      />
      <span className="text-[10px] text-slate-600">one per line</span>
    </Field>
  );
}

/** The model guardrail's judge-model picker. Reuses the same live registry the llm node's model
 * picker uses (declared graph bindings ∪ reachable inference-plane models) and auto-declares a
 * binding when you pick an inference model not on the document yet — writing the choice into
 * `check.model.model`. Offline, it still lists declared bindings and hints to declare manually. */
function GuardrailModelField({
  ir,
  nodeId,
  value,
  model,
  onBlock,
  onChange,
}: {
  ir: IRDocument;
  nodeId: string;
  value: string | undefined;
  model: NonNullable<GuardrailCheck["model"]> | null;
  onBlock: Record<string, unknown>;
  onChange: (ir: IRDocument) => void;
}) {
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
    let next = setGuardrailCheck(
      ir,
      nodeId,
      {
        type: "model",
        model: { model: v, prompt: model?.prompt ?? "", passOn: model?.passOn ?? "yes" },
      },
      onBlock,
    );
    // Auto-declare a graph binding when picking an inference model not already on the document.
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
      label="judge model *"
      value={value}
      options={options}
      hint={hint}
      placeholder="pick a binding or inference model…"
      onChange={pick}
    />
  );
}

// ── graph-level panel (read-only models · editable tools/connections) ──────────

function GraphPanel({ ir }: { ir: IRDocument }) {
  const models = Object.entries(ir.models ?? {});
  return (
    <div className="flex h-full flex-col overflow-y-auto p-3 text-sm">
      {/* Start below the collapse chevron docked in the panel's top-left corner. */}
      <p className="mb-3 pt-6 text-xs text-slate-600">
        Select a node or edge to edit it. Model bindings and tools are derived from the canvas
        (added as you place + wire nodes); connections are managed below.
      </p>
      <Section title="Model bindings">
        {models.length === 0 ? (
          <EmptyHint>No models declared.</EmptyHint>
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

/** The graph's tool registry: `ir.tools` is DERIVED from the tool nodes on the canvas — like
 * `ir.models` fills in as you bind models — so this lists, read-only, every tool currently in the
 * agent with its kind. To add or remove a tool, drop/delete a tool node and wire it to an llm. */
function ToolsRegistry({ ir }: { ir: IRDocument }) {
  const tools = Object.entries((ir.tools ?? {}) as Record<string, ToolBinding>);
  return (
    <Section title="Tools">
      {tools.length === 0 ? (
        <EmptyHint>No tools yet — drop a tool node and wire it to an llm.</EmptyHint>
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

// ── connections panel (the graph-level tool/MCP auth surface) ──────────────────────────────────────

/** List + create server-side `connection`s (the tool/MCP auth seam). The ``secret`` is
 * WRITE-ONLY — typed here, sent to the encrypted store, and NEVER rendered back (a record carries
 * only ``hasSecret``). */
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
        <EmptyHint>Control plane unreachable.</EmptyHint>
      ) : (connections ?? []).length === 0 ? (
        <EmptyHint>No connections. Create one to give an http tool / MCP its auth.</EmptyHint>
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
                <span className="flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
                  <Lock size={10} aria-hidden /> secret set
                </span>
              )}
            </div>
          </div>
        ))
      )}

      <div className="mt-1.5 flex gap-2">
        <Button className="h-7 text-xs" onClick={() => setOpen((o) => !o)}>
          {open ? (
            "Cancel"
          ) : (
            <>
              <Plus size={14} aria-hidden /> New connection
            </>
          )}
        </Button>
      </div>

      {open && (
        <div className="mt-2 space-y-2 rounded border border-slate-800 p-2">
          <Field label="Name">
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Kind">
            <Select
              className="!text-xs"
              value={kind}
              onChange={(e) => setKind(e.target.value as "http_auth" | "mcp_server")}
            >
              <option value="http_auth">http_auth</option>
              <option value="mcp_server">mcp_server</option>
            </Select>
          </Field>
          <Field label="Config (non-secret JSON)">
            <Textarea
              spellCheck={false}
              className="mono h-20 !text-xs"
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
          {err && <ErrorBanner error={err} />}
          <Button
            variant="primary"
            className="h-7 text-xs"
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

// A compact sidebar empty-state hint — deliberately NOT the shared dashed-box Empty from ./ui
// (that treatment is sized for full list pages) and named apart from it so the two can't be
// import-swapped by accident.
function EmptyHint({ children }: { children: React.ReactNode }) {
  return <p className="text-xs text-slate-600">{children}</p>;
}

// The Field-label caption for content that is NOT a single labelable control (button groups, the
// icon picker). A real <label> here would make clicking the caption activate the first button and
// override the buttons' accessible names, so this renders a plain <span> caption instead.
function FieldGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
        {label}
      </span>
      {children}
    </div>
  );
}
