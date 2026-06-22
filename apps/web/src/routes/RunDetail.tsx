import { Link, useParams } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { Card, ErrorBanner, Spinner, StatusBadge } from "../components/ui";
import { relativeTime } from "../lib/format";
import { useLiveRun } from "../lib/live";
import { useRun, useThread } from "../queries";

function Detail({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mono text-sm text-slate-200">{value}</div>
    </div>
  );
}

export function RunDetail() {
  const { runId } = useParams({ from: "/runs/$runId" });
  const live = useLiveRun(runId);
  const { data: run, isLoading, error } = useRun(runId, { live: !live?.done });
  const threadId = run?.thread_id ?? null;
  const { data: thread } = useThread(threadId ?? "");

  if (isLoading) return <Spinner />;
  if (error) return <ErrorBanner error={error} />;
  if (!run) return <ErrorBanner error="run not found" />;

  // When we streamed this run (a live entry exists), the live store holds the authoritative
  // state — including the terminal "completed"/"failed" from the final SSE frame. Prefer it
  // over the polled run row, which may be a step behind (or have stopped polling) at the
  // instant the stream ends. Only fall back to the persisted run when there is no live stream
  // (e.g. opening an older run, or after a page refresh).
  const status = live ? live.status : run.status;
  const isStreaming = !!live && !live.done;

  // Persisted output: M9 §2.2 persists `run.output` for EVERY run (threaded or not), so a terminal
  // run's answer is read straight from the row. Fall back to the thread's assistant turns only for
  // older runs persisted before the output column existed (output === null).
  const persistedOutput =
    run.output ??
    thread?.messages
      ?.filter((m) => m.run_id === run.id && m.role === "assistant")
      .map((m) => m.content)
      .join("\n");

  // A live run's accumulated text stays visible after it completes; otherwise use the persisted
  // output from the run row.
  const output = live ? live.output : persistedOutput;

  // A reasoning model streams its thinking live (event: reasoning). Show it as progress so a long
  // thinking phase doesn't look frozen; it is never the answer.
  const reasoning = live?.reasoning;
  // M9 §2.4 / the empty-output reason: a `completed` run can carry an honest note (e.g. the model
  // hit its token limit before answering). Treat error-on-completed as a note, not a failure.
  const isNote = !!run.error && status === "completed";

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3">
        <Link to="/" className="text-sm text-slate-400 hover:text-slate-200">
          ← Runs
        </Link>
        <h1 className="mono text-sm font-semibold text-slate-100">{run.id}</h1>
        <StatusBadge status={status} />
      </div>

      <Card className="grid grid-cols-2 gap-4 p-4 sm:grid-cols-3">
        <Detail label="Model" value={run.model || "—"} />
        <Detail
          label="Graph"
          value={run.graph_id ? `${run.graph_id} @ ${run.graph_version}` : "—"}
        />
        <Detail
          label="Thread"
          value={
            threadId ? (
              <Link
                to="/threads/$threadId"
                params={{ threadId }}
                className="text-indigo-400 hover:text-indigo-300"
              >
                {threadId}
              </Link>
            ) : (
              "—"
            )
          }
        />
        <Detail label="Created" value={relativeTime(run.created_at)} />
        <Detail label="Updated" value={relativeTime(run.updated_at)} />
        {run.content_hash && <Detail label="Content hash" value={run.content_hash} />}
      </Card>

      {run.error &&
        (isNote ? (
          <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300">
            <span className="font-semibold">Note:</span> {run.error}
          </div>
        ) : (
          <div className="rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
            <span className="font-semibold">Error:</span> {run.error}
          </div>
        ))}

      {reasoning && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-slate-300">
            Thinking
            {isStreaming && <span className="ml-2 text-xs text-amber-400">reasoning…</span>}
          </h2>
          <Card className="p-4">
            <pre className="mono whitespace-pre-wrap break-words text-sm text-slate-400">
              {reasoning}
            </pre>
          </Card>
        </section>
      )}

      <section className="space-y-2">
        <h2 className="text-sm font-semibold text-slate-300">
          {isStreaming ? "Live output" : "Output"}
          {isStreaming && <span className="ml-2 text-xs text-amber-400">streaming…</span>}
        </h2>
        <Card className="p-4">
          {output ? (
            <pre className="mono whitespace-pre-wrap break-words text-sm text-slate-100">
              {output}
              {isStreaming && <span className="animate-pulse text-amber-400">▌</span>}
            </pre>
          ) : (
            <p className="text-sm text-slate-500">
              {isNote
                ? "The model returned no answer (see the note above)."
                : "No output recorded for this run."}
            </p>
          )}
        </Card>
      </section>

      {live && live.timeline.length > 0 && (
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-slate-300">Event timeline</h2>
          <Card className="divide-y divide-slate-800/60">
            {live.timeline.map((entry, i) => (
              <div
                key={`${entry.ts}:${i}:${entry.event}`}
                className="flex items-start gap-3 px-3 py-1.5 text-xs"
              >
                <span
                  className={entry.event === "run" ? "mono text-indigo-400" : "mono text-slate-500"}
                >
                  {entry.event}
                </span>
                <span className="mono flex-1 break-words text-slate-300">{entry.detail}</span>
              </div>
            ))}
          </Card>
          <p className="text-xs text-slate-600">
            Per-node logs are emitted to the control-plane process logger; no API exposes them yet
            (recorded rough edge). This timeline reflects the SSE event stream.
          </p>
        </section>
      )}
    </div>
  );
}
