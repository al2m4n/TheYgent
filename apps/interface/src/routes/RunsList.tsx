import { Link } from "@tanstack/react-router";
import { Empty, ErrorBanner, Spinner, StatusBadge, Table, Td, Th } from "../components/ui";
import { relativeTime, shortId } from "../lib/format";
import { useRuns } from "../queries";

export function RunsList() {
  const { data: runs, isLoading, error } = useRuns(50);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Runs</h1>
          <p className="text-xs text-slate-500">Most recent 50 · auto-refreshing every 3s</p>
        </div>
        <Link
          to="/compose"
          className="rounded-md border border-indigo-500 bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          New run
        </Link>
      </div>

      <ErrorBanner error={error} />

      {isLoading ? (
        <Spinner />
      ) : !runs || runs.length === 0 ? (
        <Empty>
          No runs yet.{" "}
          <Link to="/compose" className="text-indigo-400">
            Compose one →
          </Link>
        </Empty>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Run</Th>
              <Th>Status</Th>
              <Th>Model</Th>
              <Th>Graph</Th>
              <Th>Thread</Th>
              <Th>Created</Th>
            </tr>
          </thead>
          <tbody>
            {runs.map((run) => (
              <tr key={run.id} className="hover:bg-slate-800/30">
                <Td>
                  <Link
                    to="/runs/$runId"
                    params={{ runId: run.id }}
                    className="mono text-indigo-400 hover:text-indigo-300"
                  >
                    {shortId(run.id, 12)}
                  </Link>
                </Td>
                <Td>
                  <StatusBadge status={run.status} />
                </Td>
                <Td className="mono text-slate-300">{run.model || "—"}</Td>
                <Td className="mono text-slate-400">
                  {run.graph_id ? `${shortId(run.graph_id, 10)}@${run.graph_version}` : "—"}
                </Td>
                <Td>
                  {run.thread_id ? (
                    <Link
                      to="/threads/$threadId"
                      params={{ threadId: run.thread_id }}
                      className="mono text-slate-400 hover:text-slate-200"
                    >
                      {shortId(run.thread_id, 10)}
                    </Link>
                  ) : (
                    <span className="text-slate-600">—</span>
                  )}
                </Td>
                <Td className="text-slate-400">{relativeTime(run.created_at)}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}
