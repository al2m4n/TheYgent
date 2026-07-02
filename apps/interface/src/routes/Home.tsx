// Home — the Agents page: saved agents from the registry as a compact card grid. Each card shows a
// live preview of the agent's graph (rendered from its latest IR; falls back to a placeholder
// identicon for an agent with no saved version), the name + key metadata (version, node count, last
// modified), and a footer with the author + a Bench action. Click a card to open the agent on the
// canvas. A search + sort bar sits on top. Read-only over the registry; no new endpoints.

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { AgentBench } from "../bench/AgentBench";
import { AgentThumbnail, useThumbVariant } from "../components/AgentThumbnail";
import { FilterBar } from "../components/Filters";
import { GraphPreview } from "../components/GraphPreview";
import {
  Badge,
  Button,
  Empty,
  ErrorBanner,
  Modal,
  Page,
  Select,
  Spinner,
  buttonClass,
} from "../components/ui";
import { fromStoredVersion } from "../lib/agent";
import { type AgentDetail, type AgentSummary, api } from "../lib/api";
import { relativeTime } from "../lib/format";

type Sort = "modified" | "created" | "name" | "versions";

const SORT_LABEL: Record<Sort, string> = {
  modified: "Recently modified",
  created: "Recently created",
  name: "Name (A–Z)",
  versions: "Most versions",
};

export function Home() {
  const {
    data: agents,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.listAgents({ limit: 100 }),
  });
  const [benchAgentId, setBenchAgentId] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<Sort>("modified");

  const shown = useMemo(() => {
    const arr = [...(agents ?? [])];
    arr.sort((a, b) => {
      switch (sort) {
        case "name":
          return a.name.localeCompare(b.name);
        case "created":
          return b.created_at.localeCompare(a.created_at);
        case "versions":
          return b.version_count - a.version_count;
        default:
          return b.updated_at.localeCompare(a.updated_at);
      }
    });
    const needle = q.trim().toLowerCase();
    return needle ? arr.filter((a) => `${a.name} ${a.id}`.toLowerCase().includes(needle)) : arr;
  }, [agents, sort, q]);

  return (
    <Page className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Agents</h1>
          <p className="text-xs text-slate-500">
            Your saved agents — open one on the canvas, bench it, or create a new one.
          </p>
        </div>
        <Link
          to="/editor"
          search={{ agent: undefined, version: undefined }}
          className={buttonClass("primary", "shrink-0")}
        >
          <Plus size={14} /> New agent
        </Link>
      </div>

      {isLoading && <Spinner />}
      <ErrorBanner
        error={error && `Could not reach the control plane: ${(error as Error).message}`}
      />

      {agents && agents.length === 0 && (
        <Empty>
          <p>No saved agents yet.</p>
          <p className="mt-1 text-xs text-slate-600">
            Create one on the canvas and save it to see it here.
          </p>
          <Link
            to="/editor"
            search={{ agent: undefined, version: undefined }}
            className={buttonClass("primary", "mt-4")}
          >
            <Plus size={14} /> New agent
          </Link>
        </Empty>
      )}

      {agents && agents.length > 0 && (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search agents…"
            total={agents.length}
            shown={shown.length}
            onClear={q ? () => setQ("") : undefined}
            trailing={
              <label className="flex items-center gap-1.5 text-xs text-slate-500">
                Sort
                <Select
                  value={sort}
                  onChange={(e) => setSort(e.target.value as Sort)}
                  className="!w-auto"
                >
                  {(Object.keys(SORT_LABEL) as Sort[]).map((s) => (
                    <option key={s} value={s}>
                      {SORT_LABEL[s]}
                    </option>
                  ))}
                </Select>
              </label>
            }
          />

          {shown.length === 0 ? (
            <Empty>No agents match the current filters.</Empty>
          ) : (
            <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(220px,1fr))]">
              {shown.map((a) => (
                <AgentCard key={a.id} agent={a} onBench={() => setBenchAgentId(a.id)} />
              ))}
            </div>
          )}
        </>
      )}

      {benchAgentId && <BenchModal agentId={benchAgentId} onClose={() => setBenchAgentId(null)} />}
    </Page>
  );
}

function AgentCard({ agent, onBench }: { agent: AgentSummary; onBench: () => void }) {
  const { seed, reroll } = useThumbVariant(agent.id);
  const hasVersion = !!agent.latest_version;

  // The card's preview is the agent's latest graph. A pinned version's IR is immutable, so cache it
  // hard. Lazy per card (only agents that have a version fetch); falls back to the identicon.
  const { data: version, isError } = useQuery({
    queryKey: ["agentVersion", agent.id, agent.latest_version],
    queryFn: () => api.getAgentVersion(agent.id, agent.latest_version as string),
    enabled: hasVersion,
    staleTime: 5 * 60 * 1000,
  });
  const ir = version ? fromStoredVersion(version) : null;
  const loadingGraph = hasVersion && !ir && !isError;
  const showIdenticon = !ir && !loadingGraph; // no version, or the IR fetch failed

  return (
    <div className="group relative flex flex-col overflow-hidden rounded-lg border border-slate-800 bg-[var(--c-surface-2)] transition-colors hover:border-slate-600">
      <Link
        to="/editor"
        search={{ agent: agent.id, version: agent.latest_version ?? undefined }}
        className="flex flex-1 flex-col"
      >
        <div className="aspect-[16/10] w-full overflow-hidden border-b border-slate-800/60">
          {ir ? (
            <GraphPreview ir={ir} className="h-full w-full" />
          ) : loadingGraph ? (
            <div className="h-full w-full animate-pulse bg-[var(--c-surface)]" />
          ) : (
            <AgentThumbnail seed={seed} className="h-full w-full" />
          )}
        </div>
        <div className="flex flex-1 flex-col gap-1 p-3">
          <div className="flex items-start justify-between gap-2">
            <span className="truncate text-sm font-medium text-slate-100" title={agent.name}>
              {agent.name}
            </span>
            {agent.latest_version && (
              <Badge tone="blue">
                <span className="mono">v{agent.latest_version}</span>
              </Badge>
            )}
          </div>
          <div className="mono truncate text-[11px] text-slate-600" title={agent.id}>
            {agent.id}
          </div>
          <div className="mt-auto flex flex-wrap items-center gap-x-1.5 pt-1 text-[11px] text-slate-500">
            <span>Updated {relativeTime(agent.updated_at)}</span>
            <span>·</span>
            <span>
              {agent.version_count} version{agent.version_count === 1 ? "" : "s"}
            </span>
            {ir && (
              <>
                <span>·</span>
                <span>{(ir.nodes ?? []).length} nodes</span>
              </>
            )}
          </div>
        </div>
      </Link>

      {/* "possibility of change" — only meaningful for the placeholder identicon (a graph preview is
          derived from the IR). Sibling of the link so it's its own click target. */}
      {showIdenticon && (
        <button
          type="button"
          onClick={reroll}
          title="Change thumbnail (placeholder)"
          className="absolute top-2 right-2 z-10 rounded-md border border-white/20 bg-black/40 px-2 py-0.5 text-[11px] font-medium text-white opacity-0 backdrop-blur-sm transition-opacity hover:bg-black/60 focus-visible:opacity-100 group-hover:opacity-100"
        >
          Change
        </button>
      )}

      <div className="flex items-center justify-between border-t border-slate-800/60 px-3 py-2">
        <span className="text-[11px] text-slate-500">By me</span>
        <Button
          variant="primary"
          disabled={!hasVersion}
          onClick={onBench}
          className="!px-2 !py-1 text-xs"
        >
          Bench
        </Button>
      </div>
    </div>
  );
}

// Loads the agent's detail (its version list) then opens the agent bench. Scoped to the open modal
// so the detail fetch happens on demand, not for every card.
function BenchModal({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["agent", agentId],
    queryFn: () => api.getAgent(agentId),
  });
  const agent = data as AgentDetail | undefined;
  return (
    <Modal title={agent ? `Bench · ${agent.name}` : "Bench"} width="max-w-5xl" onClose={onClose}>
      {error ? (
        <ErrorBanner error={error} />
      ) : isLoading || !agent ? (
        <Spinner />
      ) : agent.versions.length === 0 ? (
        <p className="text-sm text-slate-500">This agent has no versions yet.</p>
      ) : (
        <AgentBench agent={agent} />
      )}
    </Modal>
  );
}
