import { Link, useParams } from "@tanstack/react-router";
import { Card, ErrorBanner, Page, Spinner, buttonClass } from "../components/ui";
import { relativeTime, shortId } from "../lib/format";
import { useThread } from "../queries";

export function ThreadDetail() {
  const { threadId } = useParams({ from: "/threads/$threadId" });
  const { data: thread, isLoading, error } = useThread(threadId);

  if (isLoading)
    return (
      <Page>
        <Spinner />
      </Page>
    );
  if (error)
    return (
      <Page>
        <ErrorBanner error={error} />
      </Page>
    );
  if (!thread)
    return (
      <Page>
        <ErrorBanner error="thread not found" />
      </Page>
    );

  return (
    <Page className="space-y-4">
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
          className={buttonClass("primary", "ml-auto shrink-0")}
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
                  isUser ? "border-blue-500/30 bg-blue-500/10" : "bg-slate-900/60"
                }`}
              >
                <div className="mb-1 flex items-center gap-2 text-xs text-slate-500">
                  <span className="font-semibold uppercase">{m.role}</span>
                  <Link
                    to="/runs/$runId"
                    params={{ runId: m.run_id }}
                    title={m.run_id}
                    className="mono text-slate-500 hover:text-slate-300"
                  >
                    run {shortId(m.run_id)}
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
    </Page>
  );
}
