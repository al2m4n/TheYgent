// The AI Gateway page's Report: what the traffic cost, what it would have cost had the routing
// policy's decisions been applied, and what is wrong with the setup. Every figure is the control
// plane's sum of the records the gateways sent (``GET /gateway/report``) — the same sums the
// gateway's own command-line report prints.
//
// Who sees what is the control plane's rule, mirrored here: everyone sees their own records in
// full; at organization scope, non-admins see totals and groupings that single out no one.

import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { CircleDollarSign, Coins, PiggyBank, Repeat2 } from "lucide-react";
import { type ReactNode, useState } from "react";
import {
  type GatewayDimension,
  type GatewayReport,
  type GatewaySetupWarningCode,
  type GatewayTotals,
  api,
} from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { type TimeRange, resolveBounds } from "../../lib/timeRange";
import { TimeRangeFilter } from "../TimeRangeFilter";
import { Badge, ErrorBanner, NoteBanner } from "../ui";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from "../ui/empty";
import { NativeSelect, NativeSelectOption } from "../ui/native-select";
import { Skeleton } from "../ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../ui/table";
import { StatTile } from "./StatTile";

const DAY_MS = 86_400_000;
const LAST_WEEK: TimeRange = { type: "relative", ms: 7 * DAY_MS };

const DIMENSIONS: { value: GatewayDimension; label: string; identifying?: boolean }[] = [
  { value: "class", label: "Request class" },
  { value: "client", label: "Client" },
  { value: "model", label: "Model requested" },
  { value: "served-model", label: "Model that served it" },
  { value: "developer", label: "Developer", identifying: true },
  { value: "session", label: "Session", identifying: true },
  { value: "decision", label: "Routing decision" },
  { value: "reason", label: "Reason" },
  { value: "shadow-model", label: "Model a shadow ran on" },
];

const WARNINGS: Record<GatewaySetupWarningCode, (values: string[]) => ReactNode> = {
  tool_search_off: () => (
    <>
      Claude Code sent every tool's schema upfront: tool search is off, which any custom base URL
      does by default and which multiplies input tokens. Set <code>ENABLE_TOOL_SEARCH=true</code>{" "}
      (the desktop app: tool search on) — the setups on the Setup &amp; keys tab do.
    </>
  ),
  hint_headers_off: () => (
    <>
      Claude Code is not labelling its requests, so none is a routing candidate. Set{" "}
      <code>CLAUDE_CODE_GATEWAY_HINT_HEADERS=1</code>.
    </>
  ),
  unrecognized_request_class: (values) => (
    <>
      Claude Code sent request classes this gateway does not know ({values.join(", ")}); they pass
      through unrouted. Update the gateway.
    </>
  ),
  unpriced_models: (values) => (
    <>
      No price is known for {values.join(", ")}; those calls are left out of the dollar figures. Add
      contracted rates to the gateway config.
    </>
  ),
};

/** Readable names for developer ids: usernames for admins (who may list developers), "you" for
 *  the reader's own id. Anything else stays the id. */
function useDeveloperNames(): (id: string | null) => string {
  const { user, hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const developers = useQuery({
    queryKey: ["gatewayDevelopers"],
    queryFn: () => api.listGatewayDevelopers(),
    enabled: isAdmin,
  });
  const names = new Map((developers.data ?? []).map((d) => [d.user_id, d.username]));
  return (id) => {
    if (id === null) return "this gateway";
    if (id === user?.id) return "you";
    return names.get(id) ?? id;
  };
}

function usd(value: number): string {
  if (value === 0) return "$0";
  return `$${value < 1 ? value.toFixed(4) : value.toFixed(2)}`;
}

function percent(part: number, whole: number): string {
  return whole > 0 ? `${((part / whole) * 100).toFixed(1)}%` : "—";
}

function tokens(t: GatewayTotals): number {
  return t.input_tokens + t.output_tokens + t.cache_read_tokens + t.cache_write_tokens;
}

/** The report's time bounds for a range picked now. "All time" starts at the epoch. */
function bounds(range: TimeRange): { since: string; until?: string } {
  const { fromMs, toMs } = resolveBounds(range, Date.now());
  return {
    since: new Date(fromMs ?? 0).toISOString(),
    ...(toMs != null ? { until: new Date(toMs).toISOString() } : {}),
  };
}

export function ReportTab() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [range, setRange] = useState<TimeRange>(LAST_WEEK);
  // Unset until chosen: admins start on everyone, others on their own records. The role can still
  // be loading on first render, so the default is read each render rather than frozen.
  const [chosenScope, setScope] = useState<"mine" | "org" | null>(null);
  const scope = chosenScope ?? (isAdmin ? "org" : "mine");
  const [dimension, setDimension] = useState<GatewayDimension>("class");
  const seesIndividuals = isAdmin || scope === "mine";
  const by: GatewayDimension = seesIndividuals
    ? dimension
    : DIMENSIONS.find((d) => d.value === dimension)?.identifying
      ? "class"
      : dimension;

  const report = useQuery({
    queryKey: ["gatewayReport", range, scope, by],
    // "shadow-model" also feeds the shadow runs' calibration, per model.
    queryFn: () =>
      api.getGatewayReport({
        ...bounds(range),
        by: [...new Set(["day", by, "shadow-model"] as const)],
        scope,
      }),
    refetchInterval: 60_000,
    // Changing the range, scope or grouping keeps the last report on screen until the next one
    // arrives, instead of flashing back to skeletons.
    placeholderData: keepPreviousData,
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <NativeSelect
          aria-label="Whose usage"
          value={scope}
          onChange={(e) => setScope(e.target.value as "mine" | "org")}
        >
          <NativeSelectOption value="mine">Your usage</NativeSelectOption>
          <NativeSelectOption value="org">
            {isAdmin ? "Everyone" : "Everyone (totals only)"}
          </NativeSelectOption>
        </NativeSelect>
        <TimeRangeFilter value={range} onChange={setRange} />
      </div>
      <ErrorBanner error={report.error} />
      {report.isPending ? (
        <div className="space-y-4" aria-busy="true">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {["spend", "routing", "saving", "calls"].map((k) => (
              <Skeleton key={k} className="h-20 rounded-lg" />
            ))}
          </div>
          <Skeleton className="h-40 rounded-lg" />
        </div>
      ) : report.data && report.data.total.calls === 0 ? (
        <Empty className="rounded-lg border border-dashed">
          <EmptyHeader>
            <EmptyTitle>No traffic recorded in this range</EmptyTitle>
            <EmptyDescription>
              Gateways record exchanges in observer mode and report them here within a minute. Pick
              a longer range, or check the Overview tab for the gateways' state.
            </EmptyDescription>
          </EmptyHeader>
        </Empty>
      ) : report.data ? (
        <ReportBody
          report={report.data}
          dimension={by}
          onDimension={setDimension}
          seesIndividuals={seesIndividuals}
        />
      ) : null}
    </div>
  );
}

function ReportBody({
  report,
  dimension,
  onDimension,
  seesIndividuals,
}: {
  report: GatewayReport;
  dimension: GatewayDimension;
  onDimension: (d: GatewayDimension) => void;
  seesIndividuals: boolean;
}) {
  const total = report.total;
  // Router mode served some calls elsewhere: spend is no longer what the traffic would cost as
  // requested, so that figure gets its own place.
  const routed = total.applied_calls > 0;
  return (
    <div className="space-y-4">
      {report.warnings.map((w) => (
        <NoteBanner key={w.code}>
          {WARNINGS[w.code](w.values)}{" "}
          <span className="opacity-75">({w.calls.toLocaleString()} calls)</span>
        </NoteBanner>
      ))}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          icon={CircleDollarSign}
          label="Spend"
          value={usd(total.cost_usd)}
          hint={
            total.unpriced_calls > 0
              ? `${total.unpriced_calls.toLocaleString()} calls unpriced`
              : routed
                ? `${usd(total.baseline_usd)} without routing`
                : "at the configured prices"
          }
        />
        <StatTile
          icon={Repeat2}
          label="With routing"
          value={usd(total.with_routing_usd)}
          hint={
            routed
              ? "as routed, plus the decisions not applied"
              : "had the policy's decisions been applied"
          }
        />
        <StatTile
          icon={PiggyBank}
          label="Saving"
          value={usd(total.saving_usd)}
          hint={
            routed
              ? `${percent(total.saving_usd, total.baseline_usd)} of the cost without routing`
              : `${percent(total.saving_usd, total.cost_usd)} of spend`
          }
        />
        <StatTile
          icon={Coins}
          label="Calls"
          value={total.calls.toLocaleString()}
          hint={`${routed ? `${total.applied_calls.toLocaleString()} routed · ` : ""}${(total.routed_calls - total.applied_calls).toLocaleString()} would route · ${total.routed_local_calls.toLocaleString()} would not reach the API`}
        />
      </div>
      <SavingsByDay rows={report.groups.day ?? []} />
      <Breakdown
        report={report}
        dimension={dimension}
        onDimension={onDimension}
        seesIndividuals={seesIndividuals}
      />
      {total.shadow_calls > 0 && <ShadowRuns report={report} />}
      {report.seats.length > 0 && <Seats report={report} />}
      <p className="text-[11px] text-muted-foreground">
        {total.input_tokens.toLocaleString()} input · {total.output_tokens.toLocaleString()} output
        · {total.cache_read_tokens.toLocaleString()} cache read ·{" "}
        {total.cache_write_tokens.toLocaleString()} cache write tokens. Prices:{" "}
        {report.pricing_versions.join(", ") || "none recorded"}. On a subscription, dollars are
        API-equivalent value, not a bill. Claude Code's own cost display assumes the model it
        requested; where the gateway served another, these figures are the true ones.
        {report.summarized_before && (
          <>
            {" "}
            Days before {report.summarized_before} are archived: their figures come from daily
            totals, without setup warnings, seat readings or the prices used.
          </>
        )}
      </p>
    </div>
  );
}

/** Each day's cost without routing (its spend, unless routes were applied), with the part routing
 *  keeps: the band between them is the saving. */
function SavingsByDay({ rows }: { rows: ({ key: string } & GatewayTotals)[] }) {
  const days = [...rows].sort((a, b) => a.key.localeCompare(b.key));
  const max = Math.max(...days.map((d) => Math.max(d.baseline_usd, d.with_routing_usd)), 0);
  const label = (d: GatewayTotals & { key: string }) =>
    `${d.key}: ${usd(d.cost_usd)} spent${d.applied_calls > 0 ? `, ${usd(d.baseline_usd)} without routing` : ""}, ${usd(d.with_routing_usd)} with routing`;
  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>Spend by day</CardTitle>
        <CardDescription>
          Full bar: what the day cost as Claude Code requested it. Filled part: with routing. The
          difference is the saving.
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-4">
        <ul className="flex h-32 items-end gap-1" aria-label="Spend by day">
          {days.map((d) => (
            <li
              key={d.key}
              className="relative flex h-full min-w-2 flex-1 flex-col justify-end"
              title={label(d)}
              aria-label={label(d)}
            >
              <div
                className="w-full rounded-t-sm bg-muted"
                style={{ height: `${max > 0 ? (d.baseline_usd / max) * 100 : 0}%` }}
              />
              <div
                className="absolute bottom-0 w-full rounded-t-sm bg-primary/70"
                style={{ height: `${max > 0 ? (d.with_routing_usd / max) * 100 : 0}%` }}
              />
            </li>
          ))}
        </ul>
        {days.length > 0 && (
          <div className="mt-1 flex justify-between text-[11px] text-muted-foreground">
            <span>{days[0].key}</span>
            <span>{days[days.length - 1].key}</span>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Breakdown({
  report,
  dimension,
  onDimension,
  seesIndividuals,
}: {
  report: GatewayReport;
  dimension: GatewayDimension;
  onDimension: (d: GatewayDimension) => void;
  seesIndividuals: boolean;
}) {
  const rows = report.groups[dimension] ?? [];
  const label = DIMENSIONS.find((d) => d.value === dimension)?.label ?? dimension;
  const nameOf = useDeveloperNames();
  const routed = report.total.applied_calls > 0;
  return (
    <Card>
      <CardHeader className="border-b">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>By {label.toLowerCase()}</CardTitle>
          <NativeSelect
            aria-label="Group by"
            value={dimension}
            onChange={(e) => onDimension(e.target.value as GatewayDimension)}
          >
            {DIMENSIONS.filter((d) => seesIndividuals || !d.identifying).map((d) => (
              <NativeSelectOption key={d.value} value={d.value}>
                {d.label}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </div>
        {dimension === "model" && report.served_differs && (
          <CardDescription>
            Grouped by the model Claude Code requested, as its own accounting files it; some calls
            were served by another — see "Model that served it".
          </CardDescription>
        )}
      </CardHeader>
      <CardContent className="pt-2">
        <Table aria-label={`By ${label.toLowerCase()}`}>
          <TableHeader>
            <TableRow>
              <TableHead>{label}</TableHead>
              <TableHead className="text-right">Calls</TableHead>
              <TableHead className="text-right">Tokens</TableHead>
              <TableHead className="text-right">Spend</TableHead>
              {routed && <TableHead className="text-right">Without routing</TableHead>}
              <TableHead className="text-right">With routing</TableHead>
              <TableHead className="text-right">Saving</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row) => (
              <TableRow key={row.key}>
                <TableCell className="font-mono text-xs">
                  {dimension === "developer" && row.key !== "(none)" ? nameOf(row.key) : row.key}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {row.calls.toLocaleString()}
                  {row.errors > 0 && (
                    <span className="ml-1 text-muted-foreground">({row.errors} failed)</span>
                  )}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {tokens(row).toLocaleString()}
                </TableCell>
                <TableCell className="text-right tabular-nums">{usd(row.cost_usd)}</TableCell>
                {routed && (
                  <TableCell className="text-right tabular-nums">{usd(row.baseline_usd)}</TableCell>
                )}
                <TableCell className="text-right tabular-nums">
                  {usd(row.with_routing_usd)}
                </TableCell>
                <TableCell className="text-right tabular-nums">
                  {row.saving_usd > 0 ? (
                    <Badge tone="green">{usd(row.saving_usd)}</Badge>
                  ) : (
                    usd(row.saving_usd)
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

/** What shadow runs measured, per model: how often its answer would have been used, and how much
 *  it wrote against the requested model — the counterfactuals assume the same. */
function ShadowRuns({ report }: { report: GatewayReport }) {
  const rows = (report.groups["shadow-model"] ?? []).filter((row) => row.shadow_calls > 0);
  const shown = rows.length > 0 ? rows : [{ ...report.total, key: "All models" }];
  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>Shadow runs</CardTitle>
        <CardDescription>
          After a request routing would have sent elsewhere — or, in a local trial, one Anthropic
          answered — a copy went to that model, unseen by Claude Code. The savings above assume a
          target writes as much as the model Claude Code asked for; here is what the models actually
          did. On agent turns, "same next action" says how often the target chose the tool, or the
          answer, that the real reply did: how far a model, a local one above all, can be trusted
          with agent work. Shadow runs cost {usd(report.total.shadow_cost_usd)}, outside the spend
          above.
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-2">
        <Table aria-label="Shadow runs">
          <TableHeader>
            <TableRow>
              <TableHead>Model</TableHead>
              <TableHead className="text-right">Shadows</TableHead>
              <TableHead className="text-right">Usable</TableHead>
              <TableHead className="text-right">Same next action</TableHead>
              <TableHead className="text-right">Output vs requested</TableHead>
              <TableHead className="text-right">Cost</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((row) => (
              <TableRow key={row.key}>
                <TableCell className="font-mono text-xs">{row.key}</TableCell>
                <TableCell className="text-right">{row.shadow_calls.toLocaleString()}</TableCell>
                <TableCell className="text-right">
                  {percent(row.shadow_answered_calls, row.shadow_calls)}
                </TableCell>
                <TableCell className="text-right">
                  {percent(row.shadow_agreed_calls, row.shadow_compared_calls)}
                </TableCell>
                <TableCell className="text-right">
                  {row.shadowed_output_tokens > 0
                    ? percent(row.shadow_output_tokens, row.shadowed_output_tokens)
                    : "—"}
                </TableCell>
                <TableCell className="text-right">{usd(row.shadow_cost_usd)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}

function Seats({ report }: { report: GatewayReport }) {
  const pct = (v: number | null) => (v === null ? "—" : `${Math.round(v * 100)}%`);
  const nameOf = useDeveloperNames();
  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>Subscription seats</CardTitle>
        <CardDescription>
          Each seat's plan windows at its latest reading, and how much of a window this range used,
          summed over every window it spans (+180% is 1.8 windows' worth). A seat's allowance is
          shared with everything else it does, claude.ai included.
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-2">
        <Table aria-label="Subscription seats">
          <TableHeader>
            <TableRow>
              <TableHead>Developer</TableHead>
              <TableHead className="text-right">5-hour window</TableHead>
              <TableHead className="text-right">7-day window</TableHead>
              <TableHead className="text-right">Used here (5h / 7d)</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {report.seats.map((seat) => (
              <TableRow key={seat.developer_id ?? "personal"}>
                <TableCell className="font-mono text-xs">{nameOf(seat.developer_id)}</TableCell>
                <TableCell className="text-right">{pct(seat.five_hour_utilization)}</TableCell>
                <TableCell className="text-right">{pct(seat.seven_day_utilization)}</TableCell>
                <TableCell className="text-right">
                  +{pct(seat.five_hour_used)} / +{pct(seat.seven_day_used)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
