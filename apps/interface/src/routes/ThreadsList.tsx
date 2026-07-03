import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { FilterBar } from "../components/Filters";
import { Empty, ErrorBanner, Page, Spinner, Table, Td, Th, linkClass } from "../components/ui";
import { relativeTime } from "../lib/format";
import { useInView } from "../lib/useInView";
import { flattenPages, useThreadsInfinite } from "../queries";

export function ThreadsList() {
  const { data, isLoading, error, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useThreadsInfinite();
  const threads = useMemo(() => flattenPages(data), [data]);
  const loadMoreRef = useInView(fetchNextPage, { enabled: hasNextPage && !isFetchingNextPage });
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return threads ?? [];
    return (threads ?? []).filter((t) =>
      `${t.id} ${t.preview ?? ""}`.toLowerCase().includes(needle),
    );
  }, [threads, q]);

  return (
    <Page className="space-y-4">
      <h1 className="text-lg font-semibold text-slate-100">Threads</h1>
      <ErrorBanner error={error} />
      {isLoading ? (
        <Spinner />
      ) : !threads || threads.length === 0 ? (
        <Empty>
          No conversational threads yet. Run something with a thread id from the{" "}
          <Link to="/compose" className={linkClass}>
            composer →
          </Link>
        </Empty>
      ) : (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search id, preview…"
            total={threads.length}
            shown={filtered.length}
            onClear={() => setQ("")}
          />
          {filtered.length === 0 ? (
            <Empty>No threads match the current filters.</Empty>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Thread</Th>
                  <Th>Messages</Th>
                  <Th>First message</Th>
                  <Th>Last activity</Th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((t) => (
                  <tr key={t.id} className="hover:bg-slate-800/30">
                    <Td className="whitespace-nowrap">
                      <Link
                        to="/threads/$threadId"
                        params={{ threadId: t.id }}
                        className={`mono ${linkClass}`}
                      >
                        {t.id}
                      </Link>
                    </Td>
                    <Td className="text-slate-300">{t.message_count}</Td>
                    <Td className="max-w-md truncate text-slate-400">{t.preview ?? "—"}</Td>
                    <Td className="whitespace-nowrap text-slate-400">
                      {relativeTime(t.last_activity)}
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
