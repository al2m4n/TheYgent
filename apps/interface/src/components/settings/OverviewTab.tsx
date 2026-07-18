// Settings → Overview: where this browser reaches the two planes (with browser-local overrides),
// their live health, and the boot-structural configuration facts — read-only diagnostics with
// LOUD warnings (ephemeral secret key, unattended surfaces closed, durable off, topology).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Cpu, Server } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { type ReactNode, useState } from "react";
import {
  CONTROL_PLANE_URL,
  INFERENCE_URL,
  type Plane,
  type PlaneHealth,
  api,
  controlPlaneUrl,
  getEndpointOverrides,
  inferenceUrl,
  residentEngines,
  setEndpointOverride,
} from "../../lib/api";
import { notify } from "../../lib/notify";
import { cn } from "../../lib/utils";
import {
  Button,
  Card,
  ErrorBanner,
  Field,
  Input,
  NoteBanner,
  SectionHeading,
  Spinner,
} from "../ui";
import type { PlatformSettingsForm } from "./useSettingsForm";

export function OverviewTab({ form }: { form: PlatformSettingsForm }) {
  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-2">
        <ControlPlaneCard />
        <InferencePlaneCard />
      </div>
      <EndpointOverrides />
      <BootConfig form={form} />
    </div>
  );
}

// ── plane health ──────────────────────────────────────────────────────────────

function statusLook(health: PlaneHealth | undefined): { label: string; className: string } {
  if (!health) return { label: "checking…", className: "bg-muted text-muted-foreground" };
  if (!health.reachable)
    return {
      label: "offline",
      className: "bg-rose-500/15 text-rose-700 dark:text-rose-300",
    };
  if (health.status === "ready" || health.status === "ok")
    return {
      label: health.status,
      className: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-300",
    };
  return {
    label: health.status,
    className: "bg-amber-500/15 text-amber-700 dark:text-amber-300",
  };
}

function PlaneHealthCard({
  icon: Icon,
  title,
  url,
  overridden,
  health,
  children,
}: {
  icon: LucideIcon;
  title: string;
  url: string;
  overridden: boolean;
  health: PlaneHealth | undefined;
  children?: ReactNode;
}) {
  const look = statusLook(health);
  return (
    <Card className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <span className="flex size-8 items-center justify-center rounded-md bg-muted text-muted-foreground">
            <Icon size={16} />
          </span>
          <div className="min-w-0">
            <div className="text-sm font-medium text-foreground">{title}</div>
            <div className="mono truncate text-[11px] text-muted-foreground/70" title={url}>
              {url}
              {overridden && (
                <span className="ml-1 text-blue-600 dark:text-blue-400">(override)</span>
              )}
            </div>
          </div>
        </div>
        <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-medium", look.className)}>
          {look.label}
        </span>
      </div>
      <div className="mt-3 space-y-1 text-xs text-muted-foreground">
        {health?.reason && <p>{health.reason}</p>}
        {children}
      </div>
    </Card>
  );
}

function ControlPlaneCard() {
  const health = useQuery({ queryKey: ["health", "control"], queryFn: () => api.controlHealth() });
  // /readyz ships the registered servers as an array of {name, transport, connected} entries;
  // guard the shape so a proxy or older plane returning something else never crashes the card.
  const rawServers = health.data?.mcp?.servers;
  const mcpServers = (Array.isArray(rawServers) ? rawServers : [])
    .map((s) => s.name)
    .filter((n): n is string => Boolean(n));
  const overrides = getEndpointOverrides();
  return (
    <PlaneHealthCard
      icon={Server}
      title="Control plane"
      url={controlPlaneUrl()}
      overridden={overrides.control != null}
      health={health.data}
    >
      {health.data?.reachable && (
        <p>
          {mcpServers.length} registered MCP {mcpServers.length === 1 ? "server" : "servers"}
          {mcpServers.length > 0 && (
            <span className="mono"> — {mcpServers.slice(0, 4).join(", ")}</span>
          )}
        </p>
      )}
    </PlaneHealthCard>
  );
}

function InferencePlaneCard() {
  const health = useQuery({
    queryKey: ["health", "inference"],
    queryFn: () => api.inferenceHealth(),
  });
  const engines = useQuery({
    queryKey: ["engines"],
    queryFn: () => api.getEngines(),
    enabled: health.data?.reachable === true,
  });
  const resident = residentEngines(engines.data);
  const overrides = getEndpointOverrides();
  return (
    <PlaneHealthCard
      icon={Cpu}
      title="Inference plane"
      url={inferenceUrl()}
      overridden={overrides.inference != null}
      health={health.data}
    >
      {health.data?.reachable && engines.data && (
        <p>
          {resident.length}/{engines.data.maxResident} resident{" "}
          {resident.length === 1 ? "engine" : "engines"}
          {resident.length > 0 && (
            <span className="mono"> — {resident.map((e) => e.logicalId).join(", ")}</span>
          )}
        </p>
      )}
    </PlaneHealthCard>
  );
}

// ── browser-local endpoint overrides ─────────────────────────────────────────

const PLANES: { plane: Plane; label: string; fallback: string }[] = [
  { plane: "control", label: "Control plane URL", fallback: CONTROL_PLANE_URL },
  { plane: "inference", label: "Inference plane URL", fallback: INFERENCE_URL },
];

function EndpointOverrides() {
  const qc = useQueryClient();
  const [values, setValues] = useState<Record<Plane, string>>(() => {
    const o = getEndpointOverrides();
    return { control: o.control ?? "", inference: o.inference ?? "" };
  });

  const apply = async (plane: Plane, value: string | null) => {
    setEndpointOverride(plane, value);
    setValues((prev) => ({ ...prev, [plane]: getEndpointOverrides()[plane] ?? "" }));
    notify.success(value ? `${plane} plane override applied` : `${plane} plane override cleared`);
    // Everything cached was fetched from the OLD base — abort anything mid-retry against it,
    // then refetch the world against the new one.
    await qc.cancelQueries();
    await qc.invalidateQueries();
  };

  return (
    <Card className="p-4">
      <SectionHeading>Endpoint overrides</SectionHeading>
      <p className="mt-1 text-xs text-muted-foreground">
        This browser only — override where THIS browser reaches each plane (stored locally, never
        sent anywhere), so one build of the app can point at another install. Blank = the configured
        default.
      </p>
      <div className="mt-3 grid gap-3 lg:grid-cols-2">
        {PLANES.map(({ plane, label, fallback }) => (
          <div key={plane} className="flex items-end gap-2">
            <div className="flex-1">
              <Field label={label}>
                <Input
                  aria-label={label}
                  value={values[plane]}
                  placeholder={fallback}
                  onChange={(e) => setValues((prev) => ({ ...prev, [plane]: e.target.value }))}
                />
              </Field>
            </div>
            <Button
              variant="primary"
              disabled={values[plane].trim() === (getEndpointOverrides()[plane] ?? "")}
              onClick={() => apply(plane, values[plane].trim() || null)}
            >
              Apply
            </Button>
            <Button
              disabled={getEndpointOverrides()[plane] == null}
              onClick={() => apply(plane, null)}
            >
              Reset
            </Button>
          </div>
        ))}
      </div>
    </Card>
  );
}

// ── boot-structural configuration (read-only diagnostics) ────────────────────

// Boot values are arbitrary JSON facts: some are objects (connection facts like {host, database})
// and some are lists (allowed origins) — render them readably instead of coercing to String().
function fmtBootValue(v: unknown): string {
  if (v == null) return "—";
  if (Array.isArray(v)) return v.map((item) => String(item)).join(", ");
  if (typeof v === "object")
    return Object.entries(v as Record<string, unknown>)
      .map(([k, val]) => `${k}=${String(val)}`)
      .join(" · ");
  return String(v);
}

function BootConfig({ form }: { form: PlatformSettingsForm }) {
  // Gate on data, not isLoading: while the query retries an unreachable plane, isLoading flaps —
  // never render the (empty) success layout before anything has actually arrived.
  if (form.loadError && !form.data)
    return (
      <Card className="p-4">
        <SectionHeading>Boot configuration</SectionHeading>
        <div className="mt-2">
          <ErrorBanner error={form.loadError} />
        </div>
      </Card>
    );
  if (!form.data) return <Spinner label="Loading boot configuration…" />;

  const boot = form.data?.boot ?? [];
  const orphaned = form.data?.orphaned ?? [];
  const warnings = boot.filter((b) => b.warning);

  return (
    <Card className="p-4">
      <SectionHeading>Boot configuration</SectionHeading>
      <p className="mt-1 text-xs text-muted-foreground">
        Structural facts fixed at process start — set via environment / deployment, never from this
        page.
      </p>

      {warnings.length > 0 && (
        <div className="mt-3 space-y-2">
          {warnings.map((b) => (
            <NoteBanner key={b.key}>
              <span>
                <span className="mono font-medium">{b.key}</span>: {b.warning}
              </span>
            </NoteBanner>
          ))}
        </div>
      )}

      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-6 gap-y-2 text-xs">
        {boot.map((b) => (
          <div key={b.key} className="contents">
            <dt className="mono text-muted-foreground">{b.key}</dt>
            <dd>
              <span className="mono text-foreground">{fmtBootValue(b.value)}</span>
              <span className="ml-2 text-muted-foreground/70">{b.description}</span>
            </dd>
          </div>
        ))}
      </dl>

      {orphaned.length > 0 && (
        <div className="mt-4 space-y-2">
          <NoteBanner>
            <span>
              {orphaned.length} stored {orphaned.length === 1 ? "setting" : "settings"} no longer in
              the catalog (left over from an earlier version) — safe to remove.
            </span>
          </NoteBanner>
          <ul className="space-y-1">
            {orphaned.map((key) => (
              <li key={key} className="flex items-center gap-2 text-xs">
                <span className="mono text-muted-foreground">{key}</span>
                <Button
                  variant="ghost"
                  className="h-6 px-2 text-xs text-destructive"
                  disabled={form.saving}
                  onClick={() => form.removeOrphan(key)}
                >
                  Remove
                </Button>
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}
