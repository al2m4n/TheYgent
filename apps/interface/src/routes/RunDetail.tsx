import { Link, useParams } from "@tanstack/react-router";
import { type ReactNode, useEffect } from "react";
import { ResumePanel } from "../components/ResumePanel";
import { TimeAgo } from "../components/TimeAgo";
import { ErrorBanner, NoteBanner, Page, Spinner, StatusBadge, linkClass } from "../components/ui";
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from "../components/ui/breadcrumb";
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card";
import { RunWaterfall } from "../components/waterfall";
import { useLiveRun } from "../lib/live";
import { useRun, useSession } from "../queries";

function Detail({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mono break-all text-sm text-foreground">{value}</div>
    </div>
  );
}

export function RunDetail() {
  const { runId } = useParams({ from: "/runs/$runId" });
  const live = useLiveRun(runId);
  // `live: true` also covers a durable run paused at a human node ("waiting"): the row polls at a
  // relaxed cadence so a resume — from here or anywhere else — shows up without a manual reload.
  const { data: run, isLoading, error, refetch } = useRun(runId, { live: !live?.done });
  const sessionId = run?.session_id ?? null;
  const { data: session } = useSession(sessionId ?? "");

  // When a streamed run finishes, the canonical output may live ONLY in the persisted run row:
  // a graph whose terminal node isn't an `llm` (a `tool`/`router`/`mcp_tool` output, e.g. the
  // `echo` graph) emits NO `event: delta` frames, so `live.output` is empty. Polling stops at the
  // terminal status, so refetch once when the stream ends to pull `run.output`.
  useEffect(() => {
    if (live?.done) refetch();
  }, [live?.done, refetch]);

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
  if (!run)
    return (
      <Page>
        <ErrorBanner error="run not found" />
      </Page>
    );

  // When we streamed this run (a live entry exists), the live store holds the authoritative
  // state — including the terminal "completed"/"failed" from the final SSE frame. Prefer it
  // over the polled run row, which may be a step behind (or have stopped polling) at the
  // instant the stream ends. Only fall back to the persisted run when there is no live stream
  // (e.g. opening an older run, or after a page refresh).
  const status = live ? live.status : run.status;
  const isStreaming = !!live && !live.done;

  // Persisted output: the server persists `run.output` for EVERY run (in a session or not), so a
  // terminal run's answer is read straight from the row. Fall back to the session's assistant turns
  // only for older runs persisted before the output column existed (output === null).
  const persistedOutput =
    run.output ??
    session?.messages
      ?.filter((m) => m.run_id === run.id && m.role === "assistant")
      .map((m) => m.content)
      .join("\n");

  // While streaming, show the live accumulating tokens. Once terminal, the canonical answer is the
  // persisted run output (the output node's value) — which for a tool/router/mcp_tool-terminal
  // graph never crossed the SSE stream as deltas, so `live.output` is empty. Prefer persisted; fall
  // back to the streamed text only for older runs with no persisted output.
  const output = isStreaming ? live?.output : (persistedOutput ?? live?.output);

  // A reasoning model streams its thinking live (event: reasoning). Show it as progress so a long
  // thinking phase doesn't look frozen; it is never the answer.
  const reasoning = live?.reasoning;
  // The empty-output reason: a `completed` run can carry an honest note (e.g. the model hit its
  // token limit before answering). Treat error-on-completed as a note, not a failure.
  const isNote = !!run.error && status === "completed";

  return (
    <Page className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Breadcrumb>
          <BreadcrumbList>
            <BreadcrumbItem>
              <BreadcrumbLink asChild>
                <Link to="/runs">Runs</Link>
              </BreadcrumbLink>
            </BreadcrumbItem>
            <BreadcrumbSeparator />
            <BreadcrumbItem>
              <BreadcrumbPage className="mono break-all font-semibold">{run.id}</BreadcrumbPage>
            </BreadcrumbItem>
          </BreadcrumbList>
        </Breadcrumb>
        <StatusBadge status={status} />
      </div>

      <Card>
        <CardContent className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Detail label="Model" value={run.model || "—"} />
          <Detail
            label="Graph"
            value={run.graph_id ? `${run.graph_id} @ ${run.graph_version}` : "—"}
          />
          <Detail
            label="Session"
            value={
              sessionId ? (
                <Link to="/sessions/$sessionId" params={{ sessionId }} className={linkClass}>
                  {sessionId}
                </Link>
              ) : (
                "—"
              )
            }
          />
          <Detail label="Created" value={<TimeAgo iso={run.created_at} />} />
          <Detail label="Updated" value={<TimeAgo iso={run.updated_at} />} />
          {run.content_hash && <Detail label="Content hash" value={run.content_hash} />}
        </CardContent>
      </Card>

      {status === "waiting" && (
        <ResumePanel
          runId={run.id}
          awaitingNode={run.awaiting_node ?? null}
          onResumed={() => refetch()}
        />
      )}

      {run.error &&
        (isNote ? (
          <NoteBanner>
            <span className="font-semibold">Note:</span> {run.error}
          </NoteBanner>
        ) : (
          <ErrorBanner error={run.error} />
        ))}

      {reasoning && (
        <Card>
          <CardHeader className="border-b">
            <CardTitle className="flex items-center gap-2 text-sm">
              Thinking
              {isStreaming && (
                <span className="text-xs font-normal text-amber-600 dark:text-amber-400">
                  reasoning…
                </span>
              )}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="mono whitespace-pre-wrap break-words text-sm text-muted-foreground">
              {reasoning}
            </pre>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader className="border-b">
          <CardTitle className="flex items-center gap-2 text-sm">
            {isStreaming ? "Live output" : "Output"}
            {isStreaming && (
              <span className="text-xs font-normal text-amber-600 dark:text-amber-400">
                streaming…
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {output ? (
            <pre className="mono whitespace-pre-wrap break-words text-sm text-foreground">
              {output}
              {isStreaming && (
                <span className="animate-pulse text-amber-600 dark:text-amber-400">▌</span>
              )}
            </pre>
          ) : (
            <p className="text-sm text-muted-foreground">
              {isNote
                ? "The model returned no answer (see the note above)."
                : "No output recorded for this run."}
            </p>
          )}
        </CardContent>
      </Card>

      {/* The run waterfall — timing bars, gaps, worker attribution, click-through per-node I/O —
          reads the persisted span tree plus the live /trace/stream overlay (not the SSE event
          stream). */}
      <RunWaterfall runId={run.id} isLive={isStreaming} />
    </Page>
  );
}
