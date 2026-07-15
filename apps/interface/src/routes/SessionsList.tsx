import { Link } from "@tanstack/react-router";
import { MessagesSquare } from "lucide-react";
import { useMemo, useState } from "react";
import { FilterBar } from "../components/Filters";
import { TimeAgo } from "../components/TimeAgo";
import { TimeRangeFilter } from "../components/TimeRangeFilter";
import { Badge, ErrorBanner, Page, Spinner, buttonClass, linkClass } from "../components/ui";
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
import type { SessionSummary } from "../lib/runtypes";
import { ALL_TIME, type TimeRange, inRange, isActive } from "../lib/timeRange";
import { useInView } from "../lib/useInView";
import { useNow } from "../lib/useNow";
import { flattenPages, useSessionsInfinite } from "../queries";

// What the session was talking to, from its stored metadata (absent on sessions recorded before
// targets were stored — those render a dash).
function targetOf(s: SessionSummary): string | null {
  const meta = s.metadata ?? {};
  if (typeof meta.agent_name === "string") return `agent · ${meta.agent_name}`;
  if (typeof meta.agent_id === "string") return "agent";
  if (typeof meta.model === "string") return String(meta.model);
  return null;
}

// The header row is shared by the loaded table and the loading skeleton so the columns never jump.
function SessionsTableHeader() {
  return (
    <TableHeader>
      <TableRow>
        <TableHead>Session</TableHead>
        <TableHead>Target</TableHead>
        <TableHead>Messages</TableHead>
        <TableHead>First message</TableHead>
        <TableHead>Last activity</TableHead>
      </TableRow>
    </TableHeader>
  );
}

// Static keys: skeleton rows are pure placeholders with no identity of their own.
const SKELETON_ROWS = ["s1", "s2", "s3", "s4", "s5"];

function SessionsTableSkeleton() {
  return (
    <div className="overflow-hidden rounded-xl border bg-card">
      <Table>
        <SessionsTableHeader />
        <TableBody>
          {SKELETON_ROWS.map((k) => (
            <TableRow key={k}>
              <TableCell>
                <Skeleton className="h-4 w-56" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-28" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-10" />
              </TableCell>
              <TableCell>
                <Skeleton className="h-4 w-64" />
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

export function SessionsList() {
  const { data, isLoading, error, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useSessionsInfinite();
  const sessions = useMemo(() => flattenPages(data), [data]);
  const loadMoreRef = useInView(fetchNextPage, { enabled: hasNextPage && !isFetchingNextPage });
  const [q, setQ] = useState("");
  const [range, setRange] = useState<TimeRange>(ALL_TIME);
  // Keep a relative window ("last 5 minutes") rolling as time passes, not frozen at load.
  const now = useNow(range.type === "relative");

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (sessions ?? []).filter((t) => {
      if (!inRange(t.last_activity, range, now)) return false;
      if (!needle) return true;
      return `${t.id} ${t.preview ?? ""} ${targetOf(t) ?? ""}`.toLowerCase().includes(needle);
    });
  }, [sessions, q, range, now]);

  return (
    <Page className="space-y-4">
      {/* The page reads "Chats"; the rows themselves stay sessions (the stored unit). */}
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold text-foreground">Chats</h1>
        <Link to="/chat" className={buttonClass("primary", "ml-auto")}>
          New chat
        </Link>
      </div>
      <ErrorBanner error={error} />
      {isLoading ? (
        <SessionsTableSkeleton />
      ) : !sessions || sessions.length === 0 ? (
        <Empty className="border py-10">
          <EmptyHeader>
            <EmptyMedia variant="icon">
              <MessagesSquare />
            </EmptyMedia>
            <EmptyTitle>No sessions yet</EmptyTitle>
            <EmptyDescription>
              Start a{" "}
              <Link to="/chat" className={linkClass}>
                new chat →
              </Link>{" "}
              or bench a model from Registries — every conversation lands here.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search id, preview, target…"
            trailing={<TimeRangeFilter value={range} onChange={setRange} />}
            extraActive={isActive(range)}
            total={sessions.length}
            shown={filtered.length}
            onClear={() => {
              setQ("");
              setRange(ALL_TIME);
            }}
          />
          {filtered.length === 0 ? (
            <Empty className="border py-10">
              <EmptyHeader>
                <EmptyTitle>No matching sessions</EmptyTitle>
                <EmptyDescription>No sessions match the current filters.</EmptyDescription>
              </EmptyHeader>
            </Empty>
          ) : (
            <div className="overflow-hidden rounded-xl border bg-card">
              <Table>
                <SessionsTableHeader />
                <TableBody>
                  {filtered.map((t) => {
                    const target = targetOf(t);
                    return (
                      <TableRow key={t.id}>
                        <TableCell>
                          <Link
                            to="/sessions/$sessionId"
                            params={{ sessionId: t.id }}
                            className={`mono ${linkClass}`}
                          >
                            {t.id}
                          </Link>
                        </TableCell>
                        <TableCell>{target ? <Badge tone="blue">{target}</Badge> : "—"}</TableCell>
                        <TableCell>{t.message_count}</TableCell>
                        <TableCell
                          className="max-w-md truncate text-muted-foreground"
                          title={t.preview ?? "—"}
                        >
                          {t.preview ?? "—"}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          <TimeAgo iso={t.last_activity} />
                        </TableCell>
                      </TableRow>
                    );
                  })}
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
