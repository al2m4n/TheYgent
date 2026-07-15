// The ONE module that talks HTTP. It owns the base URL, the auth-header seam, and JSON error
// shaping. Every view calls through here — never `fetch` directly — so the real-auth swap is a
// one-file change.
//
// This module consumes ONLY the registry endpoints (load/save an agent). It adds ZERO backend
// routes: if a needed endpoint were missing we'd flag it, not work around it client-side.

import type { IRDocument } from "@theygent/ir-types";
import type {
  CaptureLevel,
  IoPolicy,
  NodeIo,
  Run,
  SessionDetail,
  SessionMessage,
  SessionSummary,
  Span,
} from "./runtypes";
import { type SSEEvent, readSSE } from "./sse";

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

// ── registry wire shapes (snake_case — the run surface convention) ───────────
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
  // The stored IR is the canonical, view-stripped document with `contentHash` stamped; the
  // layout lives in the separate `view` block. We re-attach `view` onto the IR before handing
  // it to the adapter (see lib/agent.ts).
  ir: IRDocument;
  view: Record<string, unknown> | null;
}

// ── inference-plane model view (camelCase — the /admin/* convention) ─────────
// A registered logical model the inference plane can serve. We surface its `logicalId` (what a
// graph model binding forwards to the seam) + the engine `binding` so a binding can be declared
// from it. We never send an engine name to the data plane — this is management-plane read-only.
export interface ModelView {
  logicalId: string;
  binding: {
    binding: string;
    model?: string;
    source?: string;
    /** The registered task: chat | vision | embeddings | audio.transcription | audio.speech. */
    modality?: string;
    [k: string]: unknown;
  };
  state?: unknown;
}

// ── control-plane MCP server summary (camelCase) ─────────────────────────────
export interface McpServerSummary {
  name: string;
  transport: string;
  connected: boolean;
  /** A remote (http/sse) registration's endpoint; null for stdio. */
  url?: string | null;
  /** Header KEYS only — values never round-trip the wire (the sovereignty rule). */
  headerKeys?: string[];
}

// The PUT /admin/mcp/servers/{name} body — a name-keyed server registration. Stdio uses
// command/args/env/cwd (`env` carries the user's secrets/paths into the spawned subprocess; it
// stays in the user's trust domain); http/sse use url/headers. Generated (openapi/graphql)
// servers are NOT registered here — they are mcp_server connections (POST /connections).
export interface McpServerConfig {
  transport: "stdio" | "http" | "sse";
  command?: string | null;
  args?: string[];
  env?: Record<string, string> | null;
  cwd?: string | null;
  url?: string | null;
  headers?: Record<string, string> | null;
}

export interface McpToolDescriptor {
  name: string;
  description?: string | null;
  inputSchema?: Record<string, unknown> | null;
}

// ── MCP hub catalog (browse/install servers from a registry) — camelCase /admin/* wire ───────────
// Hand-mirrored catalog metadata (like CatalogEntry for models) — never the IR. Browse is
// metadata-only; install lands as an `mcp_server` connection with `config.origin` stamped.

export interface McpRegistryInfo {
  id: string;
  label: string;
  url: string;
}

export interface McpCatalogEntry {
  registry: string;
  name: string;
  title: string | null;
  description: string;
  version: string;
  status: string;
  isLatest: boolean;
  updatedAt: string | null;
  repositoryUrl: string | null;
  websiteUrl: string | null;
  stars: number | null;
  transports: string[];
  packageTypes: string[];
  deprecationMessage: string | null;
  installed: boolean;
  installedAs: string | null;
  installedConnection: string | null;
}

export interface McpCatalogPage {
  entries: McpCatalogEntry[];
  nextCursor: string | null;
}

// One value the user must (or may) supply before a candidate installs. `target` says where it
// lands: an env var, a launch arg, a request header, or the url. `secret` values are routed to
// the encrypted store server-side — the install form only ever sends them once.
export interface McpInstallInput {
  name: string;
  description: string | null;
  required: boolean;
  secret: boolean;
  default: string | null;
  choices: string[] | null;
  placeholder: string | null;
  target: "env" | "arg" | "header" | "url";
}

export interface McpInstallCandidate {
  id: string;
  kind: "stdio" | "http" | "sse";
  label: string;
  inputs: McpInstallInput[];
  supportsOauth: boolean;
  warnings: string[];
  command: string | null;
  args: string[];
  url: string | null;
  [k: string]: unknown;
}

export interface McpCatalogDetail {
  entry: McpCatalogEntry;
  candidates: McpInstallCandidate[];
}

// The tools a generated (openapi/graphql) server WOULD expose — nothing is created by a preview.
// `url` is the upstream base the server derived (e.g. from the spec's servers list).
export interface GeneratedPreview {
  tools: McpToolDescriptor[];
  url: string | null;
}

export interface McpOauthStart {
  status: "authorized" | "pending" | "error";
  authorizationUrl?: string;
  error?: string;
}

export interface McpOauthStatus {
  authorized: boolean;
  pending: boolean;
  lastError: string | null;
  connected: boolean;
}

// ── connection resource (the tool/MCP auth seam) ─────────────────────────────
// A server-side auth + config binding the IR references by id from its `tools` block. The wire shape
// is `public_dump`: NON-SECRET `config` only, plus `hasSecret` (never the secret or its ref). The
// `secret` is WRITE-ONLY on create/patch — it is encrypted server-side and NEVER returned.
export type ConnectionKind = "http_auth" | "mcp_server";

export interface ConnectionRecord {
  id: string;
  name: string;
  kind: ConnectionKind;
  config: Record<string, unknown>;
  hasSecret: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface CreateConnectionBody {
  name: string;
  kind: ConnectionKind;
  config?: Record<string, unknown>;
  secret?: string; // write-only — goes to the encrypted secret store, never echoed back
  enabled?: boolean;
}

// ── trigger (the deploy primitive — surfaced on the input node) ──────────────
export type TriggerKind = "http" | "schedule" | "webhook";

export interface TriggerRecord {
  id: string;
  agent_id: string;
  version: string | null;
  content_hash: string | null;
  kind: TriggerKind;
  config: Record<string, unknown>; // secret redacted to "***" server-side
  enabled: boolean;
  last_fired_at: string | null;
  created_at: string;
  updated_at: string;
}

// ── retrieval sources (RAG — snake_case control-plane wire) ──────────────────
// A named document collection agents retrieve from. Ingestion (a crawl or an upload batch) runs as
// a background job server-side; the UI polls `getRagSource` while `status === "ingesting"` (the
// same pattern as model-download polling). The `rag` node references a source by its STABLE id, so
// re-ingesting never changes an agent's hash.
export type RagSourceKind = "upload" | "crawl";
export type RagSourceStatus = "empty" | "ingesting" | "ready" | "failed" | "cancelled";

export interface RagIngestProgress {
  pages?: number;
  documents?: number;
  chunks?: number;
  unchanged?: number;
}

export interface RagSource {
  id: string;
  name: string;
  kind: RagSourceKind;
  config: Record<string, unknown>;
  embedding_model: string;
  embedding_dim: number | null;
  status: RagSourceStatus;
  error: string | null;
  progress: RagIngestProgress | null;
  documents: number;
  chunks: number;
  created_at: string;
  updated_at: string;
}

export interface RagDocument {
  id: string;
  source_id: string;
  uri: string;
  title: string;
  status: "pending" | "embedded" | "failed";
  error: string | null;
  chars: number;
  chunks: number;
  created_at: string;
  updated_at: string;
}

export interface RagQueryMatch {
  chunk_id: string;
  document_id: string;
  uri: string;
  title: string | null;
  heading: string | null;
  position: number;
  text: string;
  score: number;
  similarity: number | null;
}

export interface RagQueryResult {
  source_id: string;
  source_name: string;
  query: string;
  matches: RagQueryMatch[];
}

// ── inference-plane engines + capabilities (camelCase /admin/*) ──────────────
// One currently-resident managed engine (the /admin/engines `resident` list). Count + priority,
// not RAM/VRAM bytes (the plane arbitrates by count). `resident` stays typed `unknown` on
// EnginesView so existing defensive readers keep compiling; `residentEngines()` narrows it.
export interface ResidentEngine {
  logicalId: string;
  engine: string;
  model: string;
  baseUrl: string;
  inflight: number;
  draining: boolean;
  priority: number;
}

export interface EnginesView {
  maxResident: number;
  resident: unknown;
}

// Narrow the loosely-typed `resident` payload to the engine rows, tolerating an unexpected shape.
export function residentEngines(engines: EnginesView | undefined | null): ResidentEngine[] {
  const r = engines?.resident;
  return Array.isArray(r) ? (r as ResidentEngine[]) : [];
}

// A plane's liveness snapshot for the dashboard status widgets. Unlike request(), the probe does
// NOT throw on a 503 not-ready: the not-ready body (status + reason) IS the signal we render, and a
// network failure (the plane is unreachable) becomes `reachable: false` — the offline state.
export interface PlaneHealth {
  /** false = the plane could not be reached at all (network error / CORS / down). */
  reachable: boolean;
  /** "ready" | "not-ready" | "ok" | "offline" — the plane's own word, or our fallback. */
  status: string;
  reason?: string | null;
  /** Control-plane /readyz echoes registered MCP servers; inference /readyz its per-engine readiness. */
  mcp?: { servers?: Record<string, unknown> } | null;
  engines?: Record<string, { ready: boolean; reason?: string | null }> | null;
}

async function probeReady(base: string): Promise<PlaneHealth> {
  try {
    // /readyz is unauthenticated on both planes; the header is harmless and keeps one code path.
    const res = await fetch(`${base}/readyz`, { headers: authHeaders() });
    let body: Record<string, unknown> = {};
    try {
      body = (await res.json()) as Record<string, unknown>;
    } catch {
      // a non-JSON body (proxy error page, etc.) — fall back to the HTTP status below.
    }
    return {
      reachable: true,
      status: typeof body.status === "string" ? body.status : res.ok ? "ready" : "not-ready",
      reason: typeof body.reason === "string" ? body.reason : null,
      mcp: (body.mcp as PlaneHealth["mcp"]) ?? null,
      engines: (body.engines as PlaneHealth["engines"]) ?? null,
    };
  } catch {
    return { reachable: false, status: "offline", reason: null };
  }
}

export interface Capabilities {
  maxContext?: number | null;
  toolCalling?: boolean;
  structuredOutput?: boolean;
  vision?: boolean;
  reasoning?: boolean;
  // The modality descriptor the bench routes tester panels on. Frozen vocabulary:
  // chat | vision | embeddings | audio.transcription | audio.speech.
  modalities?: string[];
  approximate?: boolean;
}

// A user-side named secret (for reachable bindings' `secret://NAME` credentialRefs). The value is
// WRITE-ONLY — the wire only ever returns the name + `hasValue`, never the secret itself.
export interface Credential {
  name: string;
  hasValue: boolean;
}

// ── catalog (discovery + install) wire shapes — NOT IR, just /admin/* shapes ──
// These mirror the inference plane's normalized CatalogProvider types. They are catalog metadata,
// never the agent IR (the IR stays a typed @theygent/ir-types IRDocument), so the rule against
// hand-written IR types is untouched.
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
  // Browse-time capability hints — derived from HF metadata WITHOUT a download (chat template,
  // architectures, GGUF header). Best-effort STATIC hints; the authoritative source stays the
  // post-install probe (getModelCapabilities). Field names mirror `Capabilities` so both badge
  // the same way. Present on model entries; a non-HF provider leaves them at their defaults.
  reasoning?: boolean;
  toolCalling?: boolean;
  vision?: boolean;
  maxContext?: number | null;
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

// ── bench store wire shapes (snake_case — control-plane convention) ──────────
// Metrics + digests only; raw payloads never cross into these.
export type BenchTargetKind = "model" | "agent";

export interface BenchRunInput {
  target_kind: BenchTargetKind;
  modality: string;
  logical_id?: string;
  model_ref?: string;
  binding?: string;
  params?: Record<string, unknown>;
  agent_id?: string;
  version?: string;
  content_hash?: string;
  metrics: Record<string, number>;
  output?: string; // used to compute output_digest; only persisted when capture=true
  capture?: boolean;
  suite_id?: string;
  case_id?: string;
  assertion?: string;
  assertion_passed?: boolean;
  run_id?: string;
  label?: string;
}

export interface BenchRunRecord extends BenchRunInput {
  id: string;
  params_digest?: string | null;
  output_digest?: string | null;
  capture_ref?: string | null;
  created_at: string;
}

export interface BenchCompare {
  a: BenchRunRecord;
  b: BenchRunRecord;
  metric_deltas: Record<string, number>;
  outputs_match: boolean;
}

export interface BenchCaseInput {
  input?: unknown;
  expected?: unknown;
  assertion?: string;
  assertion_config?: Record<string, unknown>;
}

export interface BenchSuiteInput {
  name: string;
  target_kind: BenchTargetKind;
  modality?: string;
  logical_id?: string;
  binding?: string;
  agent_id?: string;
  version?: string;
  content_hash?: string;
  cases: BenchCaseInput[];
}

export interface BenchCaseRecord extends BenchCaseInput {
  id: string;
  seq: number;
  created_at: string;
}

export interface BenchSuiteRecord extends Omit<BenchSuiteInput, "cases"> {
  id: string;
  cases: BenchCaseRecord[];
  created_at: string;
  updated_at: string;
}

export interface BenchPresetInput {
  name: string;
  modality: string;
  logical_id?: string;
  params: Record<string, unknown>;
}

export interface BenchPresetRecord extends BenchPresetInput {
  id: string;
  created_at: string;
  updated_at: string;
}

export const api = {
  // ── control-plane runs + sessions (the operator surface, ported from the cockpit) ──
  listRuns: (params: { limit?: number; before?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.before) q.set("before", params.before);
    const qs = q.toString();
    return request<{ runs: Run[] }>(CONTROL_PLANE_URL, `/runs${qs ? `?${qs}` : ""}`).then(
      (r) => r.runs,
    );
  },

  getRun: (id: string) => request<Run>(CONTROL_PLANE_URL, `/runs/${encodeURIComponent(id)}`),

  // Exact overview totals for the dashboard tiles — one COUNT(*) per table, distinct from the
  // paginated lists (which only expose a page window, so a client can't derive a true total).
  getStats: () =>
    request<{ runs: number; sessions: number; agents: number }>(CONTROL_PLANE_URL, "/stats"),

  // ── artifacts (audio in/out for an agent run) ────────────────────────────────
  // An audio agent is orchestrated by the control-plane walker, so its input/output audio crosses
  // the control plane as an artifact REFERENCE. These move the bytes: upload a recorded clip to get
  // a ref to pass as run input; download the ref the speak node produced to play it. Raw body, not
  // JSON — request() only does JSON, and an <audio src> can't carry the auth header, so we fetch
  // the bytes and object-URL them.
  uploadArtifact: async (
    blob: Blob,
  ): Promise<{ ref: string; contentType: string; bytes: number }> => {
    const res = await fetch(`${CONTROL_PLANE_URL}/artifacts`, {
      method: "POST",
      headers: { "Content-Type": blob.type || "application/octet-stream", ...authHeaders() },
      body: blob,
    });
    if (!res.ok) throw await toError(res);
    return res.json();
  },
  downloadArtifact: async (ref: string): Promise<Blob> => {
    const res = await fetch(`${CONTROL_PLANE_URL}/artifacts/${encodeURIComponent(ref)}`, {
      headers: authHeaders(),
    });
    if (!res.ok) throw await toError(res);
    return res.blob();
  },

  listSessions: (params: { limit?: number; before?: string } = {}) => {
    const q = new URLSearchParams();
    if (params.limit) q.set("limit", String(params.limit));
    if (params.before) q.set("before", params.before);
    const qs = q.toString();
    return request<{ sessions: SessionSummary[] }>(
      CONTROL_PLANE_URL,
      `/sessions${qs ? `?${qs}` : ""}`,
    ).then((r) => r.sessions);
  },

  getSession: (id: string) =>
    request<SessionDetail>(CONTROL_PLANE_URL, `/sessions/${encodeURIComponent(id)}`),

  // Create — or idempotently ensure — a session. An existing id returns the row (replacing
  // `metadata` wholesale iff provided); a missing id has the server generate one.
  createSession: (body: { id?: string; metadata?: Record<string, unknown> }) =>
    request<SessionSummary>(CONTROL_PLANE_URL, "/sessions", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Append a user + assistant turn pair to an EXISTING session (404 `session_not_found` otherwise —
  // no implicit create). Client-written turns carry no run, so their `run_id` comes back null.
  appendSessionTurns: (
    sessionId: string,
    body: { user_content: string; assistant_content: string },
  ) =>
    request<{ messages: SessionMessage[] }>(
      CONTROL_PLANE_URL,
      `/sessions/${encodeURIComponent(sessionId)}/turns`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  // Delete a session and its messages; runs that pointed at it stay, detached (session_id → null).
  deleteSession: (sessionId: string) =>
    request<void>(CONTROL_PLANE_URL, `/sessions/${encodeURIComponent(sessionId)}`, {
      method: "DELETE",
    }),

  // ── control-plane observability (the run waterfall) ──────────────────────────
  // The waterfall payload: the run's span tree (timing + status + worker attribution + edge sizes).
  // NO payloads — those are the lazy /io call below. Every waterfall surface (bench, run detail,
  // chat trace) reads this one fetcher; if observability is absent the caller degrades to a note.
  getTrace: (runId: string) =>
    request<{ runId: string; status: string; spans: Span[] }>(
      CONTROL_PLANE_URL,
      `/runs/${encodeURIComponent(runId)}/trace`,
    ).then((r) => r.spans),

  // The click-through: the full per-node I/O. Gated states (off/metadata/not-permitted) come back
  // as a 200 with inputs/outputs null + a `reason` — never an error, so the timeline stays legible.
  getNodeIo: (runId: string, nodeId: string) =>
    request<NodeIo>(
      CONTROL_PLANE_URL,
      `/runs/${encodeURIComponent(runId)}/nodes/${encodeURIComponent(nodeId)}/io`,
    ),

  getIoPolicy: (agentId: string) =>
    request<IoPolicy>(CONTROL_PLANE_URL, `/agents/${encodeURIComponent(agentId)}/io-policy`),

  putIoPolicy: (
    agentId: string,
    body: { io_capture: CaptureLevel; io_retention_seconds?: number | null },
  ) =>
    request<IoPolicy>(CONTROL_PLANE_URL, `/agents/${encodeURIComponent(agentId)}/io-policy`, {
      method: "PUT",
      body: JSON.stringify(body),
    }),

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

  // Create a new agent + its first version. The id/version come FROM the IR. The body is `{ ir }`
  // where `ir` carries its `view` block; the server strips `view`, hashes, persists.
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

  // Agent bench: invoke a saved, PINNED agent via the interactive, non-durable run path.
  // Non-stream by default → returns the terminal result; correlate via GET /runs/{id}.
  runAgent: (
    id: string,
    body: { input?: unknown; version?: string; content_hash?: string; stream?: boolean },
  ) =>
    request<{ runId: string; status: string; output?: string; error?: string }>(
      CONTROL_PLANE_URL,
      `/agents/${encodeURIComponent(id)}/runs`,
      { method: "POST", body: JSON.stringify({ stream: false, ...body }) },
    ),

  // Run a saved agent on the DURABLE runtime — the path for durable-only agents (loop/map/subgraph/
  // human) the interactive run rejects. Returns a run id to poll via GET /runs/{id}; a non-durable
  // server answers 400 `durable_required`. Snake_case `run_id` matches the /runs surface it polls.
  runAgentDurable: (
    id: string,
    body: { input?: unknown; version?: string; content_hash?: string },
  ) =>
    request<{ run_id: string }>(
      CONTROL_PLANE_URL,
      `/agents/${encodeURIComponent(id)}/durable-runs`,
      { method: "POST", body: JSON.stringify(body) },
    ),

  // Deliver the awaited input to a durable run paused at a human node (status "waiting"). The
  // workflow resumes from its checkpoint; poll GET /runs/{id} for the outcome. 409 when the run
  // isn't waiting; 422 when the node's declared input schema requires keys the payload lacks.
  resumeRun: (runId: string, input: unknown) =>
    request<{ runId: string; status: string; awaitingNode?: string | null }>(
      CONTROL_PLANE_URL,
      `/runs/${encodeURIComponent(runId)}/resume`,
      { method: "POST", body: JSON.stringify({ input }) },
    ),

  // Tool/MCP tester: run an INLINE one-node graph (input → mcp_tool → output) through the
  // existing /graphs/runs path — which already accepts inline IR. This adds NO new backend and NO
  // new execution path: it is the standard agent/run path pointed at a single tool, NOT a
  // dedicated /admin/mcp/.../invoke endpoint. Non-stream by default → terminal result; the tool's
  // structured output comes back JSON-serialized in `output` (app.py `_coerce_output`). Mirrors `runAgent`.
  runGraph: (body: { ir: IRDocument; input?: unknown; stream?: boolean }) =>
    request<{ runId: string; status: string; output?: string; error?: string }>(
      CONTROL_PLANE_URL,
      "/graphs/runs",
      { method: "POST", body: JSON.stringify({ stream: false, ...body }) },
    ),

  // Inference plane (separate base URL): the registered logical models, to populate the model
  // picker. Read-only; tolerated to fail (the picker falls back to free text if unreachable).
  listModels: () =>
    request<{ models: ModelView[] }>(INFERENCE_URL, "/admin/models").then((r) => r.models),

  // ── control plane: connections (the tool/MCP auth seam) ─────────────────────
  // The secret is write-only (create/patch); the wire never returns it (only `hasSecret`).
  listConnections: () =>
    request<{ connections: ConnectionRecord[] }>(CONTROL_PLANE_URL, "/connections").then(
      (r) => r.connections,
    ),

  createConnection: (body: CreateConnectionBody) =>
    request<ConnectionRecord>(CONTROL_PLANE_URL, "/connections", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  patchConnection: (
    id: string,
    body: { name?: string; config?: Record<string, unknown>; secret?: string; enabled?: boolean },
  ) =>
    request<ConnectionRecord>(CONTROL_PLANE_URL, `/connections/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deleteConnection: (id: string) =>
    request<void>(CONTROL_PLANE_URL, `/connections/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  // ── control plane: triggers (surfaced on the input node) ─────────────────────
  listTriggers: () =>
    request<{ triggers: TriggerRecord[] }>(CONTROL_PLANE_URL, "/triggers").then((r) => r.triggers),

  // ── control plane: retrieval sources (RAG) ──────────────────────────────────
  listRagSources: () =>
    request<{ sources: RagSource[] }>(CONTROL_PLANE_URL, "/rag/sources").then((r) => r.sources),

  getRagSource: (id: string) =>
    request<RagSource>(CONTROL_PLANE_URL, `/rag/sources/${encodeURIComponent(id)}`),

  createRagSource: (body: {
    name: string;
    kind: RagSourceKind;
    embedding_model: string;
    config?: Record<string, unknown>;
  }) =>
    request<RagSource>(CONTROL_PLANE_URL, "/rag/sources", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  updateRagSource: (id: string, body: { name?: string; config?: Record<string, unknown> }) =>
    request<RagSource>(CONTROL_PLANE_URL, `/rag/sources/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),

  deleteRagSource: (id: string) =>
    request<void>(CONTROL_PLANE_URL, `/rag/sources/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),

  listRagDocuments: (id: string) =>
    request<{ documents: RagDocument[] }>(
      CONTROL_PLANE_URL,
      `/rag/sources/${encodeURIComponent(id)}/documents`,
    ).then((r) => r.documents),

  // Start (or re-run) a crawl. Returns the source already flipped to "ingesting" — hand it to
  // `trackIngest` so the global center polls it to completion.
  ingestRagSource: (id: string) =>
    request<RagSource>(CONTROL_PLANE_URL, `/rag/sources/${encodeURIComponent(id)}:ingest`, {
      method: "POST",
    }),

  cancelRagIngest: (id: string) =>
    request<RagSource>(CONTROL_PLANE_URL, `/rag/sources/${encodeURIComponent(id)}:cancel`, {
      method: "POST",
    }),

  // Upload one document into an upload source. Raw body like uploadArtifact — the Content-Type is
  // the file's mime, not multipart; the filename rides the query string.
  uploadRagDocument: async (id: string, file: File): Promise<RagSource> => {
    const res = await fetch(
      `${CONTROL_PLANE_URL}/rag/sources/${encodeURIComponent(id)}/documents?filename=${encodeURIComponent(file.name)}`,
      {
        method: "POST",
        headers: { "Content-Type": file.type || "application/octet-stream", ...authHeaders() },
        body: file,
      },
    );
    if (!res.ok) throw await toError(res);
    return res.json();
  },

  // The query tester: retrieve top matches from one source (the same call a rag node makes).
  queryRagSource: (id: string, body: { query: string; top_k?: number; min_similarity?: number }) =>
    request<RagQueryResult>(CONTROL_PLANE_URL, `/rag/sources/${encodeURIComponent(id)}/query`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Control-plane registered MCP servers — to populate the mcp_tool `server` picker + the MCP page.
  listMcpServers: () =>
    request<{ servers: McpServerSummary[] }>(CONTROL_PLANE_URL, "/admin/mcp/servers").then(
      (r) => r.servers,
    ),

  // ── control plane: MCP server registry (the MCP page — define/manage servers) ─
  // Register or update a stdio MCP server. `env` stays in the user's trust domain.
  putMcpServer: (name: string, cfg: McpServerConfig) =>
    request<McpServerSummary>(CONTROL_PLANE_URL, `/admin/mcp/servers/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify(cfg),
    }),

  deleteMcpServer: (name: string) =>
    request<void>(CONTROL_PLANE_URL, `/admin/mcp/servers/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

  // Capability probe — connects lazily, returns the cached tool list (a 503 if the server won't spawn).
  getMcpTools: (name: string) =>
    request<{ tools: McpToolDescriptor[] }>(
      CONTROL_PLANE_URL,
      `/admin/mcp/servers/${encodeURIComponent(name)}/tools`,
    ).then((r) => r.tools),

  warmMcpServer: (name: string) =>
    request<McpServerSummary>(
      CONTROL_PLANE_URL,
      `/admin/mcp/servers/${encodeURIComponent(name)}:warm`,
      { method: "POST" },
    ),

  closeMcpServer: (name: string) =>
    request<McpServerSummary>(
      CONTROL_PLANE_URL,
      `/admin/mcp/servers/${encodeURIComponent(name)}:close`,
      { method: "POST" },
    ),

  // ── control plane: connection-backed MCP servers (the converged registration shape) ─
  // An `mcp_server` connection gets the same operational surface a name-keyed registration has:
  // capability probe, warm, close — plus the interactive authorization flow for remote servers.
  getConnectionMcpTools: (id: string) =>
    request<{ tools: McpToolDescriptor[] }>(
      CONTROL_PLANE_URL,
      `/connections/${encodeURIComponent(id)}/mcp/tools`,
    ).then((r) => r.tools),

  warmConnectionMcp: (id: string) =>
    request<{ id: string; connected: boolean }>(
      CONTROL_PLANE_URL,
      `/connections/${encodeURIComponent(id)}/mcp:warm`,
      { method: "POST" },
    ),

  closeConnectionMcp: (id: string) =>
    request<{ id: string; connected: boolean }>(
      CONTROL_PLANE_URL,
      `/connections/${encodeURIComponent(id)}/mcp:close`,
      { method: "POST" },
    ),

  // Kick off (or re-check) the user-authorized token flow for an `auth.type === "oauth"`
  // connection. `pending` means "open authorizationUrl in the browser and poll the status".
  startConnectionMcpOauth: (id: string) =>
    request<McpOauthStart>(
      CONTROL_PLANE_URL,
      `/connections/${encodeURIComponent(id)}/mcp-oauth:start`,
      { method: "POST" },
    ),

  getConnectionMcpOauth: (id: string) =>
    request<McpOauthStatus>(CONTROL_PLANE_URL, `/connections/${encodeURIComponent(id)}/mcp-oauth`),

  // ── control plane: MCP hub catalog (browse registries, install as connections) ─
  listMcpRegistries: () =>
    request<{ registries: McpRegistryInfo[] }>(CONTROL_PLANE_URL, "/admin/mcp/registries").then(
      (r) => r.registries,
    ),

  searchMcpCatalog: (
    params: { registry?: string; search?: string; limit?: number; cursor?: string } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.registry) q.set("registry", params.registry);
    if (params.search) q.set("search", params.search);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.cursor) q.set("cursor", params.cursor);
    const qs = q.toString();
    return request<McpCatalogPage>(CONTROL_PLANE_URL, `/admin/mcp/catalog${qs ? `?${qs}` : ""}`);
  },

  getMcpCatalogEntry: (registry: string, name: string, version = "latest") => {
    const q = new URLSearchParams({ registry, name, version });
    return request<McpCatalogDetail>(CONTROL_PLANE_URL, `/admin/mcp/catalog/entry?${q}`);
  },

  // Install one hub entry as an `mcp_server` connection. Secret-flagged values are routed to the
  // encrypted store SERVER-SIDE — the UI just sends the filled form, once.
  installMcpCatalogEntry: (body: {
    registry: string;
    name: string;
    version: string;
    candidateId: string;
    connectionName: string;
    values: Record<string, string>;
    useOauth?: boolean;
  }) =>
    request<ConnectionRecord>(CONTROL_PLANE_URL, "/admin/mcp/catalog/install", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Preview the tools a generated (openapi/graphql) server would expose — nothing is created.
  previewGenerated: (body: {
    kind: "openapi" | "graphql";
    spec?: Record<string, unknown>;
    specUrl?: string;
    url?: string;
    allowMutations?: boolean;
    connection?: string;
  }) =>
    request<GeneratedPreview>(CONTROL_PLANE_URL, "/admin/mcp/generated:preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  // Fetch an arbitrary, user-supplied JSON document from the BROWSER (no auth headers — this is
  // not a plane call). Used to inline an OpenAPI spec into a connection config when the user gave
  // only its URL; a CORS-blocked or non-JSON document throws so the caller can ask for a paste.
  fetchJsonDocument: async (url: string): Promise<Record<string, unknown>> => {
    const res = await fetch(url);
    if (!res.ok) throw new ApiError(`${res.status} ${res.statusText}`, res.status, "http_error");
    const doc = (await res.json()) as unknown;
    if (doc === null || typeof doc !== "object" || Array.isArray(doc)) {
      throw new ApiError("document did not parse to a JSON object", 422, "invalid_spec");
    }
    return doc as Record<string, unknown>;
  },

  // ── inference plane: model + engine registry (the "Installed" tab) ─────────
  // Register / lifecycle / capabilities all on the user-controlled inference plane, called
  // directly (no new routes added here).
  getEngines: () => request<EnginesView>(INFERENCE_URL, "/admin/engines"),

  // ── plane liveness (the dashboard status widgets) ───────────────────────────
  // Each plane is probed on its OWN base URL (the two-plane split — the interface reaches both
  // directly). A 503 not-ready is a status, not an error; only an unreachable plane is "offline".
  controlHealth: () => probeReady(CONTROL_PLANE_URL),
  inferenceHealth: () => probeReady(INFERENCE_URL),

  getModelCapabilities: (logicalId: string) =>
    request<Capabilities>(
      INFERENCE_URL,
      `/admin/models/${encodeURIComponent(logicalId)}/capabilities`,
    ),

  // ── inference plane: local credentials (user-side named secrets) ───────────
  // Manage `secret://NAME` refs for reachable bindings. Values are write-only (never returned);
  // the list is names + hasValue only. All on the user-controlled inference plane, called directly.
  listCredentials: () =>
    request<{ credentials: Credential[] }>(INFERENCE_URL, "/admin/credentials").then(
      (r) => r.credentials,
    ),

  putCredential: (name: string, value: string) =>
    request<Credential>(INFERENCE_URL, `/admin/credentials/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    }),

  deleteCredential: (name: string) =>
    request<void>(INFERENCE_URL, `/admin/credentials/${encodeURIComponent(name)}`, {
      method: "DELETE",
    }),

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

  // ── inference plane: catalog (the "Discover" tab) ───────────────────────────
  // Discovery + install live in the inference plane (the user's trust domain). Install downloads
  // weights HERE and registers them locally — theygent never sees the bytes (the sovereignty promise).
  searchCatalogModels: (
    params: {
      search?: string;
      sort?: string;
      limit?: number;
      engines?: string[]; // narrow to a subset of ready engines
      size?: string; // "small" | "medium" | "large" — param-size filter
      modality?: string; // task filter: chat | vision | embeddings | audio.transcription | audio.speech
    } = {},
  ) => {
    const q = new URLSearchParams();
    if (params.search) q.set("search", params.search);
    if (params.sort) q.set("sort", params.sort);
    if (params.limit) q.set("limit", String(params.limit));
    if (params.engines?.length) q.set("engines", params.engines.join(","));
    if (params.size) q.set("size", params.size);
    if (params.modality) q.set("modality", params.modality);
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

  // ── control plane: bench store (metrics + digests; snake_case wire) ─────────
  // Persists what the bench PRODUCED — never raw data-plane payloads (those go straight to the
  // inference plane). All under the control-plane base URL.
  recordBenchRun: (body: BenchRunInput) =>
    request<BenchRunRecord>(CONTROL_PLANE_URL, "/bench/runs", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listBenchRuns: (
    params: {
      limit?: number;
      logical_id?: string;
      agent_id?: string;
      suite_id?: string;
      case_id?: string;
    } = {},
  ) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v != null) q.set(k, String(v));
    const qs = q.toString();
    return request<{ runs: BenchRunRecord[] }>(
      CONTROL_PLANE_URL,
      `/bench/runs${qs ? `?${qs}` : ""}`,
    ).then((r) => r.runs);
  },

  compareBenchRuns: (a: string, b: string) =>
    request<BenchCompare>(
      CONTROL_PLANE_URL,
      `/bench/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`,
    ),

  createSuite: (body: BenchSuiteInput) =>
    request<BenchSuiteRecord>(CONTROL_PLANE_URL, "/bench/suites", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listSuites: () =>
    request<{ suites: BenchSuiteRecord[] }>(CONTROL_PLANE_URL, "/bench/suites").then(
      (r) => r.suites,
    ),

  getSuite: (id: string) =>
    request<BenchSuiteRecord>(CONTROL_PLANE_URL, `/bench/suites/${encodeURIComponent(id)}`),

  createPreset: (body: BenchPresetInput) =>
    request<BenchPresetRecord>(CONTROL_PLANE_URL, "/bench/presets", {
      method: "POST",
      body: JSON.stringify(body),
    }),

  listPresets: (params: { modality?: string; logical_id?: string } = {}) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) if (v != null) q.set(k, String(v));
    const qs = q.toString();
    return request<{ presets: BenchPresetRecord[] }>(
      CONTROL_PLANE_URL,
      `/bench/presets${qs ? `?${qs}` : ""}`,
    ).then((r) => r.presets);
  },

  deletePreset: (id: string) =>
    request<void>(CONTROL_PLANE_URL, `/bench/presets/${encodeURIComponent(id)}`, {
      method: "DELETE",
    }),
};

// ── streaming (the run composer + live trace; ported from the cockpit) ───────
// POST + read the SSE body on the SAME request: the composer creates and streams from one POST,
// and we need custom headers + programmatic abort — none of which `EventSource` supports. One SSE
// frame parser (lib/sse.ts) is reused across every streaming surface.

export interface StreamHandle {
  /** Async iterator of parsed SSE events. */
  events: AsyncGenerator<SSEEvent>;
  /** Abort the in-flight request/stream. */
  abort: () => void;
}

/**
 * POST a run (or graph run) and stream its SSE body. Returns immediately with an event iterator so
 * the caller drives the UI. A pre-stream error (non-2xx) is thrown before the first event, exactly
 * as the backend surfaces 503/404/400 before the 200 SSE body.
 */
export async function streamRun(
  // /runs, /graphs/runs, or /agents/{id}/runs (invoke-by-reference) — all share the SSE shape.
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
 * GET an SSE stream (the live trace waterfall `/runs/{id}/trace/stream`). Same frame parser as
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
