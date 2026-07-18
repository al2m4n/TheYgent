// Export/import orchestration: the one-zip transfer artifact is assembled and unpacked HERE, in
// the browser. Registry state lives in the inference plane's local files and never transits the
// control plane, while agents/runs/sessions live in control-plane Postgres — the browser talks to
// both planes directly, so it is the only place the two halves can meet. All HTTP goes through
// lib/api (the one seam); this module owns the zip layout, the preview model, and apply sequencing.
//
// Zip layout (formatVersion 1):
//   manifest.json          {formatVersion, exportedAt, parts, artifacts, counts}
//   control-plane.json     the snake_case control bundle, verbatim
//   inference/models.json  the camelCase inference bundle
//   artifacts/<ref>        raw artifact bytes, one file per exported ref

import type { QueryClient } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { strFromU8, strToU8, unzipSync, zipSync } from "fflate";
import {
  ApiError,
  type ArtifactRef,
  type BundleAgent,
  type BundleAgentVersion,
  type ControlBundle,
  type ControlImportReport,
  type InferenceBundle,
  type InferenceImportResult,
  type TransferWarning,
  api,
} from "./api";

// What the Export card can select. The first six map onto the control plane's /export include
// vocabulary; "registries" is the inference-plane half and never crosses the control plane.
export type ExportSection =
  | "agents"
  | "runs"
  | "traces"
  | "sessions"
  | "mcp"
  | "rag"
  | "registries";

const CONTROL_SECTIONS: ExportSection[] = ["agents", "runs", "traces", "sessions", "mcp", "rag"];

// The manifest is the zip's own index — parts present, artifact content types (a raw
// `artifacts/<ref>` entry carries no mime of its own), and per-section counts for the preview.
interface Manifest {
  formatVersion: number;
  exportedAt: string;
  parts: ("control" | "inference")[];
  artifacts: { ref: string; contentType: string }[];
  counts: Record<string, number>;
}

/** Anchor-download a blob. The one save-to-disk primitive in the app — every export uses it. */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// jsdom's Blob implements FileReader but not .arrayBuffer(), so the fallback keeps the zip
// round-trip testable; browsers take the direct call.
async function blobBytes(blob: Blob): Promise<Uint8Array> {
  if (typeof blob.arrayBuffer === "function") return new Uint8Array(await blob.arrayBuffer());
  return await new Promise<Uint8Array>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer));
    reader.onerror = () => reject(reader.error ?? new Error("could not read the file"));
    reader.readAsArrayBuffer(blob);
  });
}

const BUNDLE_SECTIONS = [
  "agents",
  "drafts",
  "runs",
  "spans",
  "node_io",
  "artifact_refs",
  "sessions",
  "connections",
  "mcp_servers",
  "rag_sources",
] as const;

function bundleCounts(control: ControlBundle | null): Record<string, number> {
  const counts: Record<string, number> = {};
  if (!control) return counts;
  for (const key of BUNDLE_SECTIONS) {
    const rows = control[key];
    if (Array.isArray(rows)) counts[key] = rows.length;
  }
  return counts;
}

export interface ExportResult {
  filename: string;
  blob: Blob;
  counts: Record<string, number>;
  /** Non-fatal skips (e.g. an artifact that no longer exists) — surfaced, never silent. */
  notes: string[];
}

// Zip the assembled halves and hand the file to the browser's download flow.
function finishZip(
  control: ControlBundle | null,
  inference: InferenceBundle | null,
  artifacts: { ref: ArtifactRef; bytes: Uint8Array }[],
  notes: string[],
): ExportResult {
  const parts: Manifest["parts"] = [];
  if (control) parts.push("control");
  if (inference) parts.push("inference");
  const counts = {
    ...bundleCounts(control),
    ...(inference ? { models: inference.models.length } : {}),
  };
  const manifest: Manifest = {
    formatVersion: 1,
    exportedAt: new Date().toISOString(),
    parts,
    artifacts: artifacts.map((a) => ({ ref: a.ref.ref, contentType: a.ref.content_type })),
    counts,
  };
  const files: Record<string, Uint8Array> = {
    "manifest.json": strToU8(JSON.stringify(manifest, null, 2)),
  };
  if (control) files["control-plane.json"] = strToU8(JSON.stringify(control));
  if (inference) files["inference/models.json"] = strToU8(JSON.stringify(inference));
  for (const a of artifacts) files[`artifacts/${a.ref.ref}`] = a.bytes;

  const blob = new Blob([zipSync(files)], { type: "application/zip" });
  const filename = `theygent-export-${new Date().toISOString().slice(0, 10)}.zip`;
  saveBlob(blob, filename);
  return { filename, blob, counts, notes };
}

/**
 * Export the selected sections as one zip: the control bundle from POST /export, the inference
 * bundle from GET /admin/export when the registry is selected, plus the raw bytes of every
 * exported artifact ref. Triggers the anchor download and returns what was written.
 */
export async function buildExportZip(selection: ExportSection[]): Promise<ExportResult> {
  const include = CONTROL_SECTIONS.filter((s) => selection.includes(s));
  const wantRegistries = selection.includes("registries");
  if (include.length === 0 && !wantRegistries) throw new Error("nothing selected to export");

  const notes: string[] = [];
  const control = include.length > 0 ? await api.exportControlBundle(include) : null;
  const inference = wantRegistries ? await api.exportInferenceBundle() : null;

  // Artifact bytes referenced by the exported traces. 404-tolerant: an artifact pruned since its
  // run is a note in the result, not a failed export.
  const artifacts: { ref: ArtifactRef; bytes: Uint8Array }[] = [];
  for (const ref of control?.artifact_refs ?? []) {
    try {
      const blob = await api.downloadArtifact(ref.ref);
      artifacts.push({ ref, bytes: await blobBytes(blob) });
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        notes.push(`Artifact ${ref.ref} no longer exists — skipped.`);
        continue;
      }
      throw e;
    }
  }

  return finishZip(control, inference, artifacts, notes);
}

/**
 * Export a chosen set of agents (every version, oldest first) as an agents-only bundle zip —
 * the Agents page's bulk export. Importable through the Settings Import card like any bundle.
 */
export async function buildAgentsExportZip(ids: string[]): Promise<ExportResult> {
  const agents: BundleAgent[] = [];
  for (const id of ids) {
    const detail = await api.getAgent(id);
    const versions: BundleAgentVersion[] = [];
    // AgentDetail.versions is newest-first; the bundle carries seq order (oldest first).
    for (const meta of [...detail.versions].reverse()) {
      const sv = await api.getAgentVersion(id, meta.version);
      versions.push({
        version: sv.version,
        content_hash: sv.content_hash,
        created_at: sv.created_at,
        ir: sv.ir,
        view: sv.view,
      });
    }
    agents.push({
      id: detail.id,
      name: detail.name,
      created_at: detail.created_at,
      updated_at: detail.updated_at,
      versions,
      io_policy: null,
      triggers: [],
    });
  }
  const control: ControlBundle = {
    format_version: 1,
    exported_at: new Date().toISOString(),
    agents,
  };
  return finishZip(control, null, [], []);
}

// ── import: parse → preview → apply ─────────────────────────────────────────

/** What the preview must say LOUDLY before anything is written (secrets/weights never travel). */
export interface PreviewWarnings {
  /** Models whose weights will be (re)downloaded from Hugging Face after registration. */
  modelDownloads: number;
  /** Models registered against a machine-local path with no reinstall metadata — 503 at spawn. */
  weightsUnavailable: number;
  /** Connections whose secret must be re-entered (secrets never ride a bundle). */
  needsSecret: number;
  /** OAuth connections that must be re-authorized on the target. */
  needsOauth: number;
  /** MCP servers whose env/header VALUES must be re-entered (only key names travel). */
  needsEnv: number;
  /** Crawl sources that must be re-ingested (the corpus never travels). */
  needsIngest: number;
  /** Upload sources whose documents must be re-uploaded. */
  needsUpload: number;
  /** Credential NAMES the source inference plane held (values never travel) — re-enter on the target. */
  credentialNames: string[];
}

export interface ImportPreview {
  source: "zip" | "json";
  control: ControlBundle | null;
  inference: InferenceBundle | null;
  artifacts: { ref: string; contentType: string; bytes: Uint8Array }[];
  counts: Record<string, number>;
  warnings: PreviewWarnings;
}

function deriveWarnings(
  control: ControlBundle | null,
  inference: InferenceBundle | null,
): PreviewWarnings {
  const models = inference?.models ?? [];
  const connections = control?.connections ?? [];
  const rag = control?.rag_sources ?? [];
  return {
    // Catalog installs re-download immediately; hf-source bindings fetch lazily at first spawn —
    // both mean weight bytes coming from Hugging Face, so the preview counts them together.
    modelDownloads: models.filter((m) => m.install != null || m.binding?.source === "hf").length,
    weightsUnavailable: models.filter(
      (m) => m.install == null && m.binding?.source === "local-path",
    ).length,
    needsSecret: connections.filter((c) => c.has_secret).length,
    needsOauth: connections.filter(
      (c) => (c.config?.auth as { type?: string } | undefined)?.type === "oauth",
    ).length,
    needsEnv: (control?.mcp_servers ?? []).filter(
      (s) => (s.env_keys?.length ?? 0) > 0 || (s.header_keys?.length ?? 0) > 0,
    ).length,
    needsIngest: rag.filter((r) => r.kind === "crawl").length,
    needsUpload: rag.filter((r) => r.kind === "upload").length,
    credentialNames: inference?.credentialNames ?? [],
  };
}

// Wrap a bare exported agent document (the Agents-page single-file export: an IR with its view
// embedded) into an agents-only control bundle. The layout splits back out — bundle versions
// carry a view-stripped ir + view apart, exactly as the registry stores them; the server
// recomputes the hash regardless.
function wrapAgentDocument(doc: unknown): ControlBundle {
  if (doc === null || typeof doc !== "object" || Array.isArray(doc)) {
    throw new Error("Not a JSON object — expected an exported agent document.");
  }
  const ir = doc as IRDocument;
  if (
    typeof ir.id !== "string" ||
    typeof ir.version !== "string" ||
    typeof ir.schemaVersion !== "string"
  ) {
    throw new Error("Not an agent document — it is missing id, version, or schemaVersion.");
  }
  const { view, ...stripped } = ir;
  const now = new Date().toISOString();
  return {
    format_version: 1,
    exported_at: now,
    agents: [
      {
        id: ir.id,
        name: ir.name || ir.id,
        created_at: now,
        updated_at: now,
        versions: [
          {
            version: ir.version,
            content_hash: typeof ir.contentHash === "string" ? ir.contentHash : "",
            created_at: now,
            ir: stripped as IRDocument,
            view: (view as Record<string, unknown> | undefined) ?? null,
          },
        ],
        io_policy: null,
        triggers: [],
      },
    ],
  };
}

/**
 * Parse a picked file into the preview model. Accepts the transfer zip, or a bare `.json` agent
 * export (wrapped into an agents-only bundle client-side). Nothing is written yet — the caller
 * shows the counts + warnings and asks for confirmation first.
 */
export async function parseImportFile(file: File): Promise<ImportPreview> {
  const bytes = await blobBytes(file);

  if (file.name.toLowerCase().endsWith(".json")) {
    let doc: unknown;
    try {
      doc = JSON.parse(strFromU8(bytes));
    } catch {
      throw new Error(`${file.name} is not valid JSON.`);
    }
    const control = wrapAgentDocument(doc);
    return {
      source: "json",
      control,
      inference: null,
      artifacts: [],
      counts: bundleCounts(control),
      warnings: deriveWarnings(control, null),
    };
  }

  let entries: Record<string, Uint8Array>;
  try {
    entries = unzipSync(bytes);
  } catch {
    throw new Error(`${file.name} is not a readable zip archive.`);
  }
  const readJson = <T>(name: string): T | null => {
    const raw = entries[name];
    if (!raw) return null;
    try {
      return JSON.parse(strFromU8(raw)) as T;
    } catch {
      throw new Error(`${name} inside the archive is not valid JSON.`);
    }
  };

  const manifest = readJson<Manifest>("manifest.json");
  const control = readJson<ControlBundle>("control-plane.json");
  const inference = readJson<InferenceBundle>("inference/models.json");
  if (!control && !inference) {
    throw new Error("The archive contains no theygent bundle (no control or inference part).");
  }

  const typeByRef = new Map(
    (manifest?.artifacts ?? []).map((a) => [a.ref, a.contentType] as const),
  );
  const artifacts = Object.entries(entries)
    .filter(([name]) => name.startsWith("artifacts/") && name !== "artifacts/")
    .map(([name, data]) => {
      const ref = name.slice("artifacts/".length);
      return {
        ref,
        contentType: typeByRef.get(ref) ?? "application/octet-stream",
        bytes: data,
      };
    });

  return {
    source: "zip",
    control,
    inference,
    artifacts,
    counts: {
      ...bundleCounts(control),
      ...(inference ? { models: inference.models.length } : {}),
    },
    warnings: deriveWarnings(control, inference),
  };
}

export interface ApplyReport {
  control: ControlImportReport | null;
  inference: InferenceImportResult | null;
  artifactsRestored: number;
  /** Every warning from both planes plus any artifact-restore failure, in one flat list. */
  warnings: TransferWarning[];
  /** In-plane weight downloads the import kicked off — for the caller to track/poll. */
  downloads: { jobId: string; logicalId: string; repo: string }[];
}

/**
 * Apply a parsed preview: control bundle first, then the artifact bytes (preserve-ref), then the
 * inference bundle — and invalidate every list the import may have grown. Per-entity skips and
 * conflicts come back in the merged report; only a transport-level failure throws.
 */
export async function applyImport(preview: ImportPreview, qc: QueryClient): Promise<ApplyReport> {
  const warnings: TransferWarning[] = [];

  let control: ControlImportReport | null = null;
  if (preview.control) {
    control = await api.importControlBundle(preview.control);
    warnings.push(...(control.warnings ?? []));
  }

  let artifactsRestored = 0;
  for (const a of preview.artifacts) {
    try {
      // fflate's views are typed over ArrayBufferLike; Blob wants a concrete ArrayBuffer — the
      // slice() copy re-homes the bytes and satisfies both.
      await api.putArtifact(
        a.ref,
        new Blob([a.bytes.slice()], { type: a.contentType }),
        a.contentType,
      );
      artifactsRestored += 1;
    } catch (e) {
      // One unwritable artifact must not abort the rest — report it and keep going.
      warnings.push({
        code: "artifact_restore_failed",
        message: `artifact ${a.ref}: ${e instanceof Error ? e.message : String(e)}`,
      });
    }
  }

  let inference: InferenceImportResult | null = null;
  if (preview.inference) {
    try {
      // The inference half posts verbatim: formatVersion keeps the server's version fence engaged,
      // credentialNames echo back so the report can name what must be re-entered on the target.
      inference = await api.importInferenceBundle({
        formatVersion: preview.inference.formatVersion,
        models: preview.inference.models,
        credentialNames: preview.inference.credentialNames,
      });
      for (const w of inference.warnings ?? []) {
        warnings.push({ code: w.code, message: `${w.logicalId}: ${w.message}` });
      }
    } catch (e) {
      // The control half is already applied — a dead inference plane must not discard its report,
      // the restored-artifact count, or the query invalidations. Partial success, said loudly.
      warnings.push({
        code: "inference_import_failed",
        message: `inference plane: ${e instanceof Error ? e.message : String(e)}`,
      });
    }
  }

  // Everything an import can create: registry lists, operator lists, config lists — both planes.
  for (const key of [
    ["agents"],
    ["drafts"],
    ["runs"],
    ["sessions"],
    ["connections"],
    ["mcpServers"],
    ["ragSources"],
    ["triggers"],
    ["models"],
    ["engines"],
    ["stats"],
  ]) {
    qc.invalidateQueries({ queryKey: key });
  }

  return {
    control,
    inference,
    artifactsRestored,
    warnings,
    downloads: inference?.downloads ?? [],
  };
}
