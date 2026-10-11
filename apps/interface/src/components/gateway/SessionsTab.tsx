// The AI Gateway page's Sessions: Claude Code sessions as the gateway saw them, and each one's
// timeline of requests with what the routing policy would have done and why. Decisions are never
// applied in observer mode; the timeline shows the reasoning so a policy can be trusted (or not)
// before it routes anything.
//
// Who sees what is the control plane's rule: everyone their own sessions; admins everyone's.

import { keepPreviousData, useInfiniteQuery } from "@tanstack/react-query";
import { ArrowLeft } from "lucide-react";
import { useState } from "react";
import {
  type GatewaySessionSummary,
  type GatewayShadowOutcome,
  type GatewayShadowRun,
  type GatewayTimelineEntry,
  type GatewayTotals,
  api,
} from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { explainReason } from "../../lib/gatewayReasons";
import { type TimeRange, resolveBounds } from "../../lib/timeRange";
import { TimeAgo } from "../TimeAgo";
import { TimeRangeFilter } from "../TimeRangeFilter";
import { Badge, ErrorBanner } from "../ui";
import { Button } from "../ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "../ui/empty";
import { Item, ItemActions, ItemContent, ItemDescription, ItemTitle } from "../ui/item";
import { NativeSelect, NativeSelectOption } from "../ui/native-select";
import { Skeleton } from "../ui/skeleton";

const PAGE = 50;
const TIMELINE_PAGE = 200;
const LAST_WEEK: TimeRange = { type: "relative", ms: 7 * 86_400_000 };
const CLASS_TONE: Record<string, string> = {
  main: "blue",
  subagent: "violet",
  auxiliary: "slate",
  compaction: "amber",
  workflow: "teal",
};

function usd(value: number | null): string {
  if (value === null) return "unpriced";
  if (value === 0) return "$0";
  return `$${value < 1 ? value.toFixed(4) : value.toFixed(2)}`;
}

function since(range: TimeRange): { since: string; until?: string } {
  const { fromMs, toMs } = resolveBounds(range, Date.now());
  return {
    since: new Date(fromMs ?? 0).toISOString(),
    ...(toMs != null ? { until: new Date(toMs).toISOString() } : {}),
  };
}

export function SessionsTab() {
  const [open, setOpen] = useState<string | null>(null);
  if (open !== null) return <SessionTimeline sessionId={open} onBack={() => setOpen(null)} />;
  return <SessionList onOpen={setOpen} />;
}

// ── the list ─────────────────────────────────────────────────────────────────

function SessionList({ onOpen }: { onOpen: (sessionId: string) => void }) {
  const { hasRole, user } = useAuth();
  const isAdmin = hasRole("admin");
  const [range, setRange] = useState<TimeRange>(LAST_WEEK);
  const [chosenScope, setScope] = useState<"mine" | "org" | null>(null);
  const scope = chosenScope ?? (isAdmin ? "org" : "mine");
  const sessions = useInfiniteQuery({
    queryKey: ["gatewaySessions", range, scope],
    queryFn: ({ pageParam }) =>
      api.listGatewaySessions({ ...since(range), scope, limit: PAGE, before: pageParam }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => (last.length === PAGE ? last[last.length - 1].last_at : undefined),
    placeholderData: keepPreviousData,
  });
  const rows = sessions.data?.pages.flat() ?? [];

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {isAdmin ? (
          <NativeSelect
            aria-label="Whose sessions"
            value={scope}
            onChange={(e) => setScope(e.target.value as "mine" | "org")}
          >
            <NativeSelectOption value="org">Everyone's sessions</NativeSelectOption>
            <NativeSelectOption value="mine">Your sessions</NativeSelectOption>
          </NativeSelect>
        ) : (
          <span className="text-xs text-muted-foreground">Your Claude Code sessions</span>
        )}
        <TimeRangeFilter value={range} onChange={setRange} />
      </div>
      <ErrorBanner error={sessions.error} />
      {sessions.isPending ? (
        <Skeleton className="h-24 rounded-lg" />
      ) : rows.length === 0 && !sessions.error ? (
        <Empty className="rounded-lg border border-dashed">
          <EmptyHeader>
            <EmptyTitle>No sessions in this range</EmptyTitle>
            <EmptyDescription>
              A session appears once Claude Code, set up from the Setup &amp; keys tab, sends
              traffic through a gateway in observer mode.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : (
        <ul className="flex flex-col gap-2" aria-label="Sessions">
          {rows.map((s) => (
            <SessionRow
              key={`${s.session_id}:${s.developer_id ?? ""}`}
              session={s}
              who={s.developer_id === user?.id ? "you" : s.developer_id}
              onOpen={() => onOpen(s.session_id)}
            />
          ))}
        </ul>
      )}
      {sessions.hasNextPage && (
        <div className="flex justify-center">
          <Button
            variant="outline"
            disabled={sessions.isFetchingNextPage}
            onClick={() => sessions.fetchNextPage()}
          >
            Older sessions
          </Button>
        </div>
      )}
    </div>
  );
}

function SessionRow({
  session,
  who,
  onOpen,
}: {
  session: GatewaySessionSummary;
  who: string | null;
  onOpen: () => void;
}) {
  const t = session.totals;
  return (
    <Item variant="outline" size="sm" asChild>
      <li>
        <ItemContent>
          <ItemTitle className="flex flex-wrap items-center gap-2">
            <span className="font-mono">{session.session_id.slice(0, 8)}</span>
            {who && <Badge tone="slate">{who}</Badge>}
            {session.client && <Badge tone="slate">{session.client}</Badge>}
            {t.saving_usd > 0 && (
              <Badge tone="green">
                routing {t.applied_calls > 0 ? "saved" : "would save"} {usd(t.saving_usd)}
              </Badge>
            )}
          </ItemTitle>
          <ItemDescription className="flex flex-wrap gap-x-4">
            <span>{t.calls.toLocaleString()} requests</span>
            <span>{usd(t.cost_usd)} spent</span>
            <span>
              last active <TimeAgo iso={session.last_at} />
            </span>
            <span>
              started <TimeAgo iso={session.first_at} />
            </span>
          </ItemDescription>
        </ItemContent>
        <ItemActions>
          <Button
            variant="outline"
            size="sm"
            onClick={onOpen}
            aria-label={`Open session ${session.session_id}`}
          >
            Timeline
          </Button>
        </ItemActions>
      </li>
    </Item>
  );
}

// ── one session's timeline ───────────────────────────────────────────────────

function SessionTimeline({ sessionId, onBack }: { sessionId: string; onBack: () => void }) {
  const detail = useInfiniteQuery({
    queryKey: ["gatewaySession", sessionId],
    queryFn: ({ pageParam }) =>
      api.getGatewaySession(sessionId, { after: pageParam, limit: TIMELINE_PAGE }),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) =>
      last.truncated ? last.requests[last.requests.length - 1]?.id : undefined,
  });
  const first = detail.data?.pages[0];
  const requests = detail.data?.pages.flatMap((p) => p.requests) ?? [];
  const start = requests.length > 0 ? Date.parse(requests[0].started_at) : 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" size="sm" onClick={onBack}>
          <ArrowLeft size={14} /> All sessions
        </Button>
        <h2 className="font-mono text-sm text-foreground">{sessionId}</h2>
      </div>
      <ErrorBanner error={detail.error} />
      {detail.isPending ? (
        <Skeleton className="h-40 rounded-lg" />
      ) : first ? (
        <>
          <SessionTotals totals={first.totals} />
          {first.archived_requests > 0 && (
            <p className="text-xs text-muted-foreground">
              {first.archived_requests.toLocaleString()} earlier{" "}
              {first.archived_requests === 1 ? "request is" : "requests are"} archived: counted in
              the totals above, no longer listed below.
            </p>
          )}
          <ol className="space-y-2" aria-label="Session timeline">
            {requests.map((r) => (
              <TimelineRow key={r.id} entry={r} offsetMs={Date.parse(r.started_at) - start} />
            ))}
          </ol>
          {detail.hasNextPage && (
            <div className="flex justify-center">
              <Button
                variant="outline"
                disabled={detail.isFetchingNextPage}
                onClick={() => detail.fetchNextPage()}
              >
                Later requests
              </Button>
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}

function SessionTotals({ totals }: { totals: GatewayTotals }) {
  const routed = totals.applied_calls;
  const wouldRoute = totals.routed_calls - routed;
  return (
    <p className="text-xs text-muted-foreground">
      {totals.calls.toLocaleString()} requests · {usd(totals.cost_usd)} spent ·{" "}
      {routed > 0 && <>{usd(totals.baseline_usd)} without routing · </>}
      {usd(totals.with_routing_usd)} with routing ·{" "}
      <span className={totals.saving_usd > 0 ? "text-foreground" : ""}>
        {usd(totals.saving_usd)} {routed > 0 ? "saved" : "saving"}
      </span>
      {routed > 0 && ` · ${routed.toLocaleString()} routed`}
      {wouldRoute > 0 && ` · ${wouldRoute.toLocaleString()} would route`}
    </p>
  );
}

function offset(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  return m >= 60
    ? `+${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`
    : `+${m}:${String(s).padStart(2, "0")}`;
}

function TimelineRow({ entry, offsetMs }: { entry: GatewayTimelineEntry; offsetMs: number }) {
  const u = entry.usage;
  const failed = entry.outcome !== "complete" || (entry.status !== null && entry.status >= 400);
  const served =
    entry.served_model && entry.served_model !== entry.requested_model
      ? ` → served by ${entry.served_model}`
      : "";
  const decision = entry.decision;
  return (
    <li className="rounded-lg border bg-card p-3" data-testid={`request-${entry.id}`}>
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="w-14 font-mono text-muted-foreground">{offset(offsetMs)}</span>
        <Badge tone={CLASS_TONE[entry.request_class] ?? "slate"}>{entry.request_class}</Badge>
        {entry.agent_type && <Badge tone="slate">{entry.agent_type}</Badge>}
        <span className="font-mono">
          {entry.requested_model ?? entry.endpoint}
          {served}
        </span>
        {failed && (
          <Badge tone="red">
            {entry.error_type ?? entry.outcome}
            {entry.status !== null && ` · ${entry.status}`}
          </Badge>
        )}
        <span className="ml-auto tabular-nums">{usd(entry.cost_usd)}</span>
      </div>
      <div className="mt-1 pl-16 text-[11px] text-muted-foreground">
        {u.input_tokens.toLocaleString()} in · {u.output_tokens.toLocaleString()} out ·{" "}
        {u.cache_read_tokens.toLocaleString()} cache read ·{" "}
        {(u.cache_write_5m_tokens + u.cache_write_1h_tokens).toLocaleString()} cache written
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 pl-16 text-xs">
        {decision.action === "route" && decision.target && decision.applied ? (
          <Badge tone="green">
            routed → {decision.target.model}
            {entry.baseline_usd !== null &&
              entry.cost_usd !== null &&
              ` (saved ${usd(entry.baseline_usd - entry.cost_usd)})`}
          </Badge>
        ) : decision.action === "route" && decision.target ? (
          <Badge tone="green">
            would route → {decision.target.model}
            {decision.counterfactual_usd !== null && ` (${usd(decision.counterfactual_usd)})`}
          </Badge>
        ) : (
          <Badge tone="slate">stays</Badge>
        )}
        {decision.reasons.map((code) => {
          const reason = explainReason(code);
          return (
            <span
              key={code}
              title={reason.detail}
              aria-label={`${reason.label}: ${reason.detail}`}
              className="rounded border border-border px-1.5 py-0.5 text-[11px] text-muted-foreground"
            >
              {reason.label}
            </span>
          );
        })}
      </div>
      {entry.shadow && <ShadowLine shadow={entry.shadow} wrote={u.output_tokens} />}
    </li>
  );
}

/** What a shadow run outcome means for the route it tested. */
const SHADOW_OUTCOME: Record<GatewayShadowOutcome, string> = {
  answered: "its answer would have been used",
  unusable: "its answer would not have been used",
  failed: "it did not answer",
  untranslatable: "the request could not be sent there",
};

function ShadowLine({ shadow, wrote }: { shadow: GatewayShadowRun; wrote: number }) {
  const output =
    shadow.outcome === "answered"
      ? ` · wrote ${shadow.usage.output_tokens.toLocaleString()} out where this wrote ${wrote.toLocaleString()}`
      : "";
  return (
    <div className="mt-1 pl-16 text-[11px] text-muted-foreground">
      Shadow run on {shadow.target.model}: {SHADOW_OUTCOME[shadow.outcome]}
      {output} · {usd(shadow.cost_usd)}
    </div>
  );
}
