// Settings → Telemetry: the local capture knobs (env-CAPPED — editable up to the env value) and
// the OTLP export card. Collector headers are a sensitive map: reads return names only, writes
// send a FULL replacement (or null to clear) — values never round-trip the wire. "Test
// connection" probes the collector with the CURRENT unsaved endpoint/headers before anything is
// persisted.

import { useMutation } from "@tanstack/react-query";
import { Plus, X } from "lucide-react";
import { useEffect, useState } from "react";
import { type OtlpTestResult, type SensitiveValue, type SettingEntry, api } from "../../lib/api";
import { Button, Card, ErrorBanner, Input, SectionHeading, Spinner } from "../ui";
import { SaveBar } from "./SaveBar";
import { SettingRow } from "./SettingField";
import { type PlatformSettingsForm, displayValue } from "./useSettingsForm";

const HEADERS_KEY = "telemetry.otlp_headers";
const ENDPOINT_KEY = "telemetry.otlp_endpoint";

export function TelemetryTab({ form }: { form: PlatformSettingsForm }) {
  // Gate on data, not isLoading — see BootConfig: retries against an unreachable plane must not
  // flash an empty success layout.
  if (form.loadError && !form.data) return <ErrorBanner error={form.loadError} />;
  if (!form.data) return <Spinner label="Loading settings…" />;

  const row = (key: string) => {
    const entry = form.entry(key);
    if (!entry) return null;
    if (entry.type === "secret")
      return (
        <SettingRow
          key={key}
          entry={entry}
          form={form}
          control={<HeadersEditor entry={entry} form={form} />}
        />
      );
    return <SettingRow key={key} entry={entry} form={form} />;
  };

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionHeading>Local capture</SectionHeading>
        <p className="mt-1 text-xs text-muted-foreground">
          What run I/O the local span store records. An environment cap can tighten these but never
          loosens them — stored values apply up to the cap.
        </p>
        <div className="mt-2">
          {row("telemetry.io_capture")}
          {row("telemetry.io_capture_max_bytes")}
        </div>
      </Card>

      <Card className="p-4">
        <SectionHeading>OTLP export</SectionHeading>
        <p className="mt-1 text-xs text-muted-foreground">
          Ship spans to your own collector. Export runs only while enabled here — this switch always
          wins, even when an environment endpoint is set.
        </p>
        <div className="mt-2">
          {row("telemetry.otlp_enabled")}
          {row("telemetry.otlp_endpoint")}
          {row(HEADERS_KEY)}
          {row("telemetry.otlp_redact_attrs")}
        </div>
        <OtlpTest form={form} />
      </Card>

      <SaveBar form={form} group="telemetry" />
    </div>
  );
}

// ── the sensitive headers editor ──────────────────────────────────────────────
// Read shape: {set, names} — the UI can show WHICH headers exist but never their values. Writes
// are full-replacement, so "editing" means re-entering every kept header's value; the editor makes
// that explicit instead of pretending a merge exists.

interface HeaderRow {
  name: string;
  value: string;
}

// Full-replacement writes make a half-filled row dangerous: a blank name or value would blank a
// stored secret (or 422 the whole batch — server validation is atomic), so it holds the save.
function rowsBlocker(rows: HeaderRow[]): string | null {
  return rows.some((r) => !r.name.trim() || !r.value)
    ? "Every kept header needs a name and a value."
    : null;
}

function HeadersEditor({ entry, form }: { entry: SettingEntry; form: PlatformSettingsForm }) {
  const staged = form.edits[entry.key];
  const stagedMap =
    staged != null && typeof staged === "object" ? (staged as Record<string, string>) : null;
  const isStaged = entry.key in form.edits;
  const clearing = isStaged && staged === null;
  const current = (entry.value ?? { set: false, names: [] }) as SensitiveValue;

  // Editing state reconstructs from the staged map (it survives tab switches — the staged edit
  // lives on the page-level form, this component only mirrors it into rows).
  const [rows, setRows] = useState<HeaderRow[] | null>(() =>
    stagedMap ? Object.entries(stagedMap).map(([name, value]) => ({ name, value })) : null,
  );

  // Rows reconstructed from a staged map must re-establish their validity blocker (it may have
  // been recomputed while this editor was unmounted on another tab).
  // biome-ignore lint/correctness/useExhaustiveDependencies: mount-time reconstruction only
  useEffect(() => {
    if (rows) form.setBlocker(entry.key, rowsBlocker(rows));
  }, []);

  // When the staged edit disappears (save landed, or a staged clear was undone), fall back to the
  // read view — just-typed plaintext values must never linger past the write.
  useEffect(() => {
    if (!isStaged) setRows(null);
  }, [isStaged]);

  const stage = (next: HeaderRow[]) => {
    if (next.length === 0) {
      // Removing every row means "clear the set" — the wire encodes that as a null reset, never
      // as an empty map (the server rejects {}). With nothing stored there is nothing to clear.
      setRows(null);
      if (current.set) form.stageReset(entry.key);
      else form.unstage(entry.key);
      return;
    }
    setRows(next);
    const map: Record<string, string> = {};
    for (const r of next) if (r.name.trim()) map[r.name.trim()] = r.value;
    form.setEdit(entry.key, map);
    form.setBlocker(entry.key, rowsBlocker(next));
  };

  const startEditing = () => {
    // Seed a replacement row per existing name, values blank — they must be re-entered (the
    // stored ones can't be read back). Purely local until something is actually typed: opening
    // the editor just to look must never stage a write that would blank the stored values.
    setRows(current.names.map((name) => ({ name, value: "" })));
  };

  const cancel = () => {
    setRows(null);
    form.unstage(entry.key);
  };

  if (clearing) {
    return (
      <div className="flex items-center gap-2 text-xs text-muted-foreground">
        <span>All headers will be removed on save.</span>
        <Button
          variant="ghost"
          className="h-6 px-2 text-xs"
          onClick={() => form.unstage(entry.key)}
        >
          Undo
        </Button>
      </div>
    );
  }

  if (rows === null) {
    return (
      <div className="space-y-2">
        {current.set && current.names.length > 0 ? (
          <ul className="space-y-1">
            {current.names.map((name) => (
              <li key={name} className="flex items-center gap-2 text-xs">
                <span className="mono text-foreground">{name}</span>
                <span className="text-muted-foreground">••••••</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No headers set.</p>
        )}
        <div className="flex gap-2">
          <Button className="h-7 px-2 text-xs" onClick={startEditing}>
            {current.set ? "Replace headers" : "Add headers"}
          </Button>
          {current.set && (
            <Button
              variant="ghost"
              className="h-7 px-2 text-xs text-destructive"
              onClick={() => {
                setRows(null);
                form.stageReset(entry.key);
              }}
            >
              Clear all
            </Button>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <p className="text-xs text-amber-700 dark:text-amber-300">
        Saving replaces the whole set — re-enter the value for every header you keep.
      </p>
      {rows.map((r, i) => (
        // biome-ignore lint/suspicious/noArrayIndexKey: rows are positional edit state with no stable id
        <div key={i} className="flex items-center gap-2">
          <Input
            aria-label={`Header ${i + 1} name`}
            className="flex-1"
            placeholder="Authorization"
            value={r.name}
            onChange={(e) =>
              stage(rows.map((row, j) => (j === i ? { ...row, name: e.target.value } : row)))
            }
          />
          <Input
            aria-label={`Header ${i + 1} value`}
            className="flex-1"
            type="password"
            placeholder="write-only value"
            value={r.value}
            onChange={(e) =>
              stage(rows.map((row, j) => (j === i ? { ...row, value: e.target.value } : row)))
            }
          />
          <Button
            variant="ghost"
            className="h-7 px-2"
            aria-label={`Remove header ${i + 1}`}
            onClick={() => stage(rows.filter((_, j) => j !== i))}
          >
            <X size={13} aria-hidden />
          </Button>
        </div>
      ))}
      <div className="flex gap-2">
        <Button
          className="h-7 px-2 text-xs"
          onClick={() => stage([...rows, { name: "", value: "" }])}
        >
          <Plus size={12} aria-hidden />
          Add header
        </Button>
        <Button variant="ghost" className="h-7 px-2 text-xs" onClick={cancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

// ── the collector reachability probe ─────────────────────────────────────────
// Exporter construction never validates anything server-side, so a bad endpoint would otherwise
// only surface as silently-dropped spans. The probe one-shots a synthetic, payload-free span with
// the values CURRENTLY in the form — saved or not.

function OtlpTest({ form }: { form: PlatformSettingsForm }) {
  const [result, setResult] = useState<OtlpTestResult | null>(null);

  const test = useMutation({
    mutationFn: () => {
      const body: { endpoint?: string; headers?: Record<string, string> } = {};
      const endpointEntry = form.entry(ENDPOINT_KEY);
      if (endpointEntry) {
        const v = displayValue(form, endpointEntry);
        if (typeof v === "string" && v.trim()) body.endpoint = v.trim();
      }
      const stagedHeaders = form.edits[HEADERS_KEY];
      if (stagedHeaders != null && typeof stagedHeaders === "object")
        body.headers = stagedHeaders as Record<string, string>;
      return api.testOtlp(body);
    },
    onSuccess: setResult,
    onError: () => setResult(null),
  });

  return (
    <div className="mt-3 space-y-2 border-t border-border/60 pt-3">
      <div className="flex items-center gap-3">
        <Button disabled={test.isPending} onClick={() => test.mutate()}>
          {test.isPending ? "Testing…" : "Test connection"}
        </Button>
        <span className="text-xs text-muted-foreground">
          Sends one synthetic, payload-free span using the values above (saved or not).
        </span>
      </div>
      {result &&
        (result.ok ? (
          <p className="text-xs text-emerald-700 dark:text-emerald-300">
            Collector reachable · {result.latency_ms} ms
          </p>
        ) : (
          <p className="text-xs text-destructive">
            Collector test failed{result.error ? `: ${result.error}` : ""}
          </p>
        ))}
      <ErrorBanner error={test.error} />
    </div>
  );
}
