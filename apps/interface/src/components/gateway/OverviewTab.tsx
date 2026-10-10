// The AI Gateway page's Overview: is the gateway set up and healthy? It reads the control plane's
// latest gateway config (mode, version) and every instance's latest heartbeat. Nothing here is
// traffic content — the gateway never sends any to the control plane.

import { useQuery } from "@tanstack/react-query";
import { Activity, Inbox, Server, SlidersHorizontal } from "lucide-react";
import { type GatewayConfig, type GatewayInstance, api } from "../../lib/api";
import { toneOf } from "../../lib/categories";
import { TimeAgo } from "../TimeAgo";
import { Badge, ErrorBanner, NoteBanner } from "../ui";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "../ui/empty";
import { Item, ItemContent, ItemDescription, ItemGroup, ItemMedia, ItemTitle } from "../ui/item";
import { Skeleton } from "../ui/skeleton";
import { StatTile } from "./StatTile";

/** Heartbeats arrive every 30 s by default; polling a little faster keeps "online" honest. */
const REFRESH_MS = 15_000;

export function OverviewTab() {
  const config = useQuery({
    queryKey: ["gatewayConfig"],
    queryFn: () => api.getGatewayConfig(),
    refetchInterval: REFRESH_MS,
  });
  const instances = useQuery({
    queryKey: ["gatewayInstances"],
    queryFn: () => api.listGatewayInstances(),
    refetchInterval: REFRESH_MS,
  });

  if (config.isPending || instances.isPending) {
    return (
      <div className="space-y-4" aria-busy="true">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {["mode", "config", "online", "backlog"].map((k) => (
            <Skeleton key={k} className="h-20 rounded-lg" />
          ))}
        </div>
        <Skeleton className="h-24 rounded-lg" />
      </div>
    );
  }
  const error = config.error ?? instances.error;
  const list = instances.data ?? [];
  return (
    <div className="space-y-4">
      <ErrorBanner error={error} />
      {!error && config.data === null && (
        <NoteBanner>
          No gateway config has been saved yet. Gateways attached to this control plane forward
          traffic without recording it until an admin saves one.
        </NoteBanner>
      )}
      <Summary config={config.data ?? null} instances={list} />
      {!instances.error && <InstanceList instances={list} latest={config.data?.version ?? null} />}
    </div>
  );
}

function Summary({
  config,
  instances,
}: {
  config: GatewayConfig | null;
  instances: GatewayInstance[];
}) {
  const online = instances.filter((i) => i.online).length;
  const backlog = instances.reduce((sum, i) => sum + i.spool_backlog, 0);
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <StatTile
        icon={SlidersHorizontal}
        label="Mode"
        value={config ? config.mode : "passthrough"}
        hint={
          config === null
            ? "not configured"
            : config.mode === "observer"
              ? "records every exchange"
              : "forwards without recording"
        }
      />
      <StatTile
        icon={Activity}
        label="Config"
        value={config ? `v${config.version}` : "—"}
        hint={config ? <TimeAgo iso={config.updated_at} /> : "none saved yet"}
      />
      <StatTile
        icon={Server}
        label="Gateways online"
        value={`${online} / ${instances.length}`}
        hint={instances.length === 0 ? "none reported yet" : "by their latest heartbeat"}
      />
      <StatTile
        icon={Inbox}
        label="Waiting to export"
        value={backlog.toLocaleString()}
        hint="records in the gateways' local logs"
      />
    </div>
  );
}

function InstanceList({
  instances,
  latest,
}: {
  instances: GatewayInstance[];
  latest: number | null;
}) {
  if (instances.length === 0) {
    return (
      <Empty className="rounded-lg border border-dashed">
        <EmptyHeader>
          <EmptyMedia variant="icon">
            <Server />
          </EmptyMedia>
          <EmptyTitle>No gateway has reported in</EmptyTitle>
          <EmptyDescription>
            Start a gateway with <code>THEYGENT_CONTROL_PLANE_URL</code> and a service key; it
            appears here within one heartbeat.
          </EmptyDescription>
        </EmptyHeader>
      </Empty>
    );
  }
  return (
    <ItemGroup className="gap-2" aria-label="Gateway instances">
      {instances.map((instance) => (
        <InstanceRow key={instance.gateway_id} instance={instance} latest={latest} />
      ))}
    </ItemGroup>
  );
}

function InstanceRow({ instance, latest }: { instance: GatewayInstance; latest: number | null }) {
  const configLabel =
    instance.config_version === null ? "no config" : `config v${instance.config_version}`;
  return (
    <Item variant="outline" size="sm" data-testid={`gateway-${instance.gateway_id}`}>
      <ItemMedia>
        <span
          className={`inline-block size-2.5 rounded-full ${toneOf(instance.online ? "green" : "slate").dot}`}
          aria-hidden
        />
      </ItemMedia>
      <ItemContent>
        <ItemTitle className="flex flex-wrap items-center gap-2">
          <span className="font-mono">{instance.gateway_id}</span>
          <Badge tone={instance.online ? "green" : "slate"}>
            {instance.online ? "online" : "offline"}
          </Badge>
          <Badge tone={instance.mode === "observer" ? "blue" : "slate"}>{instance.mode}</Badge>
          <Badge tone={instance.config_current ? "slate" : "amber"}>
            {configLabel}
            {!instance.config_current && latest !== null && ` · latest v${latest}`}
          </Badge>
          {instance.upstream_reachable === false && <Badge tone="red">upstream unreachable</Badge>}
        </ItemTitle>
        <ItemDescription className="flex flex-wrap gap-x-4 gap-y-1">
          <span>version {instance.software_version}</span>
          {instance.key_set_version !== null && <span>keys v{instance.key_set_version}</span>}
          <span>{instance.spool_backlog.toLocaleString()} waiting to export</span>
          <span>
            last heartbeat <TimeAgo iso={instance.last_seen_at} />
          </span>
          <span>
            up since <TimeAgo iso={instance.started_at} />
          </span>
        </ItemDescription>
      </ItemContent>
    </Item>
  );
}
