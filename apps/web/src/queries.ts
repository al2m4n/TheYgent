// TanStack Query hooks (M8 §3.4): server state lives here, mutations auto-invalidate the
// relevant list queries on success. No global store — local UI state is component useState.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./lib/api";

export const keys = {
  runs: (limit?: number) => ["runs", limit ?? 50] as const,
  run: (id: string) => ["run", id] as const,
  trace: (id: string) => ["trace", id] as const,
  nodeIo: (id: string, nodeId: string) => ["node-io", id, nodeId] as const,
  ioPolicy: (agentId: string) => ["io-policy", agentId] as const,
  threads: () => ["threads"] as const,
  thread: (id: string) => ["thread", id] as const,
  agents: () => ["agents"] as const,
  agent: (id: string) => ["agent", id] as const,
  models: () => ["models"] as const,
  engines: () => ["engines"] as const,
  mcpServers: () => ["mcp-servers"] as const,
  mcpServer: (name: string) => ["mcp-server", name] as const,
  mcpTools: (name: string) => ["mcp-tools", name] as const,
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

export function useIoPolicy(agentId: string) {
  return useQuery({
    queryKey: keys.ioPolicy(agentId),
    queryFn: () => api.getIoPolicy(agentId),
    enabled: !!agentId,
  });
}

export function useIoPolicyMutation(agentId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: { io_capture: "off" | "metadata" | "full" }) =>
      api.putIoPolicy(agentId, body),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.ioPolicy(agentId) }),
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

export function useAgents() {
  return useQuery({ queryKey: keys.agents(), queryFn: () => api.listAgents({ limit: 50 }) });
}

export function useAgent(id: string) {
  return useQuery({ queryKey: keys.agent(id), queryFn: () => api.getAgent(id), enabled: !!id });
}

export function useModels() {
  return useQuery({ queryKey: keys.models(), queryFn: () => api.listModels() });
}

export function useEngines() {
  return useQuery({ queryKey: keys.engines(), queryFn: () => api.getEngines() });
}

export function useMcpServers() {
  return useQuery({ queryKey: keys.mcpServers(), queryFn: () => api.listMcpServers() });
}

export function useMcpTools(name: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.mcpTools(name),
    queryFn: () => api.getMcpTools(name),
    enabled,
    retry: false,
  });
}

// ── mutations: each invalidates the registry list it changed ─────────────────

export function useAgentMutations() {
  const qc = useQueryClient();
  return {
    create: useMutation({
      mutationFn: (body: { ir: unknown; name?: string }) => api.createAgent(body),
      onSuccess: (detail) => {
        qc.invalidateQueries({ queryKey: keys.agents() });
        qc.invalidateQueries({ queryKey: keys.agent(detail.id) });
      },
    }),
    addVersion: useMutation({
      mutationFn: ({ id, ir }: { id: string; ir: unknown }) => api.addAgentVersion(id, { ir }),
      onSuccess: (detail) => {
        qc.invalidateQueries({ queryKey: keys.agents() });
        qc.invalidateQueries({ queryKey: keys.agent(detail.id) });
      },
    }),
  };
}

export function useModelMutations() {
  const qc = useQueryClient();
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: keys.models() });
    qc.invalidateQueries({ queryKey: keys.engines() });
  };
  return {
    register: useMutation({
      mutationFn: ({ logicalId, body }: { logicalId: string; body: unknown }) =>
        api.putModel(logicalId, body),
      onSuccess: invalidate,
    }),
    remove: useMutation({ mutationFn: (id: string) => api.deleteModel(id), onSuccess: invalidate }),
    warm: useMutation({ mutationFn: (id: string) => api.warmModel(id), onSuccess: invalidate }),
    evict: useMutation({ mutationFn: (id: string) => api.evictModel(id), onSuccess: invalidate }),
  };
}

export function useMcpMutations() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: keys.mcpServers() });
  return {
    register: useMutation({
      mutationFn: ({ name, body }: { name: string; body: unknown }) => api.putMcpServer(name, body),
      onSuccess: invalidate,
    }),
    remove: useMutation({
      mutationFn: (name: string) => api.deleteMcpServer(name),
      onSuccess: invalidate,
    }),
    warm: useMutation({
      mutationFn: (name: string) => api.warmMcpServer(name),
      onSuccess: invalidate,
    }),
    close: useMutation({
      mutationFn: (name: string) => api.closeMcpServer(name),
      onSuccess: invalidate,
    }),
  };
}
