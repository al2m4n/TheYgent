// The AI Gateway page's Settings (admins): the gateway config — mode, routing targets, contracted
// prices — saved as immutable versions on the control plane, with their history; and the
// organization's Anthropic key, which belongs to the gateway alone.
//
// The org key is written to the gateway's own admin surface, straight from the browser: it never
// transits the control plane, and this page never displays it — the gateway reports only whether
// it is set and where it came from.
//
// A config save names the version the edit started from; when someone saved another meanwhile,
// the control plane refuses (412) and the page says so instead of silently replacing their work.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus, ServerCog, Trash2 } from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import {
  ApiError,
  type GatewayConfig,
  type GatewayCredentials,
  type GatewayKey,
  api,
  gatewayAdmin,
  gatewayUrl,
} from "../../lib/api";
import { useAuth } from "../../lib/auth";
import {
  type FormErrors,
  RATE_FIELDS,
  type SettingsForm,
  blankRow,
  draftFromForm,
  draftOf,
  formOf,
} from "../../lib/gatewayConfig";
import { notify } from "../../lib/notify";
import { CopySnippet } from "../CopySnippet";
import { TimeAgo } from "../TimeAgo";
import {
  Badge,
  ConfirmDialog,
  ErrorBanner,
  Field,
  Input,
  Modal,
  NoteBanner,
  Table,
  Td,
  Th,
} from "../ui";
import { Button } from "../ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Item, ItemActions, ItemContent, ItemDescription, ItemTitle } from "../ui/item";
import { Skeleton } from "../ui/skeleton";
import { TableBody, TableHeader, TableRow } from "../ui/table";

const MODES: { value: SettingsForm["mode"]; label: string; detail: string }[] = [
  {
    value: "observer",
    label: "Observer",
    detail:
      "Records every exchange — its cost and what routing would have done — without changing it.",
  },
  {
    value: "passthrough",
    label: "Passthrough",
    detail: "Forwards traffic without recording anything.",
  },
];

const TARGETS: {
  field: "subagentTarget" | "mainTarget" | "localTarget";
  label: string;
  placeholder: string;
  hint: string;
}[] = [
  {
    field: "subagentTarget",
    label: "Subagent target",
    placeholder: "claude-sonnet-5-5",
    hint: "An Anthropic model a subagent could start on instead. A subagent's cache starts empty either way, so the switch costs nothing extra.",
  },
  {
    field: "mainTarget",
    label: "Main conversation target",
    placeholder: "none",
    hint: "Considered only where the main conversation's cache is cold: at its start, after compaction, or once the cache has expired.",
  },
  {
    field: "localTarget",
    label: "Local target",
    placeholder: "none",
    hint: "A logical model id on the inference plane, for side requests the Catalog allows to run locally.",
  },
];

const HISTORY_SHOWN = 10;

export function SettingsTab() {
  return (
    <div className="space-y-4">
      {/* The history grows with every Catalog review, so it comes last. */}
      <ConfigEditor
        beforeHistory={
          <>
            <OrgKeyCard />
            <ServiceKeysCard />
          </>
        }
      />
    </div>
  );
}

// ── the config ───────────────────────────────────────────────────────────────

function ConfigEditor({ beforeHistory }: { beforeHistory: ReactNode }) {
  const qc = useQueryClient();
  const config = useQuery({ queryKey: ["gatewayConfig"], queryFn: () => api.getGatewayConfig() });
  const latest = config.data;
  // The version the edit started from (null: none saved yet), and the form over it.
  const [base, setBase] = useState<GatewayConfig | null | undefined>(undefined);
  const [form, setForm] = useState<SettingsForm | null>(null);
  const [loaded, setLoaded] = useState<number | null>(null);
  const [showErrors, setShowErrors] = useState(false);
  const editor = useRef<HTMLDivElement | null>(null);
  const pristine = form !== null && base !== undefined && sameForm(form, formOf(draftOf(base)));

  // Follow the latest version while nothing is edited; an edit in progress stays put.
  useEffect(() => {
    if (latest === undefined) return;
    const moved = base === undefined || (latest?.version ?? null) !== (base?.version ?? null);
    if (form === null || (moved && pristine)) {
      setBase(latest);
      setForm(formOf(draftOf(latest)));
      setLoaded(null);
    }
  }, [latest, base, form, pristine]);

  const stale =
    latest !== undefined &&
    base !== undefined &&
    (latest?.version ?? null) !== (base?.version ?? null);
  const save = useMutation({
    mutationFn: ({ form, expect }: { form: SettingsForm; expect: GatewayConfig | null }) => {
      // Fingerprint rules belong to the Catalog: carry the ones of the version being replaced.
      const built = draftFromForm(form, expect?.fingerprints ?? []);
      if (built.draft === null) throw new Error("the form has errors");
      return api.putGatewayConfig(built.draft, expect?.version);
    },
    onSuccess: (saved) => {
      qc.setQueryData(["gatewayConfig"], saved);
      setBase(saved);
      setForm(formOf(draftOf(saved)));
      setLoaded(null);
      setShowErrors(false);
      notify.success(`Saved gateway config v${saved.version}`);
    },
    onError: (e) => {
      if (e instanceof ApiError && e.code === "config_version_conflict") {
        notify.error("Someone saved a newer version while you were editing.");
      }
    },
    onSettled: () => {
      for (const key of ["gatewayConfig", "gatewayConfigVersions", "gatewayFingerprints"]) {
        qc.invalidateQueries({ queryKey: [key] });
      }
    },
  });

  if (config.error || form === null || base === undefined) {
    // The org key is the gateway's, so it stays reachable whatever the control plane says.
    return (
      <>
        {config.error ? (
          <ErrorBanner error={config.error} />
        ) : (
          <Skeleton className="h-64 rounded-lg" />
        )}
        {beforeHistory}
      </>
    );
  }
  const built = draftFromForm(form, []);
  const errors: FormErrors = showErrors && built.errors ? built.errors : {};
  const update = (patch: Partial<SettingsForm>) => setForm({ ...form, ...patch });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (built.errors) {
      setShowErrors(true);
      return;
    }
    // Saving over a version saved meanwhile is a decision the banner below asked for.
    save.mutate({ form, expect: stale ? (latest ?? null) : base });
  };
  const conflict = save.error instanceof ApiError && save.error.code === "config_version_conflict";

  return (
    <>
      <Card ref={editor}>
        <CardHeader className="border-b">
          <CardTitle>Gateway config</CardTitle>
          <CardDescription>
            {base === null
              ? "No config saved yet: gateways attached to this control plane forward traffic without recording it."
              : `Version ${base.version}, saved `}
            {base !== null && <TimeAgo iso={base.updated_at} />}
            {base !== null &&
              ". Saving creates a new version; gateways pick it up at their next heartbeat (every 30 seconds by default)."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form className="space-y-5" onSubmit={submit} aria-label="Gateway config">
            {stale && !pristine && (
              <NoteBanner>
                Version {latest?.version} was saved since you started editing. Saving replaces its
                settings with yours, keeping its Catalog reviews; or{" "}
                <button
                  type="button"
                  className="underline"
                  onClick={() => {
                    setBase(latest ?? null);
                    setForm(formOf(draftOf(latest ?? null)));
                    setLoaded(null);
                    save.reset();
                  }}
                >
                  discard your changes
                </button>{" "}
                to see it.
              </NoteBanner>
            )}
            {loaded !== null && !stale && (
              <NoteBanner>
                Showing version {loaded}'s settings. Save to make them current, as a new version; or{" "}
                <button
                  type="button"
                  className="underline"
                  onClick={() => {
                    setForm(formOf(draftOf(base)));
                    setLoaded(null);
                    setShowErrors(false);
                  }}
                >
                  go back to version {base?.version ?? "none"}
                </button>
                .
              </NoteBanner>
            )}
            {save.error && !conflict && <ErrorBanner error={save.error} />}

            <fieldset className="min-w-0 space-y-2">
              <legend className="mb-2 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Mode
              </legend>
              <div className="grid gap-2 sm:grid-cols-2">
                {MODES.map((m) => (
                  <label
                    key={m.value}
                    className="flex cursor-pointer gap-3 rounded-lg border p-3 has-[:checked]:border-primary has-[:checked]:bg-primary/5"
                  >
                    <input
                      type="radio"
                      name="gateway-mode"
                      value={m.value}
                      checked={form.mode === m.value}
                      onChange={() => update({ mode: m.value })}
                      className="mt-0.5"
                    />
                    <span>
                      <span className="block text-sm font-medium text-foreground">{m.label}</span>
                      <span className="block text-xs text-muted-foreground">{m.detail}</span>
                    </span>
                  </label>
                ))}
              </div>
            </fieldset>

            <fieldset className="min-w-0 space-y-3">
              <legend className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                Routing targets
              </legend>
              <p className="text-xs text-muted-foreground">
                In observer mode nothing is routed: these decide what the Report and Sessions show
                routing would have saved. Leave a target empty to never consider it.
              </p>
              <div className="grid gap-3 md:grid-cols-2">
                {TARGETS.map((t) => (
                  <div key={t.field} className="space-y-1">
                    <Field label={t.label}>
                      <Input
                        value={form[t.field]}
                        placeholder={t.placeholder}
                        spellCheck={false}
                        aria-invalid={errors[t.field] ? true : undefined}
                        onChange={(e) => update({ [t.field]: e.target.value })}
                      />
                    </Field>
                    <FieldNote error={errors[t.field]} hint={t.hint} />
                  </div>
                ))}
                <div className="space-y-1">
                  <Field label="Warm switch margin">
                    <Input
                      value={form.margin}
                      inputMode="decimal"
                      aria-invalid={errors.margin ? true : undefined}
                      onChange={(e) => update({ margin: e.target.value })}
                    />
                  </Field>
                  <FieldNote
                    error={errors.margin}
                    hint="How many times its cache cost a switch in mid-conversation must save. At least 1."
                  />
                </div>
              </div>
            </fieldset>

            <PricingOverrides
              rows={form.rates}
              errors={errors}
              onChange={(rates) => update({ rates })}
            />

            <div className="flex flex-wrap items-center justify-end gap-2">
              {showErrors && built.errors && (
                <span className="text-xs text-destructive">Fix the fields marked above.</span>
              )}
              {/* Before any version exists, the defaults are worth saving as they are. */}
              <Button type="submit" disabled={save.isPending || (pristine && base !== null)}>
                {stale && !pristine ? `Save over version ${latest?.version}` : "Save"}
              </Button>
            </div>
          </form>
        </CardContent>
      </Card>
      {beforeHistory}
      <VersionHistory
        current={base?.version ?? null}
        onLoad={(version) => {
          setForm(formOf(draftOf(version)));
          setLoaded(version.version);
          setShowErrors(false);
          // Load sits below the editor; bring what changed into view. (jsdom has no scrolling.)
          editor.current?.scrollIntoView?.({ behavior: "smooth", block: "start" });
        }}
      />
    </>
  );
}

function sameForm(a: SettingsForm, b: SettingsForm): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

function FieldNote({ error, hint }: { error?: string; hint: string }) {
  return error ? (
    <p className="text-xs text-destructive">{error}</p>
  ) : (
    <p className="text-xs text-muted-foreground">{hint}</p>
  );
}

function PricingOverrides({
  rows,
  errors,
  onChange,
}: {
  rows: SettingsForm["rates"];
  errors: FormErrors;
  onChange: (rows: SettingsForm["rates"]) => void;
}) {
  const set = (i: number, patch: Partial<SettingsForm["rates"][number]>) =>
    onChange(rows.map((row, j) => (j === i ? { ...row, ...patch } : row)));
  // A fieldset is as wide as its content unless told otherwise (min-w-0): without it the table
  // would widen the card instead of scrolling inside its own frame.
  return (
    <fieldset className="min-w-0 space-y-2">
      <legend className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Contracted prices
      </legend>
      <p className="text-xs text-muted-foreground">
        USD per million tokens, for models whose contracted rates differ from list price. Costs and
        savings use these where set.
      </p>
      {rows.length > 0 && (
        <Table>
          <TableHeader>
            <TableRow>
              <Th>Model</Th>
              {RATE_FIELDS.map((f) => (
                <Th key={f.key}>{f.label}</Th>
              ))}
              <Th>
                <span className="sr-only">Remove</span>
              </Th>
            </TableRow>
          </TableHeader>
          <TableBody>
            {rows.map((row, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: rows have no identity until a model is typed
              <TableRow key={i}>
                <Td>
                  <Input
                    aria-label={`Model (row ${i + 1})`}
                    value={row.model}
                    placeholder="claude-opus-5-5"
                    spellCheck={false}
                    className="min-w-40 font-mono"
                    aria-invalid={errors[`rates.${i}.model`] ? true : undefined}
                    title={errors[`rates.${i}.model`]}
                    onChange={(e) => set(i, { model: e.target.value })}
                  />
                </Td>
                {RATE_FIELDS.map((f) => (
                  <Td key={f.key}>
                    <Input
                      aria-label={`${f.label} (row ${i + 1})`}
                      value={row.rates[f.key]}
                      inputMode="decimal"
                      className="w-24 tabular-nums"
                      aria-invalid={errors[`rates.${i}.${f.key}`] ? true : undefined}
                      title={errors[`rates.${i}.${f.key}`]}
                      onChange={(e) => set(i, { rates: { ...row.rates, [f.key]: e.target.value } })}
                    />
                  </Td>
                ))}
                <Td>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`Remove row ${i + 1}`}
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}
                  >
                    <Trash2 size={14} />
                  </Button>
                </Td>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}
      {rows.some((_, i) => Object.keys(errors).some((k) => k.startsWith(`rates.${i}.`))) && (
        <p className="text-xs text-destructive">
          Each row needs a model id (no spaces, one row per model) and all five prices, 0 or more.
        </p>
      )}
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => onChange([...rows, blankRow()])}
      >
        <Plus size={14} /> Add a model
      </Button>
    </fieldset>
  );
}

function VersionHistory({
  current,
  onLoad,
}: {
  current: number | null;
  onLoad: (config: GatewayConfig) => void;
}) {
  const { user } = useAuth();
  const versions = useQuery({
    queryKey: ["gatewayConfigVersions"],
    queryFn: () => api.listGatewayConfigVersions(),
  });
  const developers = useQuery({
    queryKey: ["gatewayDevelopers"],
    queryFn: () => api.listGatewayDevelopers(),
  });
  const names = new Map((developers.data ?? []).map((d) => [d.user_id, d.username]));
  const [loading, setLoading] = useState<number | null>(null);
  const load = async (version: number) => {
    setLoading(version);
    try {
      onLoad(await api.getGatewayConfigVersion(version));
    } catch (e) {
      notify.error(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(null);
    }
  };
  // Every Catalog review is a version too, so the history grows; the latest few lead.
  const [all, setAll] = useState(false);
  const every = versions.data ?? [];
  const list = all ? every : every.slice(0, HISTORY_SHOWN);
  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>History</CardTitle>
        <CardDescription>
          Every saved version, newest first. Load one to start an edit from it — saving it makes a
          new version.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ErrorBanner error={versions.error} />
        {versions.isPending ? (
          <Skeleton className="h-16 rounded-lg" />
        ) : list.length === 0 ? (
          <p className="text-xs text-muted-foreground">No versions saved yet.</p>
        ) : (
          <ul className="flex flex-col gap-2" aria-label="Config versions">
            {list.map((v) => (
              <Item key={v.version} variant="outline" size="sm" asChild>
                <li>
                  <ItemContent>
                    <ItemTitle className="flex items-center gap-2">
                      <span>Version {v.version}</span>
                      <Badge tone={v.mode === "observer" ? "blue" : "slate"}>{v.mode}</Badge>
                      {v.version === current && <Badge tone="green">current</Badge>}
                    </ItemTitle>
                    <ItemDescription>
                      saved <TimeAgo iso={v.created_at} />
                      {v.created_by &&
                        ` by ${v.created_by === user?.id ? "you" : (names.get(v.created_by) ?? v.created_by)}`}
                    </ItemDescription>
                  </ItemContent>
                  {v.version !== current && (
                    <ItemActions>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={loading !== null}
                        onClick={() => load(v.version)}
                        aria-label={`Load version ${v.version}`}
                      >
                        Load
                      </Button>
                    </ItemActions>
                  )}
                </li>
              </Item>
            ))}
          </ul>
        )}
        {!all && every.length > HISTORY_SHOWN && (
          <Button variant="outline" size="sm" className="mt-2" onClick={() => setAll(true)}>
            Show all {every.length} versions
          </Button>
        )}
      </CardContent>
    </Card>
  );
}

// ── the org key: the gateway's own credential ───────────────────────────────

function OrgKeyCard() {
  const qc = useQueryClient();
  const address = gatewayUrl();
  const credentials = useQuery({
    queryKey: ["gatewayCredentials", address],
    queryFn: () => gatewayAdmin.credentials(),
    retry: false,
  });
  // The key lives only in this input and the request that carries it: a plain call rather than a
  // mutation, whose cache would keep it as the mutation's variables.
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const apply = async (call: () => Promise<GatewayCredentials>, done: string) => {
    setBusy(true);
    setError(null);
    try {
      qc.setQueryData(["gatewayCredentials", address], await call());
      notify.success(done);
    } catch (e) {
      setError(e);
    } finally {
      setKey("");
      setBusy(false);
    }
  };
  const org = credentials.data?.orgApiKey;

  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle className="flex items-center gap-2">
          <KeyRound size={16} /> Organization's Anthropic key
        </CardTitle>
        <CardDescription>
          The key developers billed to the organization reach Anthropic with. It is written to the
          gateway at <span className="font-mono">{address}</span> directly — never through the
          control plane — and never shown again.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {credentials.isPending ? (
          <Skeleton className="h-16 rounded-lg" />
        ) : credentials.error ? (
          credentials.error instanceof ApiError ? (
            <ErrorBanner error={credentials.error} />
          ) : (
            <NoteBanner>
              Can't reach the gateway at <span className="font-mono">{address}</span>. The key is
              managed on the gateway itself, so this page needs its address: set it on the Setup
              &amp; keys tab.
            </NoteBanner>
          )
        ) : org ? (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              {org.set ? <Badge tone="green">set</Badge> : <Badge tone="amber">not set</Badge>}
              {org.readOnly === "env" && (
                <span className="text-xs text-muted-foreground">
                  by the deployment (THEYGENT_AI_GATEWAY_ORG_API_KEY); change it there.
                </span>
              )}
              {org.source === "stored" && org.updatedAt && (
                <span className="text-xs text-muted-foreground">
                  saved on the gateway <TimeAgo iso={org.updatedAt} />
                </span>
              )}
              {!org.set && org.writable && (
                <span className="text-xs text-muted-foreground">
                  Developers billed to the organization are refused until it is.
                </span>
              )}
            </div>
            {org.readOnly === "personal_mode" && (
              <p className="text-xs text-muted-foreground">
                This gateway runs in personal mode: each developer's own Claude login or API key
                reaches Anthropic, so there is no organization key.
              </p>
            )}
            {org.readOnly === "deployment_only" && (
              <p className="text-xs text-muted-foreground">
                This gateway takes the key from its deployment only: it can run as several replicas,
                and each would keep its own copy of a key saved here. Set{" "}
                <code>THEYGENT_AI_GATEWAY_ORG_API_KEY</code> there — in Kubernetes, in the{" "}
                <code>theygent-ai-gateway</code> Secret.
              </p>
            )}
            {org.writable && (
              <form
                className="flex flex-wrap items-end gap-2"
                onSubmit={(e) => {
                  e.preventDefault();
                  if (key.trim()) {
                    apply(() => gatewayAdmin.setOrgKey(key.trim()), "Saved the key on the gateway");
                  }
                }}
              >
                <div className="min-w-64 flex-1">
                  <Field label={org.set ? "Replace with" : "Anthropic API key"}>
                    <Input
                      type="password"
                      autoComplete="off"
                      spellCheck={false}
                      value={key}
                      onChange={(e) => setKey(e.target.value)}
                    />
                  </Field>
                </div>
                <Button type="submit" disabled={busy || !key.trim()}>
                  {org.set ? "Replace key" : "Save key"}
                </Button>
                {org.source === "stored" && (
                  <Button
                    type="button"
                    variant="outline"
                    disabled={busy}
                    onClick={() => setConfirmClear(true)}
                  >
                    Remove key
                  </Button>
                )}
              </form>
            )}
            <ErrorBanner error={error} />
          </>
        ) : null}
      </CardContent>
      {confirmClear && (
        <ConfirmDialog
          title="Remove the organization's key?"
          message="Developers billed to the organization are refused until a new key is set."
          confirmLabel="Remove"
          onCancel={() => setConfirmClear(false)}
          onConfirm={() => {
            setConfirmClear(false);
            apply(() => gatewayAdmin.clearOrgKey(), "Removed the key from the gateway");
          }}
        />
      )}
    </Card>
  );
}

// ── service keys: a gateway's credential towards this control plane ─────────

function ServiceKeysCard() {
  const qc = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<GatewayKey | null>(null);
  const keys = useQuery({
    queryKey: ["gatewayServiceKeys"],
    queryFn: () => api.listGatewayServiceKeys(),
  });
  const revoke = useMutation({
    mutationFn: (key: GatewayKey) => api.revokeGatewayServiceKey(key.id),
    onSuccess: () => notify.success("Service key revoked — gateways using it are refused at once"),
    onError: (e) => notify.error(e instanceof Error ? e.message : String(e)),
    onSettled: () => qc.invalidateQueries({ queryKey: ["gatewayServiceKeys"] }),
  });
  const list = keys.data ?? [];
  return (
    <Card>
      <CardHeader className="border-b">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2">
              <ServerCog size={16} /> Service keys
            </CardTitle>
            <CardDescription>
              What a gateway presents to this control plane to fetch developers' keys and its
              config, and to send its records. One per deployment; it opens nothing else.
            </CardDescription>
          </div>
          <Button className="shrink-0" onClick={() => setCreating(true)}>
            <Plus size={14} /> New service key
          </Button>
        </div>
      </CardHeader>
      <CardContent className="pt-4">
        <ErrorBanner error={keys.error} />
        {keys.isPending ? (
          <Skeleton className="h-12 rounded-lg" />
        ) : list.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            None yet. A gateway attached to this control plane needs one.
          </p>
        ) : (
          <ul className="flex flex-col gap-2" aria-label="Service keys">
            {list.map((key) => (
              <Item key={key.id} variant="outline" size="sm" asChild>
                <li>
                  <ItemContent>
                    <ItemTitle className="flex flex-wrap items-center gap-2">
                      {key.name}
                      <span className="font-mono text-[11px] text-muted-foreground">
                        {key.token_prefix}…
                      </span>
                      {key.revoked_at && <Badge tone="slate">revoked</Badge>}
                    </ItemTitle>
                    <ItemDescription>
                      created <TimeAgo iso={key.created_at} />
                      {key.last_used_at && (
                        <>
                          {" "}
                          · last used <TimeAgo iso={key.last_used_at} />
                        </>
                      )}
                      {key.revoked_at && (
                        <>
                          {" "}
                          · revoked <TimeAgo iso={key.revoked_at} />
                        </>
                      )}
                    </ItemDescription>
                  </ItemContent>
                  {!key.revoked_at && (
                    <ItemActions>
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setRevoking(key)}
                        aria-label={`Revoke ${key.name}`}
                      >
                        Revoke
                      </Button>
                    </ItemActions>
                  )}
                </li>
              </Item>
            ))}
          </ul>
        )}
      </CardContent>
      {creating && (
        <CreateServiceKeyDialog
          onClose={() => {
            setCreating(false);
            qc.invalidateQueries({ queryKey: ["gatewayServiceKeys"] });
          }}
        />
      )}
      {revoking && (
        <ConfirmDialog
          title="Revoke this service key?"
          message={
            <>
              Gateways using <b>{revoking.name}</b> lose their keys, config and record export until
              they get a new one. This cannot be undone.
            </>
          }
          confirmLabel="Revoke"
          onConfirm={() => {
            revoke.mutate(revoking);
            setRevoking(null);
          }}
          onCancel={() => setRevoking(null)}
        />
      )}
    </Card>
  );
}

function CreateServiceKeyDialog({ onClose }: { onClose: () => void }) {
  const [name, setName] = useState("");
  const create = useMutation({
    mutationFn: (keyName: string) => api.createGatewayServiceKey(keyName),
  });
  const token = create.data?.token ?? null;
  return (
    <Modal title={token ? "Your new service key" : "New service key"} onClose={onClose}>
      {token ? (
        <div className="space-y-4">
          <NoteBanner>Copy it now — it is shown only once.</NoteBanner>
          <CopySnippet label="service key" code={token} />
          <p className="text-xs text-muted-foreground">
            On a machine (<code>make up</code>) or with Docker Compose, in <code>.env</code>:
          </p>
          <CopySnippet label=".env" code={`THEYGENT_AI_GATEWAY_SERVICE_KEY=${token}`} />
          <p className="text-xs text-muted-foreground">In Kubernetes, as the gateway's Secret:</p>
          <CopySnippet
            label="kubectl"
            code={`kubectl -n theygent create secret generic theygent-ai-gateway \\\n  --from-literal=THEYGENT_AI_GATEWAY_SERVICE_KEY=${token}`}
          />
          <div className="flex justify-end">
            <Button onClick={onClose}>Done</Button>
          </div>
        </div>
      ) : (
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (name.trim()) create.mutate(name.trim());
          }}
        >
          <Field label="Name — which deployment it is for">
            <Input
              aria-label="Service key name"
              value={name}
              placeholder="team gateway"
              autoFocus
              onChange={(e) => setName(e.target.value)}
            />
          </Field>
          <ErrorBanner error={create.error} />
          <div className="flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" disabled={!name.trim() || create.isPending}>
              Create key
            </Button>
          </div>
        </form>
      )}
    </Modal>
  );
}
