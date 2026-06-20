// Wire shapes, consumed as-is (M8 §0: the backend contracts are frozen; the UI adapts).
//
// Two conventions coexist by surface and we honor both rather than normalize:
//  - /runs + /threads payloads are snake_case (thread_id, created_at) — the run surface.
//  - SSE frames and /admin/* payloads are camelCase (runId, logicalId) — see api.ts.

export type RunStatus = "created" | "streaming" | "completed" | "failed";

export interface Run {
  id: string;
  thread_id: string | null;
  status: RunStatus;
  model: string;
  graph_id: string | null;
  graph_version: string | null;
  content_hash: string | null;
  created_at: string;
  updated_at: string;
  error: string | null;
}

export interface ThreadSummary {
  id: string;
  created_at: string;
  last_activity: string;
  message_count: number;
  preview: string | null;
}

export interface ThreadMessage {
  id: string;
  run_id: string;
  role: string; // user | assistant
  content: string;
  position: number;
  created_at: string;
}

export interface ThreadDetail {
  id: string;
  created_at: string;
  updated_at: string;
  messages: ThreadMessage[];
}

// ── inference plane /admin/* (camelCase) ────────────────────────────────────

export interface Capabilities {
  maxContext?: number;
  approximate?: boolean;
  tools?: boolean;
  structuredOutput?: boolean;
  vision?: boolean;
  [k: string]: unknown;
}

export interface ModelView {
  logicalId: string;
  binding: {
    binding: string; // mlx | vllm | llamacpp | openai-compatible
    model?: string;
    source?: string;
    baseUrl?: string;
    [k: string]: unknown;
  };
  // The inference plane returns a structured engine state (manager.state) — NOT a string.
  // A reachable binding is never "resident" (it bypasses the manager); resident applies to
  // managed engines only.
  state: {
    resident: boolean;
    inflight: number;
    draining: boolean;
    baseUrl?: string;
  };
}

export interface EnginesView {
  maxResident: number;
  resident: Record<string, unknown> | unknown[];
}

// ── control-plane /admin/mcp/* (camelCase) ──────────────────────────────────

// GET /admin/mcp/servers returns a LIST of these summaries (mcp.states()) — not a dict.
export interface McpServerSummary {
  name: string;
  transport: string;
  connected: boolean;
}

// GET /admin/mcp/servers/{name} — the fuller per-server view (adds command/args/envKeys).
export interface McpServerView {
  name: string;
  transport: string;
  command: string | null;
  args: string[];
  envKeys: string[];
  connected: boolean;
}

export interface McpToolView {
  name: string;
  description: string | null;
  inputSchema: unknown;
}

// ── SSE event payloads (camelCase) ──────────────────────────────────────────

export interface RunFrame {
  runId: string;
  status?: RunStatus;
  model?: string;
  error?: string;
}

export interface DeltaFrame {
  runId: string;
  delta: string;
}
