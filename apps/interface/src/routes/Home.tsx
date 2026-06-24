// Home: the open dialog (M15 §3, optional) — list saved agents from M11 and open one on the
// canvas, or start a new blank graph. Read-only over the registry; no new endpoints.

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { Badge, Button } from "../components/ui";
import { api } from "../lib/api";

export function Home() {
  const {
    data: agents,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.listAgents({ limit: 100 }),
  });

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Saved agents</h1>
          <p className="text-xs text-slate-500">Open one on the canvas, or start from scratch.</p>
        </div>
        <Link to="/editor" search={{ agent: undefined, version: undefined }}>
          <Button variant="primary">＋ New graph</Button>
        </Link>
      </div>

      {isLoading && <p className="text-sm text-slate-500">Loading…</p>}
      {error && (
        <p className="rounded-md border border-red-900 bg-red-950 px-3 py-2 text-sm text-red-200">
          Could not reach the control plane: {(error as Error).message}
        </p>
      )}

      {agents && agents.length === 0 && (
        <div className="rounded-lg border border-dashed border-slate-800 px-6 py-10 text-center text-sm text-slate-500">
          No saved agents yet. Create one on the canvas and save it.
        </div>
      )}

      <ul className="space-y-2">
        {agents?.map((a) => (
          <li key={a.id}>
            <Link
              to="/editor"
              search={{ agent: a.id, version: a.latest_version ?? undefined }}
              className="flex items-center justify-between rounded-lg border border-slate-800 bg-[#11161f] px-4 py-3 hover:border-slate-600"
            >
              <div className="min-w-0">
                <div className="truncate text-sm font-medium text-slate-100">{a.name}</div>
                <div className="mono truncate text-[11px] text-slate-500">{a.id}</div>
              </div>
              <div className="flex shrink-0 items-center gap-2">
                {a.latest_version && <Badge tone="blue">v{a.latest_version}</Badge>}
                <Badge>
                  {a.version_count} version{a.version_count === 1 ? "" : "s"}
                </Badge>
              </div>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}
