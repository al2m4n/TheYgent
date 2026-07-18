// One settings field, rendered from its catalog entry: label + honesty badges (env-pinned,
// env-capped, apply mode, restart-pending, invalid-stored), the type-appropriate control, the
// per-field reset, and the description as helper text. Tabs compose <SettingRow> with the default
// control (by type) or hand in a bespoke one (headers editor, registries table, MiB input).

import { Lock, RotateCcw } from "lucide-react";
import { type ReactNode, useEffect, useState } from "react";
import type { SensitiveValue, SettingEntry } from "../../lib/api";
import { Badge, Button, Input, Select, Textarea } from "../ui";
import { Switch } from "../ui/switch";
import { type PlatformSettingsForm, displayValue } from "./useSettingsForm";

// "telemetry.io_capture_max_bytes" → "io capture max bytes" (the dotted key stays visible in mono).
export function humanizeKey(key: string): string {
  const last = key.split(".").pop() ?? key;
  return last.replace(/_/g, " ");
}

function fmtValue(v: unknown): string {
  if (v == null) return "—";
  if (typeof v === "string") return v;
  return JSON.stringify(v);
}

export function SettingBadges({ entry }: { entry: SettingEntry }) {
  return (
    <>
      {entry.env_pinned && entry.env_var && (
        <Badge tone="slate">
          <span
            className="inline-flex items-center gap-1"
            title={`Set by the ${entry.env_var} environment variable — unset it to edit here.`}
          >
            <Lock size={10} aria-hidden />
            pinned by {entry.env_var}
          </span>
        </Badge>
      )}
      {entry.env_capped && (
        <Badge tone="amber">
          <span
            title={`Editable, but the effective value never exceeds the ${entry.env_var ?? "environment"} cap.`}
          >
            capped at {fmtValue(entry.env_cap_value)} by {entry.env_var ?? "env"}
          </span>
        </Badge>
      )}
      {entry.apply !== "live" && <Badge tone="blue">{entry.apply}</Badge>}
      {entry.restart_pending && <Badge tone="amber">restart pending</Badge>}
      {entry.stored_invalid && <Badge tone="red">stored value invalid — using default</Badge>}
    </>
  );
}

export function SettingRow({
  entry,
  form,
  control,
  label,
}: {
  entry: SettingEntry;
  form: PlatformSettingsForm;
  /** Bespoke control; omitted = the default control for the entry's type. */
  control?: ReactNode;
  label?: string;
}) {
  const staged = entry.key in form.edits;
  const resetStaged = staged && form.edits[entry.key] === null;
  const applyError = form.applyErrors[entry.key];
  const blocker = form.blockers[entry.key];
  const canReset = entry.stored_value !== null && !entry.env_pinned;
  // The capped case where honesty matters most: something IS stored but the env cap wins.
  const cappedDown =
    entry.env_capped &&
    entry.stored_value !== null &&
    JSON.stringify(entry.value) !== JSON.stringify(entry.stored_value);

  return (
    <div className="space-y-1.5 border-b border-border/60 py-3 first:pt-0 last:border-b-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium capitalize text-foreground">
          {label ?? humanizeKey(entry.key)}
        </span>
        <span className="mono text-[10px] text-muted-foreground/70">{entry.key}</span>
        <SettingBadges entry={entry} />
        {resetStaged && <Badge tone="blue">resets to default on save</Badge>}
        <span className="ml-auto flex items-center gap-1">
          {resetStaged ? (
            <Button
              variant="ghost"
              className="h-6 px-2 text-xs"
              onClick={() => form.unstage(entry.key)}
            >
              Undo reset
            </Button>
          ) : (
            canReset && (
              <Button
                variant="ghost"
                className="h-6 px-2 text-xs text-muted-foreground"
                aria-label={`Reset ${entry.key} to default`}
                title={`Reset to default (${fmtValue(entry.default)})`}
                onClick={() => form.stageReset(entry.key)}
              >
                <RotateCcw size={12} aria-hidden />
                Reset
              </Button>
            )
          )}
        </span>
      </div>

      <div className="max-w-md">{control ?? <SettingControl entry={entry} form={form} />}</div>

      {cappedDown && (
        <p className="text-xs text-amber-700 dark:text-amber-300">
          Effective: {fmtValue(entry.value)} — the stored value {fmtValue(entry.stored_value)}{" "}
          exceeds the environment cap.
        </p>
      )}
      <p className="text-xs text-muted-foreground">{entry.description}</p>
      {blocker && <p className="text-xs text-destructive">{blocker}</p>}
      {applyError && (
        <p className="text-xs text-destructive">Saved, but could not apply live: {applyError}</p>
      )}
    </div>
  );
}

// ── default controls by catalog type ─────────────────────────────────────────

export function SettingControl({
  entry,
  form,
}: {
  entry: SettingEntry;
  form: PlatformSettingsForm;
}) {
  const value = displayValue(form, entry);
  const disabled = entry.env_pinned;

  switch (entry.type) {
    case "bool":
      return (
        <Switch
          aria-label={entry.key}
          checked={Boolean(value)}
          disabled={disabled}
          onCheckedChange={(v) => form.setEdit(entry.key, v)}
        />
      );
    case "int":
    case "float":
      return <NumberControl entry={entry} form={form} />;
    case "enum":
      return (
        <Select
          aria-label={entry.key}
          value={typeof value === "string" ? value : ""}
          disabled={disabled}
          onChange={(e) => form.setEdit(entry.key, e.target.value)}
        >
          {(entry.constraints?.choices ?? []).map((opt) => (
            <option key={opt} value={opt}>
              {opt}
            </option>
          ))}
        </Select>
      );
    case "str":
      return (
        <Input
          aria-label={entry.key}
          value={typeof value === "string" ? value : ""}
          disabled={disabled}
          onChange={(e) => {
            const raw = e.target.value;
            // The server rejects "" outright (null is the reset encoding), so an emptied field
            // means reset — or nothing at all when there is no stored value to reset.
            if (raw.trim() === "") {
              if (entry.stored_value != null) form.stageReset(entry.key);
              else form.unstage(entry.key);
              return;
            }
            form.setEdit(entry.key, raw);
          }}
        />
      );
    case "list[str]":
      return <ListControl entry={entry} form={form} />;
    case "secret": {
      // Fallback only — a secret map should get a bespoke editor (values never round-trip).
      const v = value as SensitiveValue | null;
      return (
        <p className="text-xs text-muted-foreground">
          {v?.set ? `${v.names.length} value(s) set — names: ${v.names.join(", ")}` : "not set"}
        </p>
      );
    }
    case "list[registry]":
      return <JsonControl entry={entry} form={form} />;
  }
}

// Numbers keep local text state so partially-typed input ("2.", "-") isn't clobbered by the
// round-trip through parse; commits happen on every valid keystroke, invalid text blocks the save.
function NumberControl({ entry, form }: { entry: SettingEntry; form: PlatformSettingsForm }) {
  const value = displayValue(form, entry);
  const [text, setText] = useState(value == null ? "" : String(value));

  // Re-sync when the underlying value changes from OUTSIDE the input (reset staged, save landed).
  // biome-ignore lint/correctness/useExhaustiveDependencies: sync from the derived display value only
  useEffect(() => {
    const parsed = entry.type === "int" ? Number.parseInt(text, 10) : Number.parseFloat(text);
    if (value == null ? text !== "" : parsed !== value) setText(value == null ? "" : String(value));
  }, [value]);

  // The invalid text this blocker guards lives only in local state — an unmount (tab switch)
  // discards the text, so the blocker must go with it or the save stays held under a field that
  // re-seeds valid on return.
  useEffect(() => () => form.setBlocker(entry.key, null), [form.setBlocker, entry.key]);

  const onChange = (raw: string) => {
    setText(raw);
    if (raw.trim() === "") {
      form.setBlocker(entry.key, "A value is required — use Reset to go back to the default.");
      return;
    }
    const n = entry.type === "int" ? Number.parseInt(raw, 10) : Number.parseFloat(raw);
    if (Number.isNaN(n)) {
      form.setBlocker(entry.key, "Not a number.");
      return;
    }
    const { min, max } = entry.constraints ?? {};
    if (min != null && n < min) {
      form.setBlocker(entry.key, `Must be at least ${min}.`);
      return;
    }
    if (max != null && n > max) {
      form.setBlocker(entry.key, `Must be at most ${max}.`);
      return;
    }
    form.setBlocker(entry.key, null);
    form.setEdit(entry.key, n);
  };

  return (
    <Input
      aria-label={entry.key}
      type="number"
      inputMode={entry.type === "int" ? "numeric" : "decimal"}
      value={text}
      min={entry.constraints?.min}
      max={entry.constraints?.max}
      step={entry.type === "int" ? 1 : "any"}
      disabled={entry.env_pinned}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

// A comma-separated tag input for list-of-strings settings (e.g. redacted attribute names).
function ListControl({ entry, form }: { entry: SettingEntry; form: PlatformSettingsForm }) {
  const value = displayValue(form, entry);
  const list = Array.isArray(value) ? (value as string[]) : [];
  const [text, setText] = useState(list.join(", "));

  // biome-ignore lint/correctness/useExhaustiveDependencies: sync from the derived display value only
  useEffect(() => {
    const parsed = text
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (JSON.stringify(parsed) !== JSON.stringify(list)) setText(list.join(", "));
  }, [JSON.stringify(list)]);

  return (
    <Input
      aria-label={entry.key}
      value={text}
      disabled={entry.env_pinned}
      placeholder="comma, separated, values"
      onChange={(e) => {
        setText(e.target.value);
        form.setEdit(
          entry.key,
          e.target.value
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        );
      }}
    />
  );
}

// Raw-JSON fallback editor for structured settings that lack a bespoke control.
function JsonControl({ entry, form }: { entry: SettingEntry; form: PlatformSettingsForm }) {
  const value = displayValue(form, entry);
  const [text, setText] = useState(() => JSON.stringify(value ?? null, null, 2));

  // Invalid JSON text lives only in local state — the blocker must not outlive an unmount (tab
  // switch), which discards the text and re-seeds valid on return.
  useEffect(() => () => form.setBlocker(entry.key, null), [form.setBlocker, entry.key]);

  return (
    <Textarea
      aria-label={entry.key}
      rows={4}
      className="mono"
      value={text}
      disabled={entry.env_pinned}
      onChange={(e) => {
        setText(e.target.value);
        try {
          form.setBlocker(entry.key, null);
          form.setEdit(entry.key, JSON.parse(e.target.value));
        } catch {
          form.setBlocker(entry.key, "Not valid JSON.");
        }
      }}
    />
  );
}
