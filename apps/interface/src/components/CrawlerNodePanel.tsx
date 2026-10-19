// The inspector for a `crawler` node: which actor it runs (and a way to change it), the provider
// connection (with a set-the-token-once field), the fields the model fills, a fixed-input form
// generated from the actor's own input schema, and the limits. The node's `parameterSchema` is
// never edited here: ticking a field asks the control plane to regenerate it from the actor's real
// schema, so what the model sees always matches what the actor accepts.

import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { IRDocument, Node as IRNode } from "@theygent/ir-types";
import { ChevronDown, Globe } from "lucide-react";
import { useEffect, useState } from "react";
import { updateNodeConfig } from "../adapter";
import { type CrawlerField, api } from "../lib/api";
import { CrawlersDialog, providerConnections } from "./CrawlersDialog";
import { Badge, Button, ErrorBanner, Field, Input, Select, Textarea } from "./ui";
import { Checkbox } from "./ui/checkbox";

type Config = Record<string, unknown>;

/** How one actor input field is edited in the fixed-input form. Editors a form can't express
 * (proxy, key-value, JSON) fall back to a JSON box. */
export type FieldControl =
  | "checkbox"
  | "number"
  | "select"
  | "text"
  | "textarea"
  | "multiselect"
  | "list"
  | "urls"
  | "json";

export function fieldControl(f: CrawlerField): FieldControl {
  if (f.type === "boolean") return "checkbox";
  if (f.type === "integer" || f.type === "number") return "number";
  if (f.type === "string") {
    if (Array.isArray(f.enum)) return "select";
    return f.editor === "textarea" ? "textarea" : "text";
  }
  if (f.type === "array") {
    if (f.editor === "requestListSources") return "urls";
    if (Array.isArray(f.enum)) return "multiselect";
    if (f.editor === "stringList" || f.itemType === "string") return "list";
  }
  return "json";
}

/** One line per entry → the array the actor wants (URLs as `{url}` objects). Blank lines drop. */
export function linesToValue(text: string, control: "list" | "urls"): unknown[] {
  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  return control === "urls" ? lines.map((url) => ({ url })) : lines;
}

function valueToLines(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value
    .map((v) =>
      typeof v === "string"
        ? v
        : v && typeof v === "object" && "url" in v
          ? String((v as { url: unknown }).url)
          : "",
    )
    .join("\n");
}

const LIMIT_FIELDS: { key: string; label: string; min: number; max?: number; step?: number }[] = [
  { key: "timeoutSeconds", label: "timeout (s)", min: 1, max: 3600 },
  { key: "maxChargeUsd", label: "max charge per call (USD)", min: 0.01, step: 0.01 },
  { key: "maxItems", label: "max billed items", min: 1 },
  { key: "itemLimit", label: "items read back", min: 1, max: 1000 },
  { key: "resultChars", label: "characters the model reads", min: 200, max: 200_000 },
];

export function CrawlerNodePanel({
  ir,
  node,
  onChange,
}: {
  ir: IRDocument;
  node: IRNode;
  onChange: (ir: IRDocument) => void;
}) {
  const config = (node.config ?? {}) as Config;
  const actor = (config.actor as string) || "";
  const [browse, setBrowse] = useState(false);
  const set = (patch: Config) => onChange(updateNodeConfig(ir, node.id, { ...config, ...patch }));

  return (
    <>
      <div className="space-y-1">
        <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
          crawler *
        </span>
        {actor ? (
          <div className="flex items-center gap-2 rounded-md border border-slate-800 px-2 py-1.5">
            <Globe size={13} aria-hidden className="shrink-0 text-slate-500" />
            <span className="mono min-w-0 flex-1 truncate text-xs text-slate-200" title={actor}>
              {actor}
            </span>
            <Badge>{(config.provider as string) || "apify"}</Badge>
            <Button className="h-6 px-2 text-[11px]" onClick={() => setBrowse(true)}>
              Change
            </Button>
          </div>
        ) : (
          <Button className="h-7 w-full text-xs" onClick={() => setBrowse(true)}>
            <Globe size={12} aria-hidden /> Browse crawlers
          </Button>
        )}
      </div>
      {browse && (
        <CrawlersDialog
          ir={ir}
          onChange={onChange}
          onClose={() => setBrowse(false)}
          replace={node.id}
        />
      )}
      {actor && <CrawlerSettings key={actor} ir={ir} node={node} config={config} set={set} />}
    </>
  );
}

function CrawlerSettings({
  ir,
  node,
  config,
  set,
}: {
  ir: IRDocument;
  node: IRNode;
  config: Config;
  set: (patch: Config) => void;
}) {
  const actor = config.actor as string;
  const detail = useQuery({
    queryKey: ["crawler", actor],
    queryFn: () => api.getCrawler(actor),
    retry: false,
    staleTime: 300_000,
  });
  const fields = detail.data?.fields ?? null;
  const expose = (config.expose as string[]) ?? [];
  const scalar = (config.scalar as string[]) ?? [];
  const input = (config.input as Config) ?? {};
  const [regenError, setRegenError] = useState<unknown>(null);

  // Ticking a field (or its one-value toggle) regenerates the stored schema from the actor's
  // real input schema; the rest of the config is kept as it is.
  const regenerate = async (nextExpose: string[], nextScalar: string[]) => {
    setRegenError(null);
    try {
      const conv = await api.convertCrawler({
        actor,
        provider: (config.provider as string) || "apify",
        expose: nextExpose,
        scalar: nextScalar,
        input,
        description: (config.description as string | null) ?? null,
        connection: (config.connection as string) || null,
        limits: (config.limits as Config) ?? null,
      });
      set({
        expose: conv.config.expose,
        scalar: conv.config.scalar,
        parameterSchema: conv.config.parameterSchema,
        input: conv.config.input,
      });
    } catch (e) {
      setRegenError(e);
    }
  };

  const order = (names: Set<string>) =>
    (fields ?? []).map((f) => f.name).filter((n) => names.has(n));
  const toggleExpose = (name: string, on: boolean) => {
    const next = new Set(expose);
    if (on) next.add(name);
    else next.delete(name);
    regenerate(
      order(next),
      scalar.filter((s) => next.has(s)),
    );
  };
  const toggleScalar = (name: string, on: boolean) => {
    const next = new Set(scalar);
    if (on) next.add(name);
    else next.delete(name);
    regenerate(expose, order(next));
  };

  // A capability crawler (its `use` handle wired into an llm) takes the exposed fields from the
  // model; a step takes them from its fixed input.
  const capability = (ir.edges ?? []).some(
    (e) => e.source === node.id && (e.channel ?? "data") === "tool",
  );
  const fixedFields = (fields ?? []).filter((f) => !expose.includes(f.name));

  return (
    <>
      <ProviderConnection
        value={(config.connection as string) || ""}
        onChange={(v) => set({ connection: v })}
      />

      <Field label="description (what the model sees)">
        <Textarea
          spellCheck={false}
          className="mono h-16 !text-xs"
          value={(config.description as string) ?? ""}
          placeholder="Use this when… (what the crawler finds, and what to pass it)"
          onChange={(e) => set({ description: e.target.value || null })}
        />
      </Field>

      <div className="space-y-1.5">
        <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
          fields the model fills
        </span>
        {detail.isError ? (
          <p className="text-[11px] text-slate-600">
            The store is unreachable, so the actor's fields can't be listed — the node keeps its
            saved fields ({expose.join(", ") || "none"}). Edit them in the Code view.
          </p>
        ) : !fields ? (
          <p className="text-[11px] text-slate-600">reading the actor's fields…</p>
        ) : (
          <div className="max-h-44 overflow-y-auto rounded-md border border-slate-800">
            {fields
              .filter((f) => f.exposable)
              .map((f) => {
                const on = expose.includes(f.name);
                const listy = f.type === "array";
                return (
                  <div
                    key={f.name}
                    className="flex items-start gap-2 border-b border-slate-800/60 px-2 py-1 last:border-b-0"
                  >
                    <Checkbox
                      className="mt-0.5"
                      aria-label={`expose ${f.name}`}
                      checked={on}
                      onCheckedChange={(v) => toggleExpose(f.name, v === true)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="mono block text-xs text-slate-200">{f.name}</span>
                      <span
                        className="block truncate text-[10px] text-slate-600"
                        title={f.description}
                      >
                        {f.description}
                      </span>
                      {on && listy && (
                        <label className="mt-0.5 flex items-center gap-1.5 text-[10px] text-slate-500">
                          <Checkbox
                            aria-label={`one value per call for ${f.name}`}
                            checked={scalar.includes(f.name)}
                            onCheckedChange={(v) => toggleScalar(f.name, v === true)}
                          />
                          one value per call (safer for local models)
                        </label>
                      )}
                    </span>
                  </div>
                );
              })}
          </div>
        )}
        {!capability && expose.length > 0 && (
          <p className="text-[10px] text-slate-600">
            Exposed fields are filled by the model only when this node is wired into an llm's tools
            port. As a step, set them in the fixed input (a value of exactly{" "}
            <span className="mono">$in</span> reads the in-port).
          </p>
        )}
        <ErrorBanner error={regenError} />
      </div>

      <div className="space-y-1.5">
        <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
          fixed input
        </span>
        <p className="text-[10px] text-slate-600">
          Sent on every call; the model never sees it. Leave a field empty to use the actor's
          default.
        </p>
        {fields ? (
          <FixedInputForm
            fields={fixedFields}
            input={input}
            onChange={(next) => set({ input: next })}
          />
        ) : (
          <JsonBox label="input (JSON)" value={input} onChange={(v) => set({ input: v ?? {} })} />
        )}
      </div>

      <LimitsSection
        limits={(config.limits as Config) ?? {}}
        onChange={(l) => set({ limits: l })}
      />

      <p className="text-[10px] text-slate-600">
        Wire this node's violet <span className="text-violet-600 dark:text-violet-300">use</span>{" "}
        handle into an llm's <span className="text-violet-600 dark:text-violet-300">tools</span>{" "}
        port so the model can call it, or wire its data ports to run it as a step.
      </p>
    </>
  );
}

function ProviderConnection({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const qc = useQueryClient();
  const { data, isError } = useQuery({
    queryKey: ["connections"],
    queryFn: api.listConnections,
    retry: false,
    staleTime: 30_000,
  });
  const conns = providerConnections(data);
  const current = conns.find((c) => c.id === value);
  const [token, setToken] = useState("");
  const [error, setError] = useState<unknown>(null);
  // Which connection the token was just saved on — a different pick shows its own state.
  const [savedFor, setSavedFor] = useState<string | null>(null);
  const saved = savedFor === value;

  const saveToken = async () => {
    setError(null);
    try {
      await api.patchConnection(value, { secret: token.trim() });
      setToken(""); // write-only: never kept in the page
      setSavedFor(value);
      qc.invalidateQueries({ queryKey: ["connections"] });
    } catch (e) {
      setError(e);
    }
  };

  return (
    <Field label="connection (Apify token) *">
      <Select value={value} onChange={(e) => onChange(e.target.value)}>
        <option value="">— pick a connection —</option>
        {value && !current && <option value={value}>{value}</option>}
        {conns.map((c) => (
          <option key={c.id} value={c.id}>
            {c.name}
            {c.hasSecret ? "" : " (no token yet)"}
          </option>
        ))}
      </Select>
      {current && !current.hasSecret && !saved && (
        <div className="mt-1 flex gap-1.5">
          <Input
            type="password"
            className="h-7 text-xs"
            placeholder="apify_api_… (stored encrypted)"
            aria-label="Apify API token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <Button className="h-7 text-xs" disabled={!token.trim()} onClick={saveToken}>
            Set token
          </Button>
        </div>
      )}
      <span className="text-[10px] text-slate-600">
        {isError
          ? "connections unreachable — the saved id still applies at run time"
          : saved
            ? "token saved — resolved server-side at run time"
            : "the token stays in the encrypted secret store; the node holds the connection id"}
      </span>
      <ErrorBanner error={error} />
    </Field>
  );
}

function FixedInputForm({
  fields,
  input,
  onChange,
}: {
  fields: CrawlerField[];
  input: Config;
  onChange: (next: Config) => void;
}) {
  const [advanced, setAdvanced] = useState(false);
  const setKey = (name: string, value: unknown) => {
    const next = { ...input };
    if (value === undefined || value === "" || (Array.isArray(value) && value.length === 0)) {
      delete next[name];
    } else {
      next[name] = value;
    }
    onChange(next);
  };
  const simple = fields.filter((f) => fieldControl(f) !== "json" && f.editor !== "hidden");
  const rest = fields.filter((f) => !simple.includes(f));
  if (fields.length === 0) {
    return <p className="text-[11px] text-slate-600">Every field is exposed to the model.</p>;
  }
  return (
    <div className="space-y-2">
      {simple.map((f) => (
        <FieldInput
          key={f.name}
          field={f}
          value={input[f.name]}
          onChange={(v) => setKey(f.name, v)}
        />
      ))}
      {rest.length > 0 && (
        <div className="rounded-md border border-slate-800">
          <button
            type="button"
            onClick={() => setAdvanced((o) => !o)}
            aria-expanded={advanced}
            className="flex w-full items-center gap-1.5 px-2 py-1.5 text-left text-[11px] font-medium uppercase tracking-wide text-slate-500"
          >
            <ChevronDown
              size={13}
              aria-hidden
              className={`shrink-0 text-slate-600 transition-transform ${advanced ? "" : "-rotate-90"}`}
            />
            Advanced ({rest.length})
          </button>
          {advanced && (
            <div className="space-y-2 border-t border-slate-800 p-2">
              {rest.map((f) => (
                <JsonBox
                  key={f.name}
                  label={`${f.name}${f.required ? " *" : ""} (JSON)`}
                  value={input[f.name] ?? null}
                  onChange={(v) => setKey(f.name, v === null ? undefined : v)}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function placeholderOf(f: CrawlerField): string {
  const d = f.default ?? f.prefill ?? f.example;
  if (d === undefined || d === null) return "actor default";
  return typeof d === "string" ? d : JSON.stringify(d);
}

function FieldInput({
  field: f,
  value,
  onChange,
}: {
  field: CrawlerField;
  value: unknown;
  onChange: (v: unknown) => void;
}) {
  const control = fieldControl(f);
  const label = `${f.title || f.name}${f.required ? " *" : ""}${f.unit ? ` (${f.unit})` : ""}`;
  const hint = f.description ? (
    <span className="block text-[10px] text-slate-600" title={f.description}>
      <span className="mono">{f.name}</span> — {f.description.slice(0, 140)}
      {f.description.length > 140 ? "…" : ""}
    </span>
  ) : null;
  if (control === "checkbox") {
    const checked = typeof value === "boolean" ? value : f.default === true;
    return (
      <div className="space-y-0.5">
        <label className="flex items-center gap-2 text-xs text-slate-300">
          <Checkbox
            aria-label={f.name}
            checked={checked}
            onCheckedChange={(v) => onChange(v === true)}
          />
          {label}
        </label>
        {hint}
      </div>
    );
  }
  return (
    <Field label={label}>
      {control === "number" ? (
        <Input
          type="number"
          min={f.minimum}
          max={f.maximum}
          value={typeof value === "number" ? String(value) : ""}
          placeholder={placeholderOf(f)}
          onChange={(e) => onChange(e.target.value === "" ? undefined : Number(e.target.value))}
        />
      ) : control === "select" ? (
        <Select
          value={typeof value === "string" ? value : ""}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">{`— ${placeholderOf(f)} —`}</option>
          {(f.enum ?? []).map((v, i) => (
            <option key={String(v)} value={String(v)}>
              {f.enumTitles?.[i] ?? String(v)}
            </option>
          ))}
        </Select>
      ) : control === "multiselect" ? (
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {(f.enum ?? []).map((v, i) => {
            const list = Array.isArray(value) ? (value as unknown[]) : [];
            return (
              <label key={String(v)} className="flex items-center gap-1.5 text-xs text-slate-300">
                <Checkbox
                  aria-label={`${f.name} ${String(v)}`}
                  checked={list.includes(v)}
                  onCheckedChange={(on) =>
                    onChange(on === true ? [...list, v] : list.filter((x) => x !== v))
                  }
                />
                {f.enumTitles?.[i] ?? String(v)}
              </label>
            );
          })}
        </div>
      ) : control === "list" || control === "urls" ? (
        <LinesInput
          value={value}
          placeholder={control === "urls" ? "one URL per line" : "one value per line"}
          onChange={(text) => onChange(linesToValue(text, control))}
        />
      ) : control === "textarea" ? (
        <Textarea
          className="h-16 !text-xs"
          value={typeof value === "string" ? value : ""}
          placeholder={placeholderOf(f)}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <Input
          value={typeof value === "string" ? value : ""}
          placeholder={placeholderOf(f)}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
      {hint}
    </Field>
  );
}

/** A one-entry-per-line textarea that keeps the raw text while typing (so a trailing newline is
 * not swallowed) and re-syncs only when the value changes from elsewhere. */
function LinesInput({
  value,
  placeholder,
  onChange,
}: {
  value: unknown;
  placeholder: string;
  onChange: (text: string) => void;
}) {
  const [text, setText] = useState(() => valueToLines(value));
  // Adopt an outside change (another node, the Code view) — but not the echo of our own edit,
  // which would eat a trailing newline mid-typing.
  useEffect(() => {
    setText((prev) => {
      const external = valueToLines(value);
      return external === valueToLines(linesToValue(prev, "list")) ? prev : external;
    });
  }, [value]);
  return (
    <Textarea
      spellCheck={false}
      className="mono h-16 !text-xs"
      value={text}
      placeholder={placeholder}
      onChange={(e) => {
        setText(e.target.value);
        onChange(e.target.value);
      }}
    />
  );
}

function JsonBox({
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
  return (
    <Field label={label}>
      <Textarea
        spellCheck={false}
        className="mono h-20 !text-xs"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          try {
            onChange(JSON.parse(e.target.value || "null"));
            setError(null);
          } catch (err) {
            setError((err as Error).message);
          }
        }}
      />
      {error && <span className="text-[11px] text-red-600 dark:text-red-400">{error}</span>}
    </Field>
  );
}

function LimitsSection({ limits, onChange }: { limits: Config; onChange: (l: Config) => void }) {
  const setKey = (key: string, value: unknown) => {
    const next = { ...limits };
    if (value === undefined) delete next[key];
    else next[key] = value;
    onChange(next);
  };
  const fields = Array.isArray(limits.fields) ? (limits.fields as string[]) : [];
  return (
    <div className="space-y-2 rounded-md border border-slate-800 p-2">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
        limits
      </span>
      <div className="grid grid-cols-2 gap-2">
        {LIMIT_FIELDS.map((l) => (
          <Field key={l.key} label={l.label}>
            <Input
              type="number"
              min={l.min}
              max={l.max}
              step={l.step ?? 1}
              value={typeof limits[l.key] === "number" ? String(limits[l.key]) : ""}
              placeholder="default"
              onChange={(e) =>
                setKey(l.key, e.target.value === "" ? undefined : Number(e.target.value))
              }
            />
          </Field>
        ))}
      </div>
      <Field label="item fields the model reads (comma-separated; empty = all)">
        <Input
          className="mono"
          value={fields.join(", ")}
          placeholder="e.g. markdown, url"
          onChange={(e) =>
            setKey(
              "fields",
              e.target.value
                .split(",")
                .map((s) => s.trim())
                .filter(Boolean),
            )
          }
        />
      </Field>
      <p className="text-[10px] text-slate-600">
        Interactive runs stop at 300 s (the provider's synchronous limit); durable runs poll, so a
        crawl can take up to the timeout. The charge cap never exceeds the platform ceiling in
        Settings → Crawlers.
      </p>
    </div>
  );
}
