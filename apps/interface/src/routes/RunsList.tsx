import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { CategoryBadge, FilterBar } from "../components/Filters";
import {
  Empty,
  ErrorBanner,
  Page,
  Spinner,
  Table,
  Td,
  Th,
  buttonClass,
  linkClass,
} from "../components/ui";
import { countBy, statusTone, toggle } from "../lib/categories";
import { relativeTime, shortId } from "../lib/format";
import { useInView } from "../lib/useInView";
import { flattenPages, useRunsInfinite } from "../queries";

// The natural status reading order (lifecycle), so the chips don't reorder as counts change.
const STATUS_ORDER = ["created", "streaming", "waiting", "completed", "failed"];

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
          <h1 className="text-lg font-semibold text-slate-100">Runs</h1>
          <p className="text-xs text-slate-500">Newest first · auto-refreshing every 3s</p>
        </div>
        <Link to="/chat" className={buttonClass("primary", "shrink-0")}>
          New chat
        </Link>
      </div>

      <ErrorBanner error={error} />

      {isLoading ? (
        <Spinner />
      ) : !runs || runs.length === 0 ? (
        <Empty>
          No runs yet. Runs come from chats, agent invocations, and triggers —{" "}
          <Link to="/chat" className={linkClass}>
            start a chat →
          </Link>
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
            <Empty>No runs match the current filters.</Empty>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Run</Th>
                  <Th>Status</Th>
                  <Th>Model</Th>
                  <Th>Graph</Th>
                  <Th>Session</Th>
                  <Th>Created</Th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((run) => (
                  <tr key={run.id} className="hover:bg-slate-800/30">
                    {/* The run id is the row's identity — show it in full (no truncation); it keeps
                        to one line and the table scrolls horizontally on a narrow viewport. */}
                    <Td className="whitespace-nowrap">
                      <Link
                        to="/runs/$runId"
                        params={{ runId: run.id }}
                        className={`mono ${linkClass}`}
                      >
                        {run.id}
                      </Link>
                    </Td>
                    <Td>
                      {/* Clicking a status chip toggles it as a filter. */}
                      <CategoryBadge
                        tone={statusTone(run.status)}
                        active={statusSel.includes(run.status)}
                        onClick={() => setStatusSel((s) => toggle(s, run.status))}
                        title={`Filter by ${run.status}`}
                      >
                        {run.status}
                      </CategoryBadge>
                    </Td>
                    <Td className="mono text-slate-300">{run.model || "—"}</Td>
                    <Td className="mono whitespace-nowrap text-slate-400">
                      {run.graph_id ? `${shortId(run.graph_id, 10)}@${run.graph_version}` : "—"}
                    </Td>
                    <Td className="whitespace-nowrap">
                      {run.session_id ? (
                        <Link
                          to="/sessions/$sessionId"
                          params={{ sessionId: run.session_id }}
                          className="mono text-slate-400 hover:text-slate-200"
                        >
                          {shortId(run.session_id, 10)}
                        </Link>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </Td>
                    <Td className="whitespace-nowrap text-slate-400">
                      {relativeTime(run.created_at)}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
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
