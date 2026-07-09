import { Link } from "@tanstack/react-router";
import { Activity } from "lucide-react";
import { useMemo, useState } from "react";
import { CategoryBadge, FilterBar } from "../components/Filters";
import { TimeAgo } from "../components/TimeAgo";
import { ErrorBanner, Page, Spinner, buttonClass, linkClass } from "../components/ui";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyMedia,
  EmptyTitle,
} from "../components/ui/empty";
import { Skeleton } from "../components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { countBy, statusTone, toggle } from "../lib/categories";
import { shortId } from "../lib/format";
import { useInView } from "../lib/useInView";
import { flattenPages, useRunsInfinite } from "../queries";

// The natural status reading order (lifecycle), so the chips don't reorder as counts change.
const STATUS_ORDER = ["created", "streaming", "waiting", "completed", "failed"];

// The header row is shared by the loaded table and the loading skeleton so the columns never jump.
function RunsTableHeader() {
  return (
    <TableHeader>
      <TableRow>
        <TableHead>Run</TableHead>
        <TableHead>Status</TableHead>
        <TableHead>Model</TableHead>
        <TableHead>Graph</TableHead>
        <TableHead>Session</TableHead>
        <TableHead>Created</TableHead>
      </TableRow>
    </TableHeader>
  );
}

// Static keys: skeleton rows are pure placeholders with no identity of their own.
const SKELETON_ROWS = ["s1", "s2", "s3", "s4", "s5"];

function RunsTableSkeleton() {
  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <Table>
        <RunsTableHeader />
        <TableBody>
          {SKELETON_ROWS.map((k) => (
            <TableRow key={k}>
              <TableCell>
                <Skeleton className="h-4 w-56" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-20" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-32" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-24" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-24" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-16" />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

export function RunsList() {
  const { data, isLoading, error, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useRunsInfinite();
  const runs = useMemo(() => flattenPages(data), [data]);
  const loadMoreRef = useInView(fetchNextPage, { enabled: hasNextPage && !isFetchingNextPage });
  const [statusSel, setStatusSel] = useState<string[]>([]);
  const [q, setQ] = useState("");

  const statusCounts = useMemo(() => countBy(runs ?? [], (r) => r.status), [runs]);

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (runs ?? []).filter((r) => {
      if (statusSel.length && !statusSel.includes(r.status)) return false;
      if (needle) {
        const hay =
          `${r.id} ${r.model ?? ""} ${r.graph_id ?? ""} ${r.session_id ?? ""}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      return true;
    });
  }, [runs, statusSel, q]);

  const statusFacet = {
    label: "Status",
    selected: statusSel,
    onToggle: (v: string) => setStatusSel((s) => toggle(s, v)),
    options: STATUS_ORDER.filter((s) => statusCounts[s]).map((s) => ({
      value: s,
      label: s,
      tone: statusTone(s),
      count: statusCounts[s],
    })),
  };

  return (
    <Page className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold text-foreground">Runs</h1>
          <p className="text-xs text-muted-foreground">Newest first · auto-refreshing every 3s</p>
        </div>
        <Link to="/chat" className={buttonClass("primary", "shrink-0")}>
          New chat
        </Link>
      </div>

      <ErrorBanner error={error} />

      {isLoading ? (
        <RunsTableSkeleton />
      ) : !runs || runs.length === 0 ? (
        <Empty className="border py-10">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <Activity />
            </EmptyMedia>
            <EmptyTitle>No runs yet</EmptyTitle>
            <EmptyDescription>
              Runs come from chats, agent invocations, and triggers —{" "}
              <Link to="/chat" className={linkClass}>
                start a chat →
              </Link>
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search id, model…"
            facets={[statusFacet]}
            total={runs.length}
            shown={filtered.length}
            onClear={() => {
              setStatusSel([]);
              setQ("");
            }}
          />

          {filtered.length === 0 ? (
            <Empty className="border py-10">
              <EmptyHeader>
                <EmptyTitle>No matching runs</EmptyTitle>
                <EmptyDescription>No runs match the current filters.</EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <div className="overflow-hidden rounded-xl border bg-card">
              <Table>
                <RunsTableHeader />
                <TableBody>
                  {filtered.map((run) => (
                    <TableRow key={run.id}>
                      {/* The run id is the row's identity — show it in full (no truncation); it keeps
                          to one line and the table scrolls horizontally on a narrow viewport. */}
                      <TableCell>
                        <Link
                          to="/runs/$runId"
                          params={{ runId: run.id }}
                          className={`mono ${linkClass}`}
                        >
                          {run.id}
                        </Link>
                      </TableCell>
                      <TableCell>
                        {/* Clicking a status chip toggles it as a filter. */}
                        <CategoryBadge
                          tone={statusTone(run.status)}
                          active={statusSel.includes(run.status)}
                          onClick={() => setStatusSel((s) => toggle(s, run.status))}
                          title={`Filter by ${run.status}`}
                        >
                          {run.status}
                        </CategoryBadge>
                      </TableCell>
                      <TableCell className="mono">{run.model || "—"}</TableCell>
                      <TableCell className="mono text-muted-foreground">
                        {run.graph_id ? `${shortId(run.graph_id, 10)}@${run.graph_version}` : "—"}
                      </TableCell>
                      <TableCell>
                        {run.session_id ? (
                          <Link
                            to="/sessions/$sessionId"
                            params={{ sessionId: run.session_id }}
                            className="mono text-muted-foreground hover:text-foreground"
                          >
                            {shortId(run.session_id, 10)}
                          </Link>
                        ) : (
                          <span className="text-muted-foreground/60">—</span>
                        )}
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        <TimeAgo iso={run.created_at} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
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
    </Page>
  );
}
