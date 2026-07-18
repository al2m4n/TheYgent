// Settings → Import / Export: move an install (or a subset of it) between machines as ONE zip,
// assembled and unpacked in the browser — the only place both planes' halves meet (lib/transfer).
// Secrets and model weights never ride a bundle, so the import PREVIEW says loudly what must be
// re-entered, re-authorized, re-downloaded, or re-ingested before anything is written; the write
// itself goes through the shared ConfirmDialog.

import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { notify, trackDownload } from "../../lib/notify";
import {
  type ApplyReport,
  type ExportSection,
  type ImportPreview,
  applyImport,
  buildExportZip,
  parseImportFile,
} from "../../lib/transfer";
import { Button, Card, ConfirmDialog, NoteBanner, SectionHeading } from "../ui";
import { Checkbox } from "../ui/checkbox";

export function ImportExportTab() {
  return (
    <div className="space-y-4">
      <ExportCard />
      <ImportCard />
    </div>
  );
}

// ── export ────────────────────────────────────────────────────────────────────

const SECTIONS: { value: ExportSection; label: string; hint: string }[] = [
  { value: "agents", label: "Agents", hint: "published versions, drafts, and triggers" },
  { value: "runs", label: "Runs", hint: "run history rows" },
  { value: "traces", label: "Traces", hint: "spans, node I/O, and artifacts — includes Runs" },
  { value: "sessions", label: "Chats", hint: "sessions and their messages" },
  { value: "mcp", label: "MCP servers", hint: "definitions only — secret values never export" },
  { value: "rag", label: "RAG sources", hint: "definitions only — documents re-ingest on import" },
  {
    value: "registries",
    label: "Model registry",
    hint: "registrations only — weights re-download on the target",
  },
];

function ExportCard() {
  const [selected, setSelected] = useState<Set<ExportSection>>(
    new Set(SECTIONS.map((s) => s.value)),
  );
  const [busy, setBusy] = useState(false);

  const toggle = (section: ExportSection, on: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (on) {
        next.add(section);
        // Traces are per-run rows — selecting them pulls Runs along (the server implies it too).
        if (section === "traces") next.add("runs");
      } else {
        next.delete(section);
        if (section === "runs") next.delete("traces");
      }
      return next;
    });
  };
  const allOn = selected.size === SECTIONS.length;

  const doExport = async () => {
    setBusy(true);
    try {
      const result = await buildExportZip([...selected]);
      for (const note of result.notes) notify.warning(note);
      notify.success(`Exported ${result.filename}`);
    } catch (e) {
      notify.error(e instanceof Error ? `Export failed: ${e.message}` : "Export failed");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card className="p-4">
      <SectionHeading>Export</SectionHeading>
      <p className="mt-1 text-xs text-muted-foreground">
        Download the selected parts of this install as one zip file. Secrets and model weights are
        never included — connections re-prompt for credentials and weights re-download on import.
      </p>
      <div className="mt-3 space-y-1.5">
        <label className="flex cursor-pointer items-center gap-2 text-sm">
          <Checkbox
            aria-label="All"
            checked={allOn}
            onCheckedChange={(v) =>
              setSelected(v === true ? new Set(SECTIONS.map((s) => s.value)) : new Set())
            }
          />
          <span className="font-medium">All</span>
        </label>
        <div className="space-y-1.5 border-l border-border/60 pl-5">
          {SECTIONS.map((s) => (
            <label key={s.value} className="flex cursor-pointer items-center gap-2 text-sm">
              <Checkbox
                aria-label={s.label}
                checked={selected.has(s.value)}
                onCheckedChange={(v) => toggle(s.value, v === true)}
              />
              <span>{s.label}</span>
              <span className="text-[11px] text-muted-foreground">{s.hint}</span>
            </label>
          ))}
        </div>
      </div>
      <div className="mt-3">
        <Button variant="primary" disabled={selected.size === 0 || busy} onClick={doExport}>
          {busy ? "Exporting…" : "Export"}
        </Button>
      </div>
    </Card>
  );
}

// ── import ────────────────────────────────────────────────────────────────────

const COUNT_LABELS: Record<string, string> = {
  agents: "Agents",
  drafts: "Drafts",
  runs: "Runs",
  spans: "Trace spans",
  node_io: "Node I/O rows",
  artifact_refs: "Artifacts",
  sessions: "Chats",
  connections: "Connections",
  mcp_servers: "MCP servers",
  rag_sources: "RAG sources",
  models: "Model registrations",
};

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

// The prominent pre-apply warnings, derived from the parsed bundle (transfer.ts). Only non-zero
// facts render — an all-quiet preview shows counts alone.
function previewWarningLines(p: ImportPreview): string[] {
  const w = p.warnings;
  const lines: string[] = [];
  if (w.modelDownloads > 0) {
    lines.push(
      `${plural(w.modelDownloads, "model")} will be registered — TheYgent will download their weights from Hugging Face.`,
    );
  }
  if (w.weightsUnavailable > 0) {
    lines.push(
      `${plural(w.weightsUnavailable, "model registration")} point${w.weightsUnavailable === 1 ? "s" : ""} at machine-local weights with no reinstall info — unavailable until reinstalled.`,
    );
  }
  if (w.credentialNames.length > 0) {
    lines.push(`Credentials to re-enter on the inference plane: ${w.credentialNames.join(", ")}.`);
  }
  if (w.needsSecret > 0 || w.needsOauth > 0) {
    const parts: string[] = [];
    if (w.needsSecret > 0) {
      parts.push(`${plural(w.needsSecret, "connection")} will need credentials re-entered`);
    }
    if (w.needsOauth > 0) {
      parts.push(`${plural(w.needsOauth, "OAuth connection")} will need re-authorizing`);
    }
    lines.push(`Secrets are never exported — ${parts.join(", and ")}.`);
  }
  if (w.needsEnv > 0) {
    lines.push(
      `${plural(w.needsEnv, "MCP server")} import${w.needsEnv === 1 ? "s" : ""} without env/header values — re-enter them before use.`,
    );
  }
  if (w.needsIngest > 0 || w.needsUpload > 0) {
    const parts: string[] = [];
    if (w.needsIngest > 0) parts.push(`${plural(w.needsIngest, "crawl source")} need re-crawling`);
    if (w.needsUpload > 0) {
      parts.push(`${plural(w.needsUpload, "upload source")} need documents re-uploaded`);
    }
    lines.push(`RAG content is never exported — ${parts.join("; ")}.`);
  }
  return lines;
}

// A per-section counter from the import report — the server names the keys; render whatever came
// back (numbers directly, count objects as k=v pairs) so a new counter never renders blank.
function describeCount(value: unknown): string {
  if (value == null) return "—";
  if (typeof value === "number" || typeof value === "string") return String(value);
  if (typeof value === "object" && !Array.isArray(value)) {
    const pairs = Object.entries(value as Record<string, unknown>);
    if (pairs.length === 0) return "—";
    return pairs.map(([k, v]) => `${k} ${String(v)}`).join(" · ");
  }
  return JSON.stringify(value);
}

function ImportCard() {
  const qc = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [fileName, setFileName] = useState<string | null>(null);
  const [preview, setPreview] = useState<ImportPreview | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [applying, setApplying] = useState(false);
  const [report, setReport] = useState<ApplyReport | null>(null);

  const pick = async (file: File) => {
    setReport(null);
    setPreview(null);
    setFileName(null);
    try {
      setPreview(await parseImportFile(file));
      setFileName(file.name);
    } catch (e) {
      notify.error(e instanceof Error ? e.message : "Could not read that file");
    }
  };

  const apply = async () => {
    if (!preview) return;
    setConfirming(false);
    setApplying(true);
    try {
      const rep = await applyImport(preview, qc);
      // Weight downloads the registry import kicked off get the persistent progress cards.
      for (const d of rep.downloads) {
        trackDownload({ id: d.jobId, logicalId: d.logicalId, repo: d.repo });
      }
      setReport(rep);
      setPreview(null);
      // A plane that failed mid-import (or any per-entity skip) lands in rep.warnings — the toast
      // must say "partial", never claim a clean success over a lossy apply.
      if (rep.warnings.length > 0) {
        notify.warning(`Imported with ${plural(rep.warnings.length, "warning")}`);
      } else {
        notify.success("Import complete");
      }
    } catch (e) {
      notify.error(e instanceof Error ? `Import failed: ${e.message}` : "Import failed");
    } finally {
      setApplying(false);
    }
  };

  const counts = preview
    ? Object.entries(preview.counts).filter(([, n]) => typeof n === "number")
    : [];
  const warningLines = preview ? previewWarningLines(preview) : [];

  return (
    <Card className="p-4">
      <SectionHeading>Import</SectionHeading>
      <p className="mt-1 text-xs text-muted-foreground">
        Pick a theygent export zip, or a single exported agent .json. You get a preview first —
        nothing is written until you confirm. Existing entities with matching ids are kept, never
        overwritten.
      </p>
      <input
        ref={fileRef}
        type="file"
        aria-label="Import file"
        accept=".zip,.json,application/zip,application/json"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void pick(f);
          e.target.value = "";
        }}
      />
      <div className="mt-3 flex items-center gap-2">
        <Button disabled={applying} onClick={() => fileRef.current?.click()}>
          Choose file…
        </Button>
        {fileName && <span className="mono text-xs text-muted-foreground">{fileName}</span>}
      </div>

      {preview && (
        <div className="mt-4 space-y-3 border-t border-border/60 pt-3">
          <SectionHeading>Preview — nothing imported yet</SectionHeading>
          <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-xs">
            {counts.map(([key, n]) => (
              <div key={key} className="contents">
                <dt className="text-muted-foreground">{COUNT_LABELS[key] ?? key}</dt>
                <dd className="text-foreground">{n}</dd>
              </div>
            ))}
          </dl>
          {warningLines.map((line) => (
            <NoteBanner key={line}>{line}</NoteBanner>
          ))}
          <div className="flex items-center gap-2">
            <Button variant="primary" disabled={applying} onClick={() => setConfirming(true)}>
              {applying ? "Importing…" : "Import…"}
            </Button>
            <Button
              variant="ghost"
              disabled={applying}
              onClick={() => {
                setPreview(null);
                setFileName(null);
              }}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}

      {report && (
        <div className="mt-4 space-y-3 border-t border-border/60 pt-3">
          <SectionHeading>Import report</SectionHeading>
          {report.control && (
            <dl className="grid grid-cols-[auto_1fr] gap-x-6 gap-y-1 text-xs">
              {Object.entries(report.control)
                .filter(([key]) => key !== "warnings")
                .map(([key, value]) => (
                  <div key={key} className="contents">
                    <dt className="text-muted-foreground">{COUNT_LABELS[key] ?? key}</dt>
                    <dd className="text-foreground">{describeCount(value)}</dd>
                  </div>
                ))}
            </dl>
          )}
          {report.inference && (
            <p className="text-xs text-foreground">
              Model registry: {report.inference.registered.length} registered ·{" "}
              {report.inference.skipped.length} skipped
              {report.downloads.length > 0 &&
                ` · ${plural(report.downloads.length, "weight download")} started`}
            </p>
          )}
          {report.inference && (report.inference.credentialNames?.length ?? 0) > 0 && (
            <p className="text-xs text-foreground">
              Credentials to re-enter on the inference plane:{" "}
              <span className="mono">{report.inference.credentialNames.join(", ")}</span>
            </p>
          )}
          {report.artifactsRestored > 0 && (
            <p className="text-xs text-foreground">
              {plural(report.artifactsRestored, "artifact")} restored.
            </p>
          )}
          {report.warnings.length > 0 && (
            <NoteBanner>
              <span>
                {report.warnings.map((w) => (
                  <span key={`${w.code}:${w.message}`} className="block">
                    <span className="mono">{w.code}</span> — {w.message}
                  </span>
                ))}
              </span>
            </NoteBanner>
          )}
        </div>
      )}

      {confirming && preview && (
        <ConfirmDialog
          title="Import this bundle?"
          message={
            <>
              Apply <span className="mono">{fileName}</span> to this install. Existing entities with
              matching ids are skipped — nothing is overwritten. Secrets are never part of a bundle,
              so imported connections and servers need their credentials re-entered afterwards.
            </>
          }
          confirmLabel="Import"
          onConfirm={() => void apply()}
          onCancel={() => setConfirming(false)}
        />
      )}
    </Card>
  );
}
