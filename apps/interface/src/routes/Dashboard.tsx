// Dashboard — the home page. A single-glance operator overview: are the two planes reachable, what
// is the inference plane running right now, and quick lists of the latest runs, chats, agents, and
// models. Everything here is read-only over the existing endpoints and degrades gracefully — an
// unreachable plane becomes an "offline" widget, never a page error. Each panel links through to its
// full page.

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import {
  Activity,
  ArrowRight,
  Bot,
  Boxes,
  Cpu,
  type LucideIcon,
  MessagesSquare,
  Plus,
  Server,
  SquarePen,
} from "lucide-react";
import { type ReactNode, useMemo } from "react";
import { TimeAgo } from "../components/TimeAgo";
import { Badge, Card, Page, SectionHeading, buttonClass, linkClass } from "../components/ui";
import { Skeleton } from "../components/ui/skeleton";
import {
  CONTROL_PLANE_URL,
  INFERENCE_URL,
  type ModelView,
  type PlaneHealth,
  api,
  residentEngines,
} from "../lib/api";
import { engineTone, statusTone, toneOf } from "../lib/categories";
import { exactCount, formatCount, shortId } from "../lib/format";
import type { Run, SessionSummary } from "../lib/runtypes";
import {
  flattenPages,
  useAgentsInfinite,
  useControlHealth,
  useEngines,
  useInferenceHealth,
  useModels,
  useRunsInfinite,
  useSessionsInfinite,
  useStats,
} from "../queries";

// ── plane status derivation ──────────────────────────────────────────────────
// Four visible states, in order of severity: checking (first probe in flight), online (reachable +
// ready), degraded (reachable but a dependency is not-ready), offline (unreachable at all).
type PlaneState = "checking" | "online" | "degraded" | "offline";

const PLANE_TONE: Record<PlaneState, string> = {
  checking: "slate",
  online: "green",
  degraded: "amber",
  offline: "red",
};
const PLANE_LABEL: Record<PlaneState, string> = {
  checking: "Checking…",
  online: "Online",
  degraded: "Degraded",
  offline: "Offline",
};

function planeState(health: PlaneHealth | undefined, isLoading: boolean): PlaneState {
  if (!health) return isLoading ? "checking" : "offline";
  if (!health.reachable) return "offline";
  return health.status === "ready" || health.status === "ok" ? "online" : "degraded";
}

// A tile's headline count. Prefer the EXACT total from the /stats endpoint (abbreviated K/M, with the
// full number on hover); if that's unavailable, fall back to the loaded page window ("50+") which
// can't claim an exact total. `title` (the full number) is set only when we actually know it.
interface CountDisplay {
  text: string;
  title?: string;
}

function countTile(
  exact: number | undefined,
  len: number,
  hasMore: boolean | undefined,
): CountDisplay {
  if (exact != null) return { text: formatCount(exact), title: exactCount(exact) };
  return { text: hasMore ? `${len}+` : String(len) };
}

// An exact, always-known count (a full list's length) — abbreviated with the full value on hover.
function exactTile(n: number): CountDisplay {
  return { text: formatCount(n), title: exactCount(n) };
}

// The best human-readable name for a run in a compact row: the model, then the graph, then its id.
function runLabel(run: Run): string {
  return run.model || (run.graph_id ? shortId(run.graph_id, 12) : shortId(run.id, 12));
}

// The best human label for a session, mirroring the sidebar's recents.
function sessionLabel(s: SessionSummary): string {
  const meta = s.metadata ?? {};
  const title = typeof meta.title === "string" ? meta.title : "";
  const target =
    typeof meta.agent_name === "string"
      ? meta.agent_name
      : typeof meta.model === "string"
        ? meta.model
        : "";
  return title || s.preview?.trim() || target || shortId(s.id, 12);
}

// A small status/tone dot (reused by run rows and the plane status pill).
function Dot({ tone, className = "" }: { tone: string; className?: string }) {
  return (
    <span
      className={`inline-block size-2 shrink-0 rounded-full ${toneOf(tone).dot} ${className}`}
    />
  );
}

// The colored status pill on each plane card.
function StatusPill({ state }: { state: PlaneState }) {
  const tone = PLANE_TONE[state];
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-[11px] font-medium ${toneOf(tone).badge}`}
    >
      <Dot tone={tone} className={state === "checking" ? "animate-pulse" : ""} />
      {PLANE_LABEL[state]}
    </span>
  );
}

export function Dashboard() {
  const control = useControlHealth();
  const inference = useInferenceHealth();
  const engines = useEngines();
  const models = useModels();
  const runsQ = useRunsInfinite();
  const sessionsQ = useSessionsInfinite();
  const agentsQ = useAgentsInfinite();

  // Cheap single-call registries that enrich the control-plane card. Each is independent and
  // tolerated to fail (retry off) — a down control plane just leaves the counts at "—". MCP servers
  // come from TWO places: hand-defined name-keyed servers AND `mcp_server` connections (the hub
  // install path); the count merges both, matching the MCP page.
  const mcp = useQuery({
    queryKey: ["mcpServers"],
    queryFn: () => api.listMcpServers(),
    retry: false,
  });
  const connections = useQuery({
    queryKey: ["connections"],
    queryFn: () => api.listConnections(),
    retry: false,
  });
  const rag = useQuery({
    queryKey: ["ragSources"],
    queryFn: () => api.listRagSources(),
    retry: false,
  });

  // Total configured MCP servers. `connected` is a LAZY warm flag (an idle server reads as
  // disconnected until its first call), so a "connected/total up" fraction would misreport a healthy
  // idle system — the honest metric is how many are configured. `undefined` (both queries failed)
  // renders as "—".
  const mcpCount = useMemo(() => {
    if (mcp.data == null && connections.data == null) return undefined;
    const named = mcp.data?.length ?? 0;
    const asConnections = connections.data?.filter((c) => c.kind === "mcp_server").length ?? 0;
    return named + asConnections;
  }, [mcp.data, connections.data]);

  // Exact totals (runs/sessions/agents) — the tiles show these abbreviated with the full number on
  // hover, falling back to the loaded-window "50+" when the count endpoint is unavailable.
  const stats = useStats();

  const runs = useMemo(() => flattenPages(runsQ.data), [runsQ.data]);
  const sessions = useMemo(() => flattenPages(sessionsQ.data), [sessionsQ.data]);
  const agents = useMemo(() => flattenPages(agentsQ.data), [agentsQ.data]);
  const modelList = models.data ?? [];
  const resident = residentEngines(engines.data);

  const agentsTile = countTile(stats.data?.agents, agents.length, agentsQ.hasNextPage);
  const chatsTile = countTile(stats.data?.sessions, sessions.length, sessionsQ.hasNextPage);
  const runsTile = countTile(stats.data?.runs, runs.length, runsQ.hasNextPage);
  const modelsTile = exactTile(modelList.length);

  // The engine ids currently warm — so a model row can show a "warm" marker.
  const warmIds = useMemo(() => new Set(resident.map((e) => e.logicalId)), [resident]);

  // "Last used" is derived from run history: the most recent run time per model / per agent. This is
  // the honest usage signal (the registry only records when a thing was *modified*).
  const lastRunByModel = useMemo(() => lastRunMap(runs, (r) => r.model), [runs]);
  const lastRunByAgent = useMemo(() => lastRunMap(runs, (r) => r.graph_id), [runs]);

  return (
    <Page className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Dashboard</h1>
          <p className="text-xs text-muted-foreground">
            Your planes, agents, and recent activity at a glance.
          </p>
        </div>
        <div className="flex shrink-0 gap-2">
          <Link
            to="/editor"
            search={{ agent: undefined, version: undefined }}
            className={buttonClass("default")}
          >
            <Plus size={14} /> New agent
          </Link>
          <Link to="/chat" className={buttonClass("primary")}>
            <SquarePen size={14} /> New chat
          </Link>
        </div>
      </header>

      {/* Plane status — the two-plane split made visible. */}
      <div className="grid gap-4 md:grid-cols-2">
        <InferencePlaneCard
          state={planeState(inference.data, inference.isLoading)}
          modelCount={modelList.length}
          resident={resident}
          maxResident={engines.data?.maxResident}
        />
        <ControlPlaneCard
          state={planeState(control.data, control.isLoading)}
          health={control.data}
          agents={agentsTile}
          chats={chatsTile}
          mcpCount={mcpCount}
          ragCount={rag.data?.length}
        />
      </div>

      {/* KPI tiles. */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile icon={Bot} label="Agents" count={agentsTile} hint="saved" to="/agents" />
        <RunsStatTile runs={runs} loading={runsQ.isLoading} count={runsTile} />
        <StatTile
          icon={MessagesSquare}
          label="Chats"
          count={chatsTile}
          hint="conversations"
          to="/sessions"
        />
        <StatTile
          icon={Boxes}
          label="Models"
          count={modelsTile}
          hint={`${new Set(modelList.map((m) => m.binding.binding)).size} engines`}
          to="/registries"
        />
      </div>

      {/* Latest runs + latest chats. */}
      <div className="grid gap-4 lg:grid-cols-2">
        <LatestRunsPanel runs={runs} loading={runsQ.isLoading} />
        <LatestChatsPanel sessions={sessions} loading={sessionsQ.isLoading} />
      </div>

      {/* Recently used agents + models. */}
      <div className="grid gap-4 lg:grid-cols-2">
        <RecentAgentsPanel
          agents={agents}
          loading={agentsQ.isLoading}
          lastRunByAgent={lastRunByAgent}
        />
        <ModelsPanel
          models={modelList}
          loading={models.isLoading}
          warmIds={warmIds}
          lastRunByModel={lastRunByModel}
        />
      </div>
    </Page>
  );
}

// Map each key (model id / agent id) to the ISO time of its most-recent run.
function lastRunMap(runs: Run[], key: (r: Run) => string | null): Map<string, string> {
  const out = new Map<string, string>();
  for (const r of runs) {
    const k = key(r);
    if (!k) continue;
    const prev = out.get(k);
    if (!prev || r.created_at > prev) out.set(k, r.created_at);
  }
  return out;
}

// ── plane cards ───────────────────────────────────────────────────────────────

function PlaneCardShell({
  icon: Icon,
  title,
  url,
  state,
  children,
}: {
  icon: LucideIcon;
  title: string;
  url: string;
  state: PlaneState;
  children: ReactNode;
}) {
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="flex size-8 items-center justify-center rounded-md bg-muted text-muted-foreground">
            <Icon size={16} />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-medium text-foreground">{title}</div>
            <div className="mono truncate text-[11px] text-muted-foreground/70" title={url}>
              {url.replace(/^https?:\/\//, "")}
            </div>
          </div>
        </div>
        <StatusPill state={state} />
      </div>
      <div className="mt-3">{children}</div>
    </Card>
  );
}

// One metric in a plane card: a value over a small caption. `title` reveals the full number on hover
// when the value is abbreviated (K/M).
function Metric({ value, label, title }: { value: ReactNode; label: string; title?: string }) {
  return (
    <div>
      <div className="text-lg font-semibold tabular-nums text-foreground" title={title}>
        {value}
      </div>
      <div className="text-[11px] text-muted-foreground">{label}</div>
    </div>
  );
}

function InferencePlaneCard({
  state,
  modelCount,
  resident,
  maxResident,
}: {
  state: PlaneState;
  modelCount: number;
  resident: ReturnType<typeof residentEngines>;
  maxResident: number | undefined;
}) {
  const offline = state === "offline";
  return (
    <PlaneCardShell icon={Cpu} title="Inference plane" url={INFERENCE_URL} state={state}>
      {offline ? (
        <p className="text-xs text-muted-foreground">
          Unreachable — the inference plane is user-controlled and runs separately. Start it, then
          this reflects its engines and models.
        </p>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            <Metric value={formatCount(modelCount)} title={exactCount(modelCount)} label="models" />
            <Metric
              value={
                <span>
                  {resident.length}
                  {maxResident != null && (
                    <span className="text-sm font-normal text-muted-foreground">
                      /{maxResident}
                    </span>
                  )}
                </span>
              }
              label="warm"
            />
            <Metric value={new Set(resident.map((e) => e.engine)).size} label="engines" />
          </div>
          {resident.length > 0 ? (
            <div className="mt-3 space-y-1 border-t border-border/60 pt-3">
              <SectionHeading>Running</SectionHeading>
              {resident.slice(0, 4).map((e) => (
                <div key={e.logicalId} className="flex items-center gap-2 text-xs">
                  <Badge tone={engineTone(e.engine)}>{e.engine}</Badge>
                  <span className="mono truncate text-foreground" title={e.logicalId}>
                    {e.logicalId}
                  </span>
                  <span className="ml-auto shrink-0 text-muted-foreground">
                    {e.draining ? "draining" : e.inflight > 0 ? `${e.inflight} in-flight` : "idle"}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <p className="mt-3 border-t border-border/60 pt-3 text-xs text-muted-foreground">
              No engines warm right now — a model spawns on first use.
            </p>
          )}
        </>
      )}
    </PlaneCardShell>
  );
}

function ControlPlaneCard({
  state,
  health,
  agents,
  chats,
  mcpCount,
  ragCount,
}: {
  state: PlaneState;
  health: PlaneHealth | undefined;
  agents: CountDisplay;
  chats: CountDisplay;
  mcpCount: number | undefined;
  ragCount: number | undefined;
}) {
  const offline = state === "offline";
  return (
    <PlaneCardShell icon={Server} title="Control plane" url={CONTROL_PLANE_URL} state={state}>
      {offline ? (
        <p className="text-xs text-muted-foreground">
          Unreachable — agents, runs, chats, and memory live here. Start the control plane to see
          them.
        </p>
      ) : (
        <>
          <div className="grid grid-cols-3 gap-2">
            <Metric value={agents.text} title={agents.title} label="agents" />
            <Metric value={chats.text} title={chats.title} label="chats" />
            <Metric
              value={mcpCount == null ? "—" : formatCount(mcpCount)}
              title={mcpCount == null ? undefined : exactCount(mcpCount)}
              label="MCP servers"
            />
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-border/60 pt-3 text-[11px] text-muted-foreground">
            <span title={ragCount == null ? undefined : exactCount(ragCount)}>
              {ragCount == null ? "—" : formatCount(ragCount)} retrieval sources
            </span>
            {state === "degraded" && health?.reason && (
              <span className="text-amber-600 dark:text-amber-400" title={health.reason}>
                · {health.reason}
              </span>
            )}
          </div>
        </>
      )}
    </PlaneCardShell>
  );
}

// ── KPI tiles ─────────────────────────────────────────────────────────────────

function StatTile({
  icon: Icon,
  label,
  count,
  hint,
  to,
}: {
  icon: LucideIcon;
  label: string;
  count: CountDisplay;
  hint: string;
  to: string;
}) {
  return (
    <Link
      to={to}
      className="group rounded-lg border bg-card p-3 transition hover:ring-1 hover:ring-foreground/15"
    >
      <div className="flex items-center justify-between text-muted-foreground">
        <span className="text-[11px] font-medium uppercase tracking-wide">{label}</span>
        <Icon size={15} className="opacity-70" />
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-foreground" title={count.title}>
        {count.text}
      </div>
      <div className="text-[11px] text-muted-foreground">{hint}</div>
    </Link>
  );
}

// The Runs tile headline is the EXACT total (abbreviated, full number on hover); the status bar below
// is a distribution over the loaded recent window (not the grand total — that would need a per-status
// count endpoint), so it reads as "recent activity".
function RunsStatTile({
  runs,
  loading,
  count,
}: {
  runs: Run[];
  loading: boolean;
  count: CountDisplay;
}) {
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const r of runs) c[r.status] = (c[r.status] ?? 0) + 1;
    return c;
  }, [runs]);
  const windowTotal = runs.length;
  const order = ["completed", "streaming", "waiting", "created", "failed"];
  const present = order.filter((s) => counts[s]);
  return (
    <Link
      to="/runs"
      className="group rounded-lg border bg-card p-3 transition hover:ring-1 hover:ring-foreground/15"
    >
      <div className="flex items-center justify-between text-muted-foreground">
        <span className="text-[11px] font-medium uppercase tracking-wide">Runs</span>
        <Activity size={15} className="opacity-70" />
      </div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-foreground" title={count.title}>
        {count.text}
      </div>
      {windowTotal > 0 ? (
        <>
          <div className="mt-1.5 flex h-1.5 w-full overflow-hidden rounded-full bg-muted">
            {present.map((s) => (
              <span
                key={s}
                className={toneOf(statusTone(s)).dot}
                style={{ width: `${(counts[s] / windowTotal) * 100}%` }}
                title={`${counts[s]} ${s}`}
              />
            ))}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {counts.failed ? `${counts.failed} failed recently` : "recent activity"}
          </div>
        </>
      ) : (
        <div className="text-[11px] text-muted-foreground">{loading ? "loading…" : "no runs"}</div>
      )}
    </Link>
  );
}

// ── panels ──────────────────────────────────────────────────────────────────

// A panel shell: a titled card with a "Show all →" link and a body that renders a list, a loading
// skeleton, or an empty note.
function Panel({
  title,
  icon: Icon,
  to,
  showAllLabel = "Show all",
  children,
}: {
  title: string;
  icon: LucideIcon;
  to: string;
  showAllLabel?: string;
  children: ReactNode;
}) {
  return (
    <Card className="flex flex-col p-0">
      <div className="flex items-center justify-between border-b border-border/60 px-4 py-2.5">
        <div className="flex items-center gap-2">
          <Icon size={15} className="text-muted-foreground" />
          <h2 className="text-sm font-medium text-foreground">{title}</h2>
        </div>
        <Link to={to} className={`inline-flex items-center gap-1 text-xs ${linkClass}`}>
          {showAllLabel} <ArrowRight size={12} />
        </Link>
      </div>
      <div className="min-w-0 flex-1 p-2">{children}</div>
    </Card>
  );
}

function ListSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-1">
      {Array.from({ length: rows }, (_, i) => `r${i}`).map((k) => (
        <Skeleton key={k} className="h-7 w-full" />
      ))}
    </div>
  );
}

function EmptyNote({ children }: { children: ReactNode }) {
  return <p className="px-2 py-6 text-center text-xs text-muted-foreground">{children}</p>;
}

const RECENT = 6;

function LatestRunsPanel({ runs, loading }: { runs: Run[]; loading: boolean }) {
  return (
    <Panel title="Latest runs" icon={Activity} to="/runs">
      {loading && runs.length === 0 ? (
        <ListSkeleton />
      ) : runs.length === 0 ? (
        <EmptyNote>No runs yet — start a chat or invoke an agent.</EmptyNote>
      ) : (
        <ul className="space-y-0.5">
          {runs.slice(0, RECENT).map((r) => (
            <li key={r.id}>
              <Link
                to="/runs/$runId"
                params={{ runId: r.id }}
                className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-muted"
              >
                <Dot tone={statusTone(r.status)} />
                <span className="mono truncate text-xs text-foreground" title={r.id}>
                  {runLabel(r)}
                </span>
                <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                  <TimeAgo iso={r.created_at} />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function LatestChatsPanel({
  sessions,
  loading,
}: {
  sessions: SessionSummary[];
  loading: boolean;
}) {
  return (
    <Panel title="Latest chats" icon={MessagesSquare} to="/sessions">
      {loading && sessions.length === 0 ? (
        <ListSkeleton />
      ) : sessions.length === 0 ? (
        <EmptyNote>No conversations yet — start a new chat.</EmptyNote>
      ) : (
        <ul className="space-y-0.5">
          {sessions.slice(0, RECENT).map((s) => (
            <li key={s.id}>
              <Link
                to="/sessions/$sessionId"
                params={{ sessionId: s.id }}
                className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-muted"
              >
                <MessagesSquare size={13} className="shrink-0 text-muted-foreground" />
                <span className="truncate text-xs text-foreground" title={s.preview ?? s.id}>
                  {sessionLabel(s)}
                </span>
                <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                  <TimeAgo iso={s.last_activity} />
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}

function RecentAgentsPanel({
  agents,
  loading,
  lastRunByAgent,
}: {
  agents: { id: string; name: string; updated_at: string; latest_version: string | null }[];
  loading: boolean;
  lastRunByAgent: Map<string, string>;
}) {
  // Order by "last used" when we have run history for it, else by last modified — the honest recency.
  const ordered = useMemo(() => {
    return [...agents]
      .sort((a, b) => {
        const at = lastRunByAgent.get(a.id) ?? a.updated_at;
        const bt = lastRunByAgent.get(b.id) ?? b.updated_at;
        return bt.localeCompare(at);
      })
      .slice(0, RECENT);
  }, [agents, lastRunByAgent]);

  return (
    <Panel title="Recent agents" icon={Bot} to="/agents">
      {loading && agents.length === 0 ? (
        <ListSkeleton />
      ) : agents.length === 0 ? (
        <EmptyNote>No saved agents yet — build one on the canvas.</EmptyNote>
      ) : (
        <ul className="space-y-0.5">
          {ordered.map((a) => {
            const lastRun = lastRunByAgent.get(a.id);
            return (
              <li key={a.id}>
                <Link
                  to="/editor"
                  search={{ agent: a.id, version: a.latest_version ?? undefined }}
                  className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-muted"
                >
                  <Bot size={13} className="shrink-0 text-muted-foreground" />
                  <span className="truncate text-xs text-foreground" title={a.id}>
                    {a.name}
                  </span>
                  {a.latest_version && (
                    <span className="mono shrink-0 text-[10px] text-primary">
                      v{a.latest_version}
                    </span>
                  )}
                  <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                    {lastRun ? (
                      <>
                        ran <TimeAgo iso={lastRun} />
                      </>
                    ) : (
                      <TimeAgo iso={a.updated_at} />
                    )}
                  </span>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}

function ModelsPanel({
  models,
  loading,
  warmIds,
  lastRunByModel,
}: {
  models: ModelView[];
  loading: boolean;
  warmIds: Set<string>;
  lastRunByModel: Map<string, string>;
}) {
  // Warm models first, then most-recently used, then the rest — the operator's "what's live" order.
  const ordered = useMemo(() => {
    return [...models]
      .sort((a, b) => {
        const aw = warmIds.has(a.logicalId) ? 1 : 0;
        const bw = warmIds.has(b.logicalId) ? 1 : 0;
        if (aw !== bw) return bw - aw;
        const at = lastRunByModel.get(a.logicalId) ?? "";
        const bt = lastRunByModel.get(b.logicalId) ?? "";
        return bt.localeCompare(at);
      })
      .slice(0, RECENT);
  }, [models, warmIds, lastRunByModel]);

  return (
    <Panel title="Models" icon={Boxes} to="/registries" showAllLabel="Manage">
      {loading && models.length === 0 ? (
        <ListSkeleton />
      ) : models.length === 0 ? (
        <EmptyNote>
          No models registered —{" "}
          <Link to="/registries" className={linkClass}>
            browse & install →
          </Link>
        </EmptyNote>
      ) : (
        <ul className="space-y-0.5">
          {ordered.map((m) => {
            const lastRun = lastRunByModel.get(m.logicalId);
            const warm = warmIds.has(m.logicalId);
            return (
              <li
                key={m.logicalId}
                className="flex items-center gap-2 rounded-md px-2 py-1.5"
                title={warm ? "warm — resident in memory" : undefined}
              >
                <Badge tone={engineTone(m.binding.binding)}>{m.binding.binding}</Badge>
                <span className="mono truncate text-xs text-foreground" title={m.logicalId}>
                  {m.logicalId}
                </span>
                {warm && <Dot tone="green" className="shrink-0" />}
                <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
                  {lastRun ? <TimeAgo iso={lastRun} /> : (m.binding.modality ?? "chat")}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
