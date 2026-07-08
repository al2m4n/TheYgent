import { Link } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { FilterBar } from "../components/Filters";
import {
  Badge,
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
import { relativeTime } from "../lib/format";
import type { SessionSummary } from "../lib/runtypes";
import { useInView } from "../lib/useInView";
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

export function SessionsList() {
  const { data, isLoading, error, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useSessionsInfinite();
  const sessions = useMemo(() => flattenPages(data), [data]);
  const loadMoreRef = useInView(fetchNextPage, { enabled: hasNextPage && !isFetchingNextPage });
  const [q, setQ] = useState("");

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return sessions ?? [];
    return (sessions ?? []).filter((t) =>
      `${t.id} ${t.preview ?? ""} ${targetOf(t) ?? ""}`.toLowerCase().includes(needle),
    );
  }, [sessions, q]);

  return (
    <Page className="space-y-4">
      {/* The page reads "Chats"; the rows themselves stay sessions (the stored unit). */}
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold text-slate-100">Chats</h1>
        <Link to="/chat" className={buttonClass("primary", "ml-auto")}>
          New chat
        </Link>
      </div>
      <ErrorBanner error={error} />
      {isLoading ? (
        <Spinner />
      ) : !sessions || sessions.length === 0 ? (
        <Empty>
          No sessions yet. Start a{" "}
          <Link to="/chat" className={linkClass}>
            new chat →
          </Link>{" "}
          or bench a model from Registries — every conversation lands here.
        </Empty>
      ) : (
        <>
          <FilterBar
            search={q}
            onSearch={setQ}
            searchPlaceholder="Search id, preview, target…"
            total={sessions.length}
            shown={filtered.length}
            onClear={() => setQ("")}
          />
          {filtered.length === 0 ? (
            <Empty>No sessions match the current filters.</Empty>
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Session</Th>
                  <Th>Target</Th>
                  <Th>Messages</Th>
                  <Th>First message</Th>
                  <Th>Last activity</Th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((t) => {
                  const target = targetOf(t);
                  return (
                    <tr key={t.id} className="hover:bg-slate-800/30">
                      <Td className="whitespace-nowrap">
                        <Link
                          to="/sessions/$sessionId"
                          params={{ sessionId: t.id }}
                          className={`mono ${linkClass}`}
                        >
                          {t.id}
                        </Link>
                      </Td>
                      <Td className="whitespace-nowrap">
                        {target ? <Badge tone="blue">{target}</Badge> : "—"}
                      </Td>
                      <Td className="text-slate-300">{t.message_count}</Td>
                      <Td className="max-w-md truncate text-slate-400">{t.preview ?? "—"}</Td>
                      <Td className="whitespace-nowrap text-slate-400">
                        {relativeTime(t.last_activity)}
                      </Td>
                    </tr>
                  );
                })}
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
