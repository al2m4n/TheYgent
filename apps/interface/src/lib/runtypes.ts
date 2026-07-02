// Run / thread / trace wire shapes for the operator surface (Runs, Threads, Compose) ported from
// the cockpit (apps/web). Consumed as-is (the backend contracts are frozen; the UI adapts).
//
// Two conventions coexist by surface and we honor both rather than normalize:
//  - /runs + /threads payloads are snake_case (thread_id, created_at) — the run surface.
//  - SSE frames are camelCase (runId) — see the SSE frame types at the bottom + lib/sse.ts.
//
// The agent-registry + inference-plane (camelCase /admin/*) shapes the interface already needs for
// the canvas live in lib/api.ts; this file adds only the run/thread/trace shapes the cockpit views
// brought with them.

// `waiting` is a durable run paused at a human node (awaiting resume); the reconcile sweep leaves
// it alone. A run is live while created/streaming and terminal at completed/failed.
export type RunStatus = "created" | "streaming" | "waiting" | "completed" | "failed";

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
  // The run's final output, persisted regardless of threading (so a one-shot run's answer survives
  // the stream/tab). Null until a terminal output is reached.
  output: string | null;
  // Durable-run breadcrumbs the backend returns: the node a `waiting` run is paused at, the terminal
  // timestamp, and the trigger that fired it (null for a UI-initiated run).
  awaiting_node?: string | null;
  completed_at?: string | null;
  trigger_id?: string | null;
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

// ── control-plane observability /runs/{id}/trace + /io + /io-policy (snake_case) ──
// The run waterfall reads theygent's OWN span store (never an external trace backend). A span is a
// run-root, a node, or a phase (queue.wait / model.generate). node_id == the React Flow node id (so
// the canvas overlay is a free join later). executor_id/worker_host = which worker handled the span.

export interface Span {
  id: string;
  run_id: string;
  trace_id: string;
  span_id: string;
  parent_span_id: string | null;
  node_id: string | null;
  node_type: string | null;
  kind: string | null; // activity | orchestration | boundary
  name: string; // node id for node spans; phase name otherwise (queue.wait, model.generate, …)
  phase: string | null;
  branch_index: number | null;
  status: string; // ok | err | skipped | running
  start_ns: number;
  end_ns: number | null; // null while in-flight (live)
  attributes: Record<string, unknown> | null; // GenAI scalars (model, ttft_ms, …) — no payloads
  error: string | null;
  executor_id: string | null; // the worker that handled it (inproc | local | a DBOS executor id)
  worker_host: string | null; // host:pid
  seq: number;
  bytes_in: number | null; // edge-size annotations (joined from node_io) — no blob load
  bytes_out: number | null;
}

export type CaptureLevel = "off" | "metadata" | "full";

// GET /runs/{id}/nodes/{nodeId}/io — the lazy click-through. inputs/outputs are null when capture is
// off/metadata or the viewer lacks io:read; `reason` says which (a gated state, never an error).
export interface NodeIo {
  run_id: string;
  node_id: string;
  capture_level: CaptureLevel;
  inputs: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  bytes_in: number;
  bytes_out: number;
  truncated: boolean;
  reason: string | null;
}

// GET/PUT /agents/{id}/io-policy — the per-agent capture policy. `effective` is what actually
// happens (ceiling ∧ topology ∧ stored); `capped` flags when the deployment/topology limits it.
export interface IoPolicy {
  agent_id: string;
  io_capture: CaptureLevel; // the stored request (or the topology default if none)
  effective: CaptureLevel;
  capped: boolean;
  ceiling: CaptureLevel;
  topology_default: CaptureLevel;
  io_retention_seconds: number | null;
  redact_rules: Record<string, unknown> | null;
  updated_at: string | null;
  has_explicit_policy: boolean;
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

// A reasoning model streams its hidden "thinking" before the answer (event: reasoning). Shown as
// progress so a thinking model doesn't look frozen; never the answer.
export interface ReasoningFrame {
  runId: string;
  reasoning: string;
}
