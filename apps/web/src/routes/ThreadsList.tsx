import { Link } from "@tanstack/react-router";
import { Empty, ErrorBanner, Spinner, Table, Td, Th } from "../components/ui";
import { relativeTime, shortId } from "../lib/format";
import { useThreads } from "../queries";

export function ThreadsList() {
  const { data: threads, isLoading, error } = useThreads();

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold text-slate-100">Threads</h1>
      <ErrorBanner error={error} />
      {isLoading ? (
        <Spinner />
      ) : !threads || threads.length === 0 ? (
        <Empty>
          No conversational threads yet. Run something with a thread id from the composer.
        </Empty>
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
            {threads.map((t) => (
              <tr key={t.id} className="hover:bg-slate-800/30">
                <Td>
                  <Link
                    to="/threads/$threadId"
                    params={{ threadId: t.id }}
                    className="mono text-indigo-400 hover:text-indigo-300"
                  >
                    {shortId(t.id, 14)}
                  </Link>
                </Td>
                <Td className="text-slate-300">{t.message_count}</Td>
                <Td className="max-w-md truncate text-slate-400">{t.preview ?? "—"}</Td>
                <Td className="text-slate-400">{relativeTime(t.last_activity)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}
