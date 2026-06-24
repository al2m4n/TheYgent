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
};
