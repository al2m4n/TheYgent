// TanStack Query hooks for the operator surface (Runs, Chats/Sessions) ported from the cockpit.
// Server state lives here; mutations auto-invalidate the relevant list queries on success. Local UI
// state stays component useState. The query keys match the ad-hoc keys the canvas routes already use
// (["agents"], ["agent", id], ["models"]) so invalidation stays consistent across both surfaces.

import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { api } from "./lib/api";

// Page size for the scroll-paginated lists (runs/sessions/agents). The control-plane list endpoints are
// keyset-paginated: pass the LAST row's `id` as `before` to get the strictly-older next page, and a
// short page (fewer than PAGE rows) means the end. Endpoints cap `limit` at 200; 50 keeps each scroll
// fetch snappy.
const PAGE = 50;

// The keyset cursor is the last loaded row's `id`; `undefined` (a short page) stops pagination.
function nextByIdCursor<T extends { id: string }>(lastPage: T[]): string | undefined {
  return lastPage.length === PAGE ? lastPage[lastPage.length - 1].id : undefined;
}

// Flatten an infinite query's pages into one list, de-duplicated by `id` — so a live refresh that
// shifts the newest window (e.g. a new run arriving on the 3s poll) can't momentarily double-render a
// row across two page boundaries.
export function flattenPages<T extends { id: string }>(data: { pages: T[][] } | undefined): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const page of data?.pages ?? []) {
    for (const item of page) {
      if (!seen.has(item.id)) {
        seen.add(item.id);
        out.push(item);
      }
    }
  }
  return out;
}

export const keys = {
  run: (id: string) => ["run", id] as const,
  // NB: keyed "run-trace", NOT "trace" — the bench's TraceWaterfall already caches ["trace", runId]
  // as the FULL {runId,status,spans} envelope, while this surface resolves api.getTrace to a bare
  // Span[]. Same backend route, same QueryClient → a shared ["trace", id] entry would hand one
  // surface the other's shape (an envelope into mergeSpans' for…of → crash). Distinct key isolates it.
  trace: (id: string) => ["run-trace", id] as const,
  nodeIo: (id: string, nodeId: string) => ["node-io", id, nodeId] as const,
  sessions: () => ["sessions"] as const,
  session: (id: string) => ["session", id] as const,
  agents: () => ["agents"] as const,
  agent: (id: string) => ["agent", id] as const,
  models: () => ["models"] as const,
};

// The Runs list: scroll-paginated (newest first) AND auto-refreshing ~3s while the tab is visible.
// The 3s poll refetches the loaded pages so live status changes land; keyed under ["runs", …] so any
// broad ["runs"] invalidation still reaches it. Refetch pauses while the tab is hidden by default.
export function useRunsInfinite() {
  return useInfiniteQuery({
    queryKey: ["runs", "infinite"],
    queryFn: ({ pageParam }) => api.listRuns({ limit: PAGE, before: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextByIdCursor,
    refetchInterval: 3000,
  });
}

// The Sessions list: scroll-paginated (newest activity first). Keyed under ["sessions", …] so a broad
// ["sessions"] invalidation reaches it.
export function useSessionsInfinite() {
  return useInfiniteQuery({
    queryKey: ["sessions", "infinite"],
    queryFn: ({ pageParam }) => api.listSessions({ limit: PAGE, before: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextByIdCursor,
  });
}

// The Agents grid: scroll-paginated (newest first). Keyed ["agents", "infinite"] — a DISTINCT key from
// the ["agents"] array the node inspector's agent picker uses (different shape), yet a broad
// ["agents"] invalidation (a save adds/updates an agent) still matches it by prefix and refreshes it.
export function useAgentsInfinite() {
  return useInfiniteQuery({
    queryKey: ["agents", "infinite"],
    queryFn: ({ pageParam }) => api.listAgents({ limit: PAGE, before: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: nextByIdCursor,
  });
}

export function useRun(id: string, opts: { live?: boolean; enabled?: boolean } = {}) {
  return useQuery({
    queryKey: keys.run(id),
    queryFn: () => api.getRun(id),
    enabled: opts.enabled ?? true,
    // A live run (notably a durable run kicked off from the bench) should keep updating even if the
    // user switches tabs mid-run — poll in the background too, not only while the tab is focused.
    refetchIntervalInBackground: opts.live ?? false,
    // While the run is live we poll faster so a terminal status lands promptly even if the
    // SSE stream was attached elsewhere; a terminal run stops polling. A `waiting` run (paused at
    // a human node) polls at a relaxed cadence so a resume — from this tab or anywhere else —
    // shows up without a manual reload.
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      if (opts.live && (s === "created" || s === "streaming")) return 1500;
      if (opts.live && s === "waiting") return 2500;
      // Still unknown (first fetch failed / not yet landed) on a live poll: keep trying (bounded,
      // so a genuinely-missing run doesn't poll forever) — a transient GET failure must not
      // strand the poller.
      if (opts.live && q.state.data === undefined && q.state.fetchFailureCount < 5) return 2500;
      return false;
    },
  });
}

// The run waterfall's persisted spans. Polls while the run is live so node bars appear as
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

export function useSession(id: string, opts: { fresh?: boolean } = {}) {
  // Only fetch a real session id. A run detail for a one-shot (session-less) run passes "",
  // and `GET /sessions/` redirects to the LIST endpoint (no `.messages`) — fetching it would
  // hand the run detail a wrong-shaped object. `enabled` keeps that request from happening.
  // `fresh` forces a refetch on every mount — the session chat seeds its transcript ONCE from
  // this data, so a stale cache at mount would show an outdated conversation.
  return useQuery({
    queryKey: keys.session(id),
    queryFn: () => api.getSession(id),
    enabled: !!id,
    refetchOnMount: opts.fresh ? "always" : undefined,
  });
}

export function useModels() {
  return useQuery({ queryKey: keys.models(), queryFn: () => api.listModels() });
}

// "Save as agent": a new agent id → create; an existing id → add a version. Each invalidates
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
