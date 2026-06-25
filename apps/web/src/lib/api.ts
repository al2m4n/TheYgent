// The ONE module that talks HTTP (M8 §3.3). It owns the base URLs, the auth header seam,
// JSON error shaping, and SSE streaming. Every view calls through here — never `fetch`
// directly — so the later real-auth milestone is a one-file change, not an audit.

import { type SSEEvent, readSSE } from "./sse";
import type {
  AgentDetail,
  AgentSummary,
  Capabilities,
  CaptureLevel,
  EnginesView,
  IoPolicy,
  McpServerSummary,
  McpServerView,
  McpToolView,
  ModelView,
  NodeIo,
  Run,
  Span,
  StoredVersion,
  ThreadDetail,
  ThreadSummary,
} from "./types";

// Two backends, two base URLs (M8: the cockpit talks to both directly; it does not proxy
// the user-controlled inference plane through the control-plane).
export const CONTROL_PLANE_URL =
  (import.meta.env.VITE_CONTROL_PLANE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8080";
export const INFERENCE_URL =
  (import.meta.env.VITE_INFERENCE_URL as string | undefined)?.replace(/\/$/, "") ??
  "http://localhost:8081";

// The static dev token the control-plane's no-op `require_auth` accepts (M8 §3.3). The seam
// is centralized here so swapping in a real token flow later touches only this function.
function authHeaders(): Record<string, string> {
  const token = localStorage.getItem("theygent.token") ?? "dev-local";
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
    // control-plane: {error:{message,code}}; inference plane: {error:{message,code,type}}.
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

// ── control-plane reads ─────────────────────────────────────────────────────

export const api = {
  listRuns: (params: { limit?: number; before?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.before) q.set("before", params.before);
    const qs = q.toString();
    return request<{ runs: Run[] }>(CONTROL_PLANE_URL, `/runs${qs ? `?${qs}` : ""}`).then(
      (r) => r.runs,
    );
  },

  getRun: (id: string) => request<Run>(CONTROL_PLANE_URL, `/runs/${id}`),

  listThreads: (params: { limit?: number; before?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.before) q.set("before", params.before);
    const qs = q.toString();
    return request<{ threads: ThreadSummary[] }>(
      CONTROL_PLANE_URL,
      `/threads${qs ? `?${qs}` : ""}`,
    ).then((r) => r.threads);
  },

  getThread: (id: string) => request<ThreadDetail>(CONTROL_PLANE_URL, `/threads/${id}`),

  // ── control-plane: agent registry (M11) ───────────────────────────────────

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

  getAgent: (id: string) => request<AgentDetail>(CONTROL_PLANE_URL, `/agents/${id}`),

  getAgentVersion: (id: string, version: string) =>
    request<StoredVersion>(CONTROL_PLANE_URL, `/agents/${id}/versions/${version}`),

  // Create a new agent + its first version from an IR (the agent id/version come FROM the IR).
  createAgent: (body: { ir: unknown; name?: string }) =>
    request<AgentDetail>(CONTROL_PLANE_URL, "/agents", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Add a new immutable version to an existing agent (the IR's version is the new coordinate).
  addAgentVersion: (id: string, body: { ir: unknown }) =>
    request<AgentDetail>(CONTROL_PLANE_URL, `/agents/${id}/versions`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // ── control-plane: observability (M17 — the run waterfall) ────────────────

  // The waterfall payload: the run's span tree (timing + status + worker attribution + edge sizes).
  // NO payloads — those are the lazy /io call below (§1.3).
  getTrace: (runId: string) =>
    request<{ runId: string; status: string; spans: Span[] }>(
      CONTROL_PLANE_URL,
      `/runs/${runId}/trace`,
    ).then((r) => r.spans),

  // The click-through: the full per-node I/O. Gated states (off/metadata/not-permitted) come back
  // as a 200 with inputs/outputs null + a `reason` — never an error, so the timeline stays legible.
  getNodeIo: (runId: string, nodeId: string) =>
    request<NodeIo>(CONTROL_PLANE_URL, `/runs/${runId}/nodes/${encodeURIComponent(nodeId)}/io`),

  getIoPolicy: (agentId: string) =>
    request<IoPolicy>(CONTROL_PLANE_URL, `/agents/${agentId}/io-policy`),

  putIoPolicy: (
    agentId: string,
    body: { io_capture: CaptureLevel; io_retention_seconds?: number | null },
  ) =>
    request<IoPolicy>(CONTROL_PLANE_URL, `/agents/${agentId}/io-policy`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  // ── inference plane: model + engine registry ──────────────────────────────

  listModels: () =>
    request<{ models: ModelView[] }>(INFERENCE_URL, "/admin/models").then((r) => r.models),

  getEngines: () => request<EnginesView>(INFERENCE_URL, "/admin/engines"),

  // On-demand only (this can warm a managed engine to probe it — §9.1.2), never on list load.
  getModelCapabilities: (logicalId: string) =>
    request<Capabilities>(INFERENCE_URL, `/admin/models/${logicalId}/capabilities`),

  putModel: (logicalId: string, body: unknown) =>
    request<ModelView>(INFERENCE_URL, `/admin/models/${logicalId}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  deleteModel: (logicalId: string) =>
    request<void>(INFERENCE_URL, `/admin/models/${logicalId}`, { method: "DELETE" }),

  warmModel: (logicalId: string) =>
    request<ModelView>(INFERENCE_URL, `/admin/models/${logicalId}:warm`, { method: "POST" }),

  evictModel: (logicalId: string) =>
    request<ModelView>(INFERENCE_URL, `/admin/models/${logicalId}:evict`, { method: "POST" }),

  // ── control-plane: MCP server registry ────────────────────────────────────

  listMcpServers: () =>
    request<{ servers: McpServerSummary[] }>(CONTROL_PLANE_URL, "/admin/mcp/servers").then(
      (r) => r.servers,
    ),

  getMcpServer: (name: string) =>
    request<McpServerView>(CONTROL_PLANE_URL, `/admin/mcp/servers/${name}`),

  putMcpServer: (name: string, body: unknown) =>
    request<McpServerView>(CONTROL_PLANE_URL, `/admin/mcp/servers/${name}`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

  deleteMcpServer: (name: string) =>
    request<void>(CONTROL_PLANE_URL, `/admin/mcp/servers/${name}`, { method: "DELETE" }),

  getMcpTools: (name: string) =>
    request<{ tools: McpToolView[] }>(CONTROL_PLANE_URL, `/admin/mcp/servers/${name}/tools`).then(
      (r) => r.tools,
    ),

  warmMcpServer: (name: string) =>
    request<McpServerView>(CONTROL_PLANE_URL, `/admin/mcp/servers/${name}:warm`, {
      method: "POST",
    }),

  closeMcpServer: (name: string) =>
    request<McpServerView>(CONTROL_PLANE_URL, `/admin/mcp/servers/${name}:close`, {
      method: "POST",
    }),
};

// ── streaming (M8 §3.2): POST + read the SSE body on the same request ────────

export interface StreamHandle {
  /** Async iterator of parsed SSE events. */
  events: AsyncGenerator<SSEEvent>;
  /** Abort the in-flight request/stream. */
  abort: () => void;
}

/**
 * POST a run (or graph run) and stream its SSE body. Returns immediately with an event
 * iterator so the caller drives the UI. A pre-stream error (non-2xx) is thrown before the
 * first event, exactly as the backend surfaces 503/404/400 before the 200 SSE (M3/M5).
 */
export async function streamRun(
  // /runs, /graphs/runs, or M11's /agents/{id}/runs (invoke-by-reference) — all share the SSE shape.
  path: string,
  body: unknown,
): Promise<StreamHandle> {
  const controller = new AbortController();
  const res = await fetch(`${CONTROL_PLANE_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
    signal: controller.signal,
  });
  if (!res.ok) throw await toError(res);
  if (!res.body) throw new ApiError("response had no body", 502, "no_body");
  return { events: readSSE(res.body, controller.signal), abort: () => controller.abort() };
}

/**
 * GET an SSE stream (M17: the live trace waterfall `/runs/{id}/trace/stream`). Same frame parser as
 * `streamRun`, but a GET — the run executes elsewhere; this only observes its span open/close events.
 */
export async function streamGet(path: string): Promise<StreamHandle> {
  const controller = new AbortController();
  const res = await fetch(`${CONTROL_PLANE_URL}${path}`, {
    headers: { ...authHeaders() },
    signal: controller.signal,
  });
  if (!res.ok) throw await toError(res);
  if (!res.body) throw new ApiError("response had no body", 502, "no_body");
  return { events: readSSE(res.body, controller.signal), abort: () => controller.abort() };
}
