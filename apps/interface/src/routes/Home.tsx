// Home — the Agents page: published agents from the registry as a compact card grid, with the
// work-in-progress drafts (the editor's autosaves) in a strip above them. Each card shows a live
// preview of the agent's graph (rendered from its latest IR; falls back to a placeholder identicon
// for an agent with no published version), the name + key metadata (version, node count, last
// modified), and a footer with the author + a Bench action. Click a card to open the agent on the
// canvas. A search + sort bar sits on top.

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Bot, ChevronRight, NotebookPen, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { AgentBench } from "../bench/AgentBench";
import { AgentThumbnail, useThumbVariant } from "../components/AgentThumbnail";
import { FilterBar } from "../components/Filters";
import { GraphPreview } from "../components/GraphPreview";
import { TimeAgo } from "../components/TimeAgo";
import { ViewToggle } from "../components/ViewToggle";
import {
  ConfirmDialog,
  ErrorBanner,
  Modal,
  Page,
  Spinner,
  buttonClass,
  linkClass,
} from "../components/ui";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardFooter } from "../components/ui/card";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../components/ui/collapsible";
import {
  Empty,
  EmptyContent,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "../components/ui/empty";
import {
  Item,
  ItemActions,
  ItemContent,
  ItemDescription,
  ItemGroup,
  ItemMedia,
  ItemSeparator,
  ItemTitle,
} from "../components/ui/item";
import { NativeSelect, NativeSelectOption } from "../components/ui/native-select";
import { Skeleton } from "../components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { fromStoredVersion } from "../lib/agent";
import { type AgentDetail, type AgentSummary, type DraftSummary, api } from "../lib/api";
import { useInView } from "../lib/useInView";
import { useViewMode } from "../lib/viewMode";
import { flattenPages, useAgentsInfinite, useDraftMutations, useDrafts } from "../queries";

type Sort = "modified" | "created" | "name" | "versions";

const SORT_LABEL: Record<Sort, string> = {
  modified: "Recently modified",
  created: "Recently created",
  name: "Name (A–Z)",
  versions: "Most versions",
};

export function Home() {
  const { data, isLoading, error, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useAgentsInfinite();
  const agents = useMemo(() => flattenPages(data), [data]);
  const loadMoreRef = useInView(fetchNextPage, { enabled: hasNextPage && !isFetchingNextPage });
  const [benchAgentId, setBenchAgentId] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [sort, setSort] = useState<Sort>("modified");
  const [view, setView] = useViewMode("agents", "grid");

  // Work-in-progress drafts (the editor's autosaves) — most recently edited first. A draft that
  // edits a published agent also badges that agent's card/row below.
  const draftsQuery = useDrafts();
  const drafts = useMemo(
    () => [...(draftsQuery.data ?? [])].sort((a, b) => b.updated_at.localeCompare(a.updated_at)),
    [draftsQuery.data],
  );
  const draftByAgent = useMemo(() => {
    const m = new Map<string, DraftSummary>();
    // Iterated oldest-last so the NEWEST draft per agent wins the badge.
    for (const d of [...drafts].reverse()) if (d.agent_id) m.set(d.agent_id, d);
    return m;
  }, [drafts]);

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
          <h1 className="text-lg font-semibold text-foreground">Agents</h1>
          <p className="text-xs text-muted-foreground">
            Your published agents — open one on the canvas, bench it, or create a new one.
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

      {drafts.length > 0 && <DraftsStrip drafts={drafts} />}

      {!isLoading && !error && agents.length === 0 && (
        <Empty className="border py-10">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Bot />
            </EmptyMedia>
            <EmptyTitle>No published agents yet</EmptyTitle>
            <EmptyDescription>
              Build one on the canvas and publish it to see it here.
            </EmptyDescription>
          </EmptyHeader>
          <EmptyContent>
            <Link
              to="/editor"
              search={{ agent: undefined, version: undefined }}
              className={buttonClass("primary")}
            >
              <Plus size={14} /> New agent
            </Link>
          </EmptyContent>
        </Empty>
      )}

      {agents.length > 0 && (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search agents…"
            total={agents.length}
            shown={shown.length}
            onClear={q ? () => setQ("") : undefined}
            trailing={
              <>
                <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
                  Sort
                  <NativeSelect
                    size="sm"
                    value={sort}
                    onChange={(e) => setSort(e.target.value as Sort)}
                  >
                    {(Object.keys(SORT_LABEL) as Sort[]).map((s) => (
                      <NativeSelectOption key={s} value={s}>
                        {SORT_LABEL[s]}
                      </NativeSelectOption>
                    ))}
                  </NativeSelect>
                </label>
                <ViewToggle value={view} onChange={setView} />
              </>
            }
          />

          {shown.length === 0 ? (
            <Empty className="border py-10">
              <EmptyDescription>No agents match the current filters.</EmptyDescription>
            </Empty>
          ) : view === "grid" ? (
            <div className="grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(220px,1fr))]">
              {shown.map((a) => (
                <AgentCard
                  key={a.id}
                  agent={a}
                  draft={draftByAgent.get(a.id)}
                  onBench={() => setBenchAgentId(a.id)}
                />
              ))}
            </div>
          ) : (
            <AgentTable agents={shown} draftByAgent={draftByAgent} onBench={setBenchAgentId} />
          )}

          {/* Scroll sentinel: pulls the next (older) page as it nears the viewport — no button. */}
          {hasNextPage && <div ref={loadMoreRef} aria-hidden className="h-px" />}
          {isFetchingNextPage && (
            <div className="flex justify-center py-3">
              <Spinner />
            </div>
          )}
        </>
      )}

      {benchAgentId && <BenchModal agentId={benchAgentId} onClose={() => setBenchAgentId(null)} />}
    </Page>
  );
}

// The disclosure state of the drafts strip. Defaults OPEN (a draft is exactly the thing you were
// working on); the toggle persists like the sidebar groups do. Guarded for non-DOM (test) envs.
const DRAFTS_OPEN_KEY = "theygent.ui.draftsOpen";

function readDraftsOpen(): boolean {
  try {
    return typeof localStorage === "undefined" || localStorage.getItem(DRAFTS_OPEN_KEY) !== "0";
  } catch {
    return true;
  }
}

function writeDraftsOpen(open: boolean): void {
  try {
    localStorage.setItem(DRAFTS_OPEN_KEY, open ? "1" : "0");
  } catch {
    // no localStorage (tests) — the in-memory state still drives the UI this session.
  }
}

// The work-in-progress strip: every autosaved draft, most recently edited first. A draft opens
// straight into the editor session it bridges; discarding is the one destructive action, so it
// confirms. Rendered above the published grid — a draft is exactly the thing you were working
// on — but collapsible (persisted), with the count always visible so a folded strip still says
// drafts exist.
function DraftsStrip({ drafts }: { drafts: DraftSummary[] }) {
  const { remove } = useDraftMutations();
  const [confirming, setConfirming] = useState<DraftSummary | null>(null);
  const [open, setOpen] = useState(readDraftsOpen);
  return (
    <Collapsible
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        writeDraftsOpen(next);
      }}
      className="space-y-2"
    >
      <CollapsibleTrigger className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground hover:text-foreground">
        <ChevronRight size={13} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        Drafts
        <Badge variant="secondary" className="text-[11px]">
          {drafts.length}
        </Badge>
      </CollapsibleTrigger>
      <CollapsibleContent className="rounded-xl border bg-card">
        <ItemGroup>
          {drafts.map((d, i) => (
            <div key={d.id}>
              {i > 0 && <ItemSeparator />}
              <Item size="sm">
                <ItemMedia variant="icon">
                  <NotebookPen />
                </ItemMedia>
                <ItemContent>
                  <ItemTitle>
                    <Link to="/editor" search={{ draft: d.id }} className={linkClass}>
                      {d.name}
                    </Link>
                    <Badge
                      variant="secondary"
                      className="bg-amber-500/15 text-[11px] text-amber-700 dark:text-amber-300"
                    >
                      Draft
                    </Badge>
                  </ItemTitle>
                  <ItemDescription>
                    {d.agent_id ? (
                      <>
                        editing <span className="mono">{d.agent_id}</span> ·{" "}
                      </>
                    ) : (
                      "not published yet · "
                    )}
                    {d.node_count} node{d.node_count === 1 ? "" : "s"} · saved{" "}
                    <TimeAgo iso={d.updated_at} />
                  </ItemDescription>
                </ItemContent>
                <ItemActions>
                  <Link to="/editor" search={{ draft: d.id }} className={buttonClass("default")}>
                    Open
                  </Link>
                  <Button variant="ghost" size="sm" onClick={() => setConfirming(d)}>
                    Discard
                  </Button>
                </ItemActions>
              </Item>
            </div>
          ))}
        </ItemGroup>
      </CollapsibleContent>
      {confirming && (
        <ConfirmDialog
          title="Discard this draft?"
          message={
            <>
              The draft <span className="font-medium">{confirming.name}</span> and its unpublished
              changes will be permanently removed.
              {confirming.agent_id && " Published versions of the agent are not affected."}
            </>
          }
          confirmLabel="Discard"
          onConfirm={() => {
            remove.mutate(confirming.id);
            setConfirming(null);
          }}
          onCancel={() => setConfirming(null)}
        />
      )}
    </Collapsible>
  );
}

// The list rendering: the same agents as a compact table. No per-row graph fetch (that's the grid's
// job) — just the registry summary each row already carries, so it stays light at any length.
function AgentTable({
  agents,
  draftByAgent,
  onBench,
}: {
  agents: AgentSummary[];
  draftByAgent: Map<string, DraftSummary>;
  onBench: (id: string) => void;
}) {
  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Agent</TableHead>
            <TableHead>Version</TableHead>
            <TableHead>Versions</TableHead>
            <TableHead>Updated</TableHead>
            <TableHead className="text-right">Run</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {agents.map((a) => (
            <TableRow key={a.id}>
              <TableCell>
                <Link
                  to="/editor"
                  search={{ agent: a.id, version: a.latest_version ?? undefined }}
                  className="flex flex-col"
                >
                  <span className={`truncate font-medium ${linkClass}`} title={a.name}>
                    {a.name}
                  </span>
                  <span className="mono truncate text-[11px] text-muted-foreground/70" title={a.id}>
                    {a.id}
                  </span>
                </Link>
              </TableCell>
              <TableCell>
                <span className="inline-flex items-center gap-1.5">
                  {a.latest_version ? (
                    <Badge
                      variant="secondary"
                      className="mono bg-primary/10 text-[11px] text-primary"
                    >
                      v{a.latest_version}
                    </Badge>
                  ) : (
                    <span className="text-muted-foreground/60">—</span>
                  )}
                  <DraftBadge draft={draftByAgent.get(a.id)} />
                </span>
              </TableCell>
              <TableCell className="text-muted-foreground">{a.version_count}</TableCell>
              <TableCell className="text-muted-foreground">
                <TimeAgo iso={a.updated_at} />
              </TableCell>
              <TableCell className="text-right">
                <Button size="sm" disabled={!a.latest_version} onClick={() => onBench(a.id)}>
                  Run
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// A published agent with unpublished draft edits wears a small amber badge that jumps straight
// into that draft's editing session.
function DraftBadge({ draft }: { draft: DraftSummary | undefined }) {
  if (!draft) return null;
  return (
    <Link
      to="/editor"
      search={{ draft: draft.id }}
      title={`Unpublished draft edits — saved ${new Date(draft.updated_at).toLocaleString()}`}
    >
      <Badge
        variant="secondary"
        className="bg-amber-500/15 text-[11px] text-amber-700 hover:bg-amber-500/25 dark:text-amber-300"
      >
        draft
      </Badge>
    </Link>
  );
}

function AgentCard({
  agent,
  draft,
  onBench,
}: {
  agent: AgentSummary;
  draft: DraftSummary | undefined;
  onBench: () => void;
}) {
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
    <Card className="group relative gap-0 py-0 transition hover:ring-foreground/25">
      <Link
        to="/editor"
        search={{ agent: agent.id, version: agent.latest_version ?? undefined }}
        className="flex flex-1 flex-col"
      >
        <div className="aspect-[16/10] w-full overflow-hidden border-b border-border/60">
          {ir ? (
            <GraphPreview ir={ir} className="h-full w-full" />
          ) : loadingGraph ? (
            <Skeleton className="h-full w-full rounded-none" />
          ) : (
            <AgentThumbnail seed={seed} className="h-full w-full" />
          )}
        </div>
        <div className="flex flex-1 flex-col gap-1 p-3">
          <div className="flex items-start justify-between gap-2">
            <span className="truncate text-sm font-medium text-foreground" title={agent.name}>
              {agent.name}
            </span>
            {agent.latest_version && (
              <Badge
                variant="secondary"
                className="mono shrink-0 bg-primary/10 text-[11px] text-primary"
              >
                v{agent.latest_version}
              </Badge>
            )}
          </div>
          {draft && (
            <div className="flex">
              <Badge
                variant="secondary"
                className="bg-amber-500/15 text-[11px] text-amber-700 dark:text-amber-300"
                title="This agent has unpublished draft edits — see the Drafts strip above"
              >
                draft
              </Badge>
            </div>
          )}
          <div className="mono truncate text-[11px] text-muted-foreground/70" title={agent.id}>
            {agent.id}
          </div>
          <div className="mt-auto flex flex-wrap items-center gap-x-1.5 pt-1 text-[11px] text-muted-foreground">
            <span>
              Updated <TimeAgo iso={agent.updated_at} />
            </span>
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

      <CardFooter className="justify-between border-t px-3 py-2">
        <span className="text-[11px] text-muted-foreground">By me</span>
        <Button size="sm" disabled={!hasVersion} onClick={onBench}>
          Run
        </Button>
      </CardFooter>
    </Card>
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
    <Modal title={agent ? `Run · ${agent.name}` : "Run"} width="max-w-5xl" onClose={onClose}>
      {error ? (
        <ErrorBanner error={error} />
      ) : isLoading || !agent ? (
        <Spinner />
      ) : agent.versions.length === 0 ? (
        <p className="text-sm text-muted-foreground">This agent has no versions yet.</p>
      ) : (
        <AgentBench agent={agent} />
      )}
    </Modal>
  );
}
