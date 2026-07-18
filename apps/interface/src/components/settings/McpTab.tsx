// Settings → MCP: connection timeouts (apply per NEW connection — stated on the field via the
// catalog's apply label), the OAuth redirect, and the extra-registries table (id/label/url rows,
// all three required and the URL validated client-side as absolute https so a blank field or a
// typo can't 422 the whole batch server-side).

import { Plus, X } from "lucide-react";
import type { SettingEntry } from "../../lib/api";
import { Button, Card, ErrorBanner, Input, SectionHeading, Spinner } from "../ui";
import { SaveBar } from "./SaveBar";
import { SettingRow } from "./SettingField";
import { type PlatformSettingsForm, displayValue } from "./useSettingsForm";

export function McpTab({ form }: { form: PlatformSettingsForm }) {
  // Gate on data, not isLoading — see BootConfig: retries against an unreachable plane must not
  // flash an empty success layout.
  if (form.loadError && !form.data) return <ErrorBanner error={form.loadError} />;
  if (!form.data) return <Spinner label="Loading settings…" />;

  const row = (key: string) => {
    const entry = form.entry(key);
    if (!entry) return null;
    if (key === "mcp.extra_registries")
      return (
        <SettingRow
          key={key}
          entry={entry}
          form={form}
          control={<RegistriesEditor entry={entry} form={form} />}
        />
      );
    return <SettingRow key={key} entry={entry} form={form} />;
  };

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionHeading>Timeouts &amp; OAuth</SectionHeading>
        <div className="mt-2">
          {row("mcp.call_timeout_s")}
          {row("mcp.connect_timeout_s")}
          {row("mcp.oauth_redirect_url")}
        </div>
      </Card>

      <Card className="p-4">
        <SectionHeading>Extra registries</SectionHeading>
        <p className="mt-1 text-xs text-muted-foreground">
          Additional MCP registries to browse alongside the built-in one.
        </p>
        <div className="mt-2">{row("mcp.extra_registries")}</div>
      </Card>

      <SaveBar form={form} group="mcp" />
    </div>
  );
}

interface RegistryRow {
  id: string;
  label: string;
  url: string;
}

function asRows(value: unknown): RegistryRow[] {
  if (!Array.isArray(value)) return [];
  return value.map((r) => {
    const row = (r ?? {}) as Record<string, unknown>;
    return {
      id: typeof row.id === "string" ? row.id : "",
      label: typeof row.label === "string" ? row.label : "",
      url: typeof row.url === "string" ? row.url : "",
    };
  });
}

function isHttpsUrl(raw: string): boolean {
  try {
    return new URL(raw).protocol === "https:";
  } catch {
    return false;
  }
}

function RegistriesEditor({ entry, form }: { entry: SettingEntry; form: PlatformSettingsForm }) {
  const rows = asRows(displayValue(form, entry));
  const disabled = entry.env_pinned;

  const stage = (next: RegistryRow[]) => {
    form.setEdit(entry.key, next);
    // The server requires all three fields non-empty (and validates the batch atomically), so a
    // blank one here would reject every other staged edit in the group.
    const bad = next.some(
      (r) => !r.id.trim() || !r.label.trim() || !r.url.trim() || !isHttpsUrl(r.url.trim()),
    );
    form.setBlocker(
      entry.key,
      bad ? "Every registry needs an id, a label, and an absolute https:// URL." : null,
    );
  };

  return (
    <div className="space-y-2">
      {rows.length === 0 && <p className="text-xs text-muted-foreground">No extra registries.</p>}
      {rows.map((r, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional edit state with no stable id
        <div key={i} className="flex items-center gap-2">
          <Input
            aria-label={`Registry ${i + 1} id`}
            className="w-32"
            placeholder="id"
            value={r.id}
            disabled={disabled}
            onChange={(e) =>
              stage(rows.map((row, j) => (j === i ? { ...row, id: e.target.value } : row)))
            }
          />
          <Input
            aria-label={`Registry ${i + 1} label`}
            className="w-40"
            placeholder="Label"
            value={r.label}
            disabled={disabled}
            onChange={(e) =>
              stage(rows.map((row, j) => (j === i ? { ...row, label: e.target.value } : row)))
            }
          />
          <Input
            aria-label={`Registry ${i + 1} url`}
            className="flex-1"
            placeholder="https://registry.example.com"
            value={r.url}
            disabled={disabled}
            onChange={(e) =>
              stage(rows.map((row, j) => (j === i ? { ...row, url: e.target.value } : row)))
            }
          />
          <Button
            variant="ghost"
            className="h-7 px-2"
            aria-label={`Remove registry ${i + 1}`}
            disabled={disabled}
            onClick={() => stage(rows.filter((_, j) => j !== i))}
          >
            <X size={13} aria-hidden />
          </Button>
        </div>
      ))}
      <Button
        className="h-7 px-2 text-xs"
        disabled={disabled}
        onClick={() => stage([...rows, { id: "", label: "", url: "" }])}
      >
        <Plus size={12} aria-hidden />
        Add registry
      </Button>
    </div>
  );
}
