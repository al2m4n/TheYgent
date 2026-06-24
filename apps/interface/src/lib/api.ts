// The ONE module that talks HTTP (mirrors the cockpit's M8 §3.3 discipline). It owns the base
// URL, the auth-header seam, and JSON error shaping. Every view calls through here — never
// `fetch` directly — so the later real-auth milestone is a one-file change.
//
// M15 consumes ONLY M11's registry endpoints (load/save an agent). It adds ZERO backend routes
// (M15 §3): if a needed endpoint were missing we'd flag it, not work around it client-side.

import type { IRDocument } from "@theygent/ir-types";

export const CONTROL_PLANE_URL =
  (import.meta.env.VITE_CONTROL_PLANE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8080";

// The inference plane is a SEPARATE, user-controlled service (the two-plane split). The interface
// reads its registered logical models DIRECTLY (like the cockpit) to populate the model picker —
// it is never proxied through the control-plane. Read-only `/admin/models`; declares no engine.
export const INFERENCE_URL =
  (import.meta.env.VITE_INFERENCE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8081";

// The static dev token the control-plane's no-op `require_auth` accepts (same seam as the
// cockpit). Centralised so swapping in a real token flow later touches only this function.
function authHeaders(): Record<string, string> {
  const token =
    (typeof localStorage !== "undefined" && localStorage.getItem("theygent.token")) || "dev-local";
  return { Authorization: `Bearer ${token}` };
}

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function toError(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code = "http_error";
  try {
    const body = await res.json();
    const err = body?.error ?? body;
    if (err?.message) message = err.message;
    if (err?.code) code = err.code;
  } catch {
    // non-JSON error body; keep the status line.
  }
  return new ApiError(message, res.status, code);
}

async function request<T>(base: string, path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${base}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) throw await toError(res);
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ── M11 registry wire shapes (snake_case — the run surface convention) ───────
// These are the registry's read/write envelopes, NOT the IR. The IR itself is always a typed
// `IRDocument` from @theygent/ir-types; these carry it plus version metadata.

export interface AgentVersionMeta {
  version: string;
  content_hash: string;
  seq: number;
  created_at: string;
}

export interface AgentSummary {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  latest_version: string | null;
  latest_content_hash: string | null;
  version_count: number;
}

export interface AgentDetail {
  id: string;
  name: string;
  created_at: string;
  updated_at: string;
  versions: AgentVersionMeta[]; // newest first
}

export interface StoredVersion {
  agent_id: string;
  version: string;
  content_hash: string;
  seq: number;
  created_at: string;
  // The stored IR is the canonical, view-stripped §8.2 document with `contentHash` stamped; the
  // layout lives in the separate `view` block (M11 §1.2). We re-attach `view` onto the IR before
  // handing it to the adapter (see lib/agent.ts).
  ir: IRDocument;
  view: Record<string, unknown> | null;
}

// ── inference-plane model view (camelCase — the /admin/* convention) ─────────
// A registered logical model the inference plane can serve. We surface its `logicalId` (what a
// graph model binding forwards to the seam) + the engine `binding` so a binding can be declared
// from it. We never send an engine name to the data plane — this is management-plane read-only.
export interface ModelView {
  logicalId: string;
  binding: { binding: string; model?: string; source?: string; [k: string]: unknown };
  state?: unknown;
}

// ── control-plane MCP server summary (camelCase) ─────────────────────────────
export interface McpServerSummary {
  name: string;
  transport: string;
  connected: boolean;
}

// ── inference-plane engines + capabilities (camelCase /admin/*) ──────────────
export interface EnginesView {
  maxResident: number;
  resident: unknown;
}

export interface Capabilities {
  maxContext?: number | null;
  toolCalling?: boolean;
  structuredOutput?: boolean;
  vision?: boolean;
  reasoning?: boolean;
  approximate?: boolean;
}

// ── M16 catalog (discovery + install) wire shapes — NOT IR, just /admin/* shapes ─
// These mirror the inference plane's normalized CatalogProvider types. They are catalog metadata,
// never the agent IR (the IR stays a typed @theygent/ir-types IRDocument), so M15's "no hand-written
// IR types" rule is untouched.
export type Fit = "fits" | "tight" | "too-large" | "unknown";

export interface CatalogVariant {
  id: string;
  label: string;
  engine: string;
  filename?: string | null;
  sizeBytes?: number | null;
  fit: Fit;
  recommended: boolean;
  quality?: string | null; // "balanced", "high quality", "full precision", …
  fitReason?: string | null; // "~5 GB needed · 16 GB RAM" (tooltip)
}

export interface CatalogEntry {
  provider: string;
  ref: string;
  title: string;
  description: string;
  category: string;
  kind: string;
  sovereignty: "in-domain" | "cloud-egress";
  engines: string[];
  badges: { downloads?: number; likes?: number; pipelineTag?: string; [k: string]: unknown };
  params?: string | null; // "7B" / "0.5B"
  license?: string | null; // "apache-2.0"
  gated?: boolean; // needs an HF token
  updatedAt?: string | null; // ISO timestamp
  installed?: boolean; // already in the local registry
  installedAs?: string | null; // the logical id it's installed under
  variants: CatalogVariant[];
}

export interface CatalogList {
  entries: CatalogEntry[];
  engines: string[]; // the engines the inference plane has ready (the filter that was applied)
}

export interface DownloadJob {
  id: string;
  logicalId: string;
  repo: string;
  engine: string;
  status: "downloading" | "registering" | "done" | "error" | "cancelled";
  doneBytes: number;
  totalBytes: number | null;
  error: string | null;
}

export const api = {
  listAgents: (params: { limit?: number; before?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.before) q.set("before", params.before);
    const qs = q.toString();
    return request<{ agents: AgentSummary[] }>(
      CONTROL_PLANE_URL,
      `/agents${qs ? `?${qs}` : ""}`,
    ).then((r) => r.agents);
  },

  getAgent: (id: string) =>
    request<AgentDetail>(CONTROL_PLANE_URL, `/agents/${encodeURIComponent(id)}`),

  getAgentVersion: (id: string, version: string) =>
    request<StoredVersion>(
      CONTROL_PLANE_URL,
      `/agents/${encodeURIComponent(id)}/versions/${encodeURIComponent(version)}`,
    ),

  // Create a new agent + its first version. The id/version come FROM the IR (M11 §1.1). The body
  // is `{ ir }` where `ir` carries its `view` block; the server strips `view`, hashes, persists.
  createAgent: (body: { ir: IRDocument; name?: string }) =>
    request<AgentDetail>(CONTROL_PLANE_URL, "/agents", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Add a new immutable version to an existing agent (the IR's `version` is the new coordinate).
  addAgentVersion: (id: string, body: { ir: IRDocument }) =>
    request<AgentDetail>(CONTROL_PLANE_URL, `/agents/${encodeURIComponent(id)}/versions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Inference plane (separate base URL): the registered logical models, to populate the model
  // picker. Read-only; tolerated to fail (the picker falls back to free text if unreachable).
  listModels: () =>
    request<{ models: ModelView[] }>(INFERENCE_URL, "/admin/models").then((r) => r.models),

  // Control-plane registered MCP servers — to populate the mcp_tool `server` picker.
  listMcpServers: () =>
    request<{ servers: McpServerSummary[] }>(CONTROL_PLANE_URL, "/admin/mcp/servers").then(
      (r) => r.servers,
    ),

  // ── inference plane: model + engine registry (the "Installed" tab) ─────────
  // Reuses the cockpit's M8 surface verbatim (no new routes for these): register / lifecycle /
  // capabilities all on the user-controlled inference plane, called directly.
  getEngines: () => request<EnginesView>(INFERENCE_URL, "/admin/engines"),

  getModelCapabilities: (logicalId: string) =>
    request<Capabilities>(
      INFERENCE_URL,
      `/admin/models/${encodeURIComponent(logicalId)}/capabilities`,
    ),

  putModel: (logicalId: string, body: unknown) =>
    request<ModelView>(INFERENCE_URL, `/admin/models/${encodeURIComponent(logicalId)}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  deleteModel: (logicalId: string) =>
    request<void>(INFERENCE_URL, `/admin/models/${encodeURIComponent(logicalId)}`, {
      method: "DELETE",
    }),

  warmModel: (logicalId: string) =>
    request<ModelView>(INFERENCE_URL, `/admin/models/${encodeURIComponent(logicalId)}:warm`, {
      method: "POST",
    }),

  evictModel: (logicalId: string) =>
    request<ModelView>(INFERENCE_URL, `/admin/models/${encodeURIComponent(logicalId)}:evict`, {
      method: "POST",
    }),

  // ── inference plane: M16 catalog (the "Discover" tab) ──────────────────────
  // Discovery + install live in the inference plane (the user's trust domain). Install downloads
  // weights HERE and registers them locally — theygent never sees the bytes (the sovereignty promise).
  searchCatalogModels: (
    params: {
      search?: string;
      sort?: string;
      limit?: number;
      engines?: string[]; // narrow to a subset of ready engines
      size?: string; // "small" | "medium" | "large" — param-size filter
    } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.search) q.set("search", params.search);
    if (params.sort) q.set("sort", params.sort);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.engines?.length) q.set("engines", params.engines.join(","));
    if (params.size) q.set("size", params.size);
    const qs = q.toString();
    return request<CatalogList>(INFERENCE_URL, `/admin/catalog/models${qs ? `?${qs}` : ""}`);
  },

  // `ref` is a provider id like "org/name"; the route is a {repo:path} converter, so the slash is
  // part of the path — do NOT percent-encode it (that would break the match).
  getCatalogModel: (ref: string) =>
    request<CatalogEntry>(INFERENCE_URL, `/admin/catalog/models/${ref}`),

  installCatalogModel: (body: {
    repo: string;
    engine: string;
    variantId: string;
    logicalId: string;
  }) =>
    request<DownloadJob>(INFERENCE_URL, "/admin/catalog/install", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  getDownload: (id: string) =>
    request<DownloadJob>(INFERENCE_URL, `/admin/catalog/downloads/${encodeURIComponent(id)}`),

  listDownloads: () =>
    request<{ downloads: DownloadJob[] }>(INFERENCE_URL, "/admin/catalog/downloads").then(
      (r) => r.downloads,
    ),

  cancelDownload: (id: string) =>
    request<DownloadJob>(
      INFERENCE_URL,
      `/admin/catalog/downloads/${encodeURIComponent(id)}:cancel`,
      { method: "POST" },
    ),
};
