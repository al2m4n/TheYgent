import { Link, useParams } from "@tanstack/react-router";
import { Card, ErrorBanner, Spinner } from "../components/ui";
import { relativeTime } from "../lib/format";
import { useThread } from "../queries";

export function ThreadDetail() {
  const { threadId } = useParams({ from: "/threads/$threadId" });
  const { data: thread, isLoading, error } = useThread(threadId);

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBanner error={error} />;
  if (!thread) return <ErrorBanner error="thread not found" />;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/threads" className="text-sm text-slate-400 hover:text-slate-200">
          ← Threads
        </Link>
        <h1 className="mono text-sm font-semibold text-slate-100">{thread.id}</h1>
        <span className="text-xs text-slate-500">
          {thread.messages.length} messages · created {relativeTime(thread.created_at)}
        </span>
        <Link
          to="/compose"
          search={{ threadId: thread.id }}
          className="ml-auto rounded-md border border-indigo-500 bg-indigo-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-indigo-500"
        >
          New run in this thread
        </Link>
      </div>

      <div className="space-y-3">
        {thread.messages.map((m) => {
          const isUser = m.role === "user";
          return (
            <div key={m.id} className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
              <Card
                className={`max-w-2xl p-3 ${
                  isUser ? "border-indigo-500/30 bg-indigo-500/10" : "bg-slate-900/60"
                }`}
              >
                <div className="mb-1 flex items-center gap-2 text-xs text-slate-500">
                  <span className="font-semibold uppercase">{m.role}</span>
                  <Link
                    to="/runs/$runId"
                    params={{ runId: m.run_id }}
                    className="mono text-slate-500 hover:text-slate-300"
                  >
                    run {m.run_id.slice(0, 8)}
                  </Link>
                </div>
                <pre className="mono whitespace-pre-wrap break-words text-sm text-slate-100">
                  {m.content}
                </pre>
              </Card>
            </div>
          );
        })}
      </div>
    </div>
  );
}
