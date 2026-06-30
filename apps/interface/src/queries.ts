// TanStack Query hooks for the operator surface (Runs, Threads, Compose) ported from the cockpit.
// Server state lives here; mutations auto-invalidate the relevant list queries on success. Local UI
// state stays component useState. The query keys match the ad-hoc keys the canvas routes already use
// (["agents"], ["agent", id], ["models"]) so invalidation stays consistent across both surfaces.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { api } from "./lib/api";

export const keys = {
  runs: (limit?: number) => ["runs", limit ?? 50] as const,
  run: (id: string) => ["run", id] as const,
  // NB: keyed "run-trace", NOT "trace" — the bench's TraceWaterfall already caches ["trace", runId]
  // as the FULL {runId,status,spans} envelope, while this surface resolves api.getTrace to a bare
  // Span[]. Same backend route, same QueryClient → a shared ["trace", id] entry would hand one
  // surface the other's shape (an envelope into mergeSpans' for…of → crash). Distinct key isolates it.
  trace: (id: string) => ["run-trace", id] as const,
  nodeIo: (id: string, nodeId: string) => ["node-io", id, nodeId] as const,
  threads: () => ["threads"] as const,
  thread: (id: string) => ["thread", id] as const,
  agents: () => ["agents"] as const,
  agent: (id: string) => ["agent", id] as const,
  models: () => ["models"] as const,
};

// Runs list auto-refreshes ~3s while the tab is visible (M8 §1.1). Query pauses background
// refetch when the tab is hidden by default (refetchIntervalInBackground stays false).
export function useRuns(limit = 50) {
  return useQuery({
    queryKey: keys.runs(limit),
    queryFn: () => api.listRuns({ limit }),
    refetchInterval: 3000,
  });
}

export function useRun(id: string, opts: { live?: boolean } = {}) {
  return useQuery({
    queryKey: keys.run(id),
    queryFn: () => api.getRun(id),
    // While the run is live we poll faster so a terminal status lands promptly even if the
    // SSE stream was attached elsewhere; a terminal run stops polling.
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      if (opts.live && (s === "created" || s === "streaming")) return 1500;
      return false;
    },
  });
}

// M17: the run waterfall's persisted spans. Polls while the run is live so node bars appear as
// nodes complete (each span is written on close); the live in-flight bars are overlaid from the
// /trace/stream SSE in the Waterfall component. Stops polling when the run is terminal.
export function useTrace(id: string, opts: { live?: boolean } = {}) {
  return useQuery({
    queryKey: keys.trace(id),
    queryFn: () => api.getTrace(id),
    refetchInterval: opts.live ? 1000 : false,
  });
}

// The lazy click-through: only fetched when a node is selected (enabled gates it). The payload is
// gated server-side (off/metadata/not-permitted → inputs null + a reason), never an error.
export function useNodeIo(runId: string, nodeId: string | null) {
  return useQuery({
    queryKey: keys.nodeIo(runId, nodeId ?? ""),
    queryFn: () => api.getNodeIo(runId, nodeId as string),
    enabled: !!nodeId,
  });
}

export function useThreads() {
  return useQuery({ queryKey: keys.threads(), queryFn: () => api.listThreads({ limit: 50 }) });
}

export function useThread(id: string) {
  // Only fetch a real thread id. A run detail for a one-shot (un-threaded) run passes "",
  // and `GET /threads/` redirects to the LIST endpoint (no `.messages`) — fetching it would
  // hand the run detail a wrong-shaped object. `enabled` keeps that request from happening.
  return useQuery({ queryKey: keys.thread(id), queryFn: () => api.getThread(id), enabled: !!id });
}

export function useModels() {
  return useQuery({ queryKey: keys.models(), queryFn: () => api.listModels() });
}

// M11 "Save as agent": a new agent id → create; an existing id → add a version. Each invalidates
// the agent list + that agent's detail so the canvas Home/Editor pick up the new version.
export function useAgentMutations() {
  const qc = useQueryClient();
  return {
    create: useMutation({
      mutationFn: (body: { ir: unknown; name?: string }) =>
        api.createAgent({ ir: body.ir as IRDocument, name: body.name }),
      onSuccess: (detail) => {
        qc.invalidateQueries({ queryKey: keys.agents() });
        qc.invalidateQueries({ queryKey: keys.agent(detail.id) });
      },
    }),
    addVersion: useMutation({
      mutationFn: ({ id, ir }: { id: string; ir: unknown }) =>
        api.addAgentVersion(id, { ir: ir as IRDocument }),
      onSuccess: (detail) => {
        qc.invalidateQueries({ queryKey: keys.agents() });
        qc.invalidateQueries({ queryKey: keys.agent(detail.id) });
      },
    }),
  };
}
