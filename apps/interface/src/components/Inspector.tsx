// The node/edge inspector (M15 §2.3): edit the selected node's `config` (schema-driven from the
// per-type JSON Schema) or the selected edge's `channel`/`condition`, with explicit Delete /
// Duplicate controls. Graph-level `models`/`tools` are exposed READ-ONLY (full binding/tool editors
// are out of scope, §2.3). When nothing is selected, the panel shows the graph's bindings.

import { useQuery } from "@tanstack/react-query";
import { type IRDocument, NODE_TYPES } from "@theygent/ir-types";
import { useEffect, useId, useState } from "react";
import {
  type Selection,
  deleteEdges,
  deleteNodes,
  duplicateNode,
  updateEdge,
  updateNodeConfig,
  updateNodeLabel,
} from "../adapter";
import { api } from "../lib/api";
import { Badge, Button, Field, Input } from "./ui";

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

        {Object.keys(properties).length === 0 ? (
          <p className="text-xs text-slate-600">This node type has no editable config.</p>
        ) : (
          Object.entries(properties).map(([key, schema]) => {
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
            if (node.type === "mcp_tool" && key === "server") {
              return (
                <McpServerPicker
                  key={`${node.id}:${key}`}
                  value={config.server as string | undefined}
                  onChange={(v) => setConfigKey("server", v)}
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

        <RawConfigEditor
          key={`raw:${node.id}`}
          value={config}
          onCommit={(c) => onChange(updateNodeConfig(ir, node.id, c))}
        />
      </div>
    </div>
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
  label: string;
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

  return (
    <Field label={label}>
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
    </Field>
  );
}

// ── reference pickers (combobox over a live registry, free text still allowed) ─

/** A free-text input with a native datalist of suggestions — pick a known value or type a custom
 * one (so an offline/empty registry never blocks editing). The validator flags a dangling ref. */
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
  const listId = useId();
  return (
    <Field label={label}>
      <Input
        list={listId}
        value={value ?? ""}
        placeholder={placeholder ?? (loading ? "loading…" : "type or pick…")}
        onChange={(e) => onChange(e.target.value)}
      />
      <datalist id={listId}>
        {options.map((o) => (
          <option key={o} value={o} />
        ))}
      </datalist>
      {hint && <span className="text-[10px] text-slate-600">{hint}</span>}
    </Field>
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
    </div>
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
