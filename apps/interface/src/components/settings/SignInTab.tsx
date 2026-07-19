// Settings → Sign-in: SSO/OAuth2 providers plus the auth-group platform settings. A provider
// is any OIDC issuer (Google, Okta, Keycloak, Entra, Authentik…) or a plain-OAuth2 service
// configured with explicit endpoints (e.g. GitHub). The client secret follows the connections
// discipline: write-only, encrypted server-side, never echoed back.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Pencil, Plus } from "lucide-react";
import { useState } from "react";
import { type AuthProviderInfo, type Role, api, controlPlaneUrl } from "../../lib/api";
import { notify } from "../../lib/notify";
import {
  Badge,
  Button,
  Card,
  ConfirmDialog,
  ErrorBanner,
  Field,
  Input,
  SectionHeading,
  Select,
  Spinner,
} from "../ui";
import { Switch } from "../ui/switch";
import { SaveBar } from "./SaveBar";
import { SettingRow } from "./SettingField";
import type { PlatformSettingsForm } from "./useSettingsForm";

const ROLES: Role[] = ["viewer", "editor", "admin"];

export function SignInTab({ form }: { form: PlatformSettingsForm }) {
  const qc = useQueryClient();
  const providers = useQuery({
    queryKey: ["auth", "providers"],
    queryFn: () => api.listAuthProviders(),
  });
  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["auth", "providers"] });
    qc.invalidateQueries({ queryKey: ["auth", "status"] }); // the login page's button list
  };

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionHeading>Single sign-on providers</SectionHeading>
        <p className="mt-1 text-xs text-muted-foreground">
          Each enabled provider becomes a button on the sign-in screen. Register this redirect URL
          with the provider:{" "}
          <span className="mono text-foreground">{controlPlaneUrl()}/auth/oidc/callback</span>
        </p>
        <div className="mt-3 space-y-2">
          {providers.isError && <ErrorBanner error={providers.error} />}
          {!providers.data && !providers.isError && <Spinner label="Loading providers…" />}
          {providers.data?.providers.map((p) => (
            <ProviderRow key={p.id} provider={p} onChanged={invalidate} />
          ))}
          {providers.data?.providers.length === 0 && (
            <p className="text-xs text-muted-foreground">No providers configured yet.</p>
          )}
        </div>
      </Card>

      <AddProviderCard onCreated={invalidate} />

      <Card className="p-4">
        <SectionHeading>Session policy</SectionHeading>
        <div className="mt-2">
          <AuthSettingRows form={form} />
        </div>
      </Card>
      <SaveBar form={form} group="auth" />
    </div>
  );
}

function AuthSettingRows({ form }: { form: PlatformSettingsForm }) {
  if (form.loadError && !form.data) return <ErrorBanner error={form.loadError} />;
  if (!form.data) return <Spinner label="Loading settings…" />;
  return (
    <>
      {["auth.session_ttl_hours", "auth.oidc_redirect_url"].map((key) => {
        const entry = form.entry(key);
        return entry ? <SettingRow key={key} entry={entry} form={form} /> : null;
      })}
    </>
  );
}

function ProviderRow({
  provider,
  onChanged,
}: {
  provider: AuthProviderInfo;
  onChanged: () => void;
}) {
  const [deleting, setDeleting] = useState(false);
  const [editing, setEditing] = useState(false);

  // The quick enable/disable toggle stays a one-click action; full config editing lives in the
  // inline panel below (Edit).
  const toggle = useMutation({
    mutationFn: (enabled: boolean) => api.updateAuthProvider(provider.id, { enabled }),
    onSuccess: onChanged,
    onError: (err) => notify.error(err instanceof Error ? err.message : String(err)),
  });
  const remove = useMutation({
    mutationFn: () => api.deleteAuthProvider(provider.id),
    onSuccess: () => {
      notify.success(`Removed ${provider.name}`);
      onChanged();
    },
    onError: (err) => notify.error(err instanceof Error ? err.message : String(err)),
  });

  const issuer =
    (provider.config.issuer_url as string | undefined) ??
    (provider.config.authorization_endpoint as string | undefined) ??
    "";

  return (
    <div className="rounded-md border border-border px-3 py-2.5">
      <div className="flex items-center gap-3">
        <KeyRound size={15} className="shrink-0 text-muted-foreground" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-sm text-foreground">
            {provider.name}
            <span className="mono text-[11px] text-muted-foreground">/{provider.slug}</span>
            {!provider.has_client_secret && <Badge tone="amber">no client secret</Badge>}
          </div>
          <div className="mono truncate text-[11px] text-muted-foreground">{issuer}</div>
        </div>
        <Button onClick={() => setEditing((e) => !e)}>
          <Pencil size={13} />
          {editing ? "Close" : "Edit"}
        </Button>
        <Button variant="danger" onClick={() => setDeleting(true)}>
          Remove
        </Button>
        <Switch
          checked={provider.enabled}
          onCheckedChange={(enabled) => toggle.mutate(enabled)}
          aria-label={`${provider.name} enabled`}
        />
      </div>
      {editing && (
        <EditProviderPanel
          provider={provider}
          onSaved={() => {
            setEditing(false);
            onChanged();
          }}
        />
      )}
      {deleting && (
        <ConfirmDialog
          title={`Remove ${provider.name}?`}
          message="Sign-ins through this provider stop working. Accounts it created remain."
          confirmLabel="Remove provider"
          onConfirm={() => {
            remove.mutate();
            setDeleting(false);
          }}
          onCancel={() => setDeleting(false)}
        />
      )}
    </div>
  );
}

// ── shared provider config form ──────────────────────────────────────────────
// The add card and the edit panel render the same field set through here, so the two never
// drift. `slug` is add-only (immutable after creation); the client secret is write-only (blank
// on edit = keep the stored one). The allowlist accepts a full email (exact-address allow) OR a
// bare domain (whole-org allow), comma-separated.

interface ProviderForm {
  name: string;
  slug: string;
  issuer: string;
  clientId: string;
  clientSecret: string;
  scopes: string;
  defaultRole: Role;
  autoProvision: boolean;
  allowlist: string;
  authorizeUrl: string;
  tokenUrl: string;
  userinfoUrl: string;
}

function emptyForm(): ProviderForm {
  return {
    name: "",
    slug: "",
    issuer: "",
    clientId: "",
    clientSecret: "",
    scopes: "",
    defaultRole: "viewer",
    autoProvision: true,
    allowlist: "",
    authorizeUrl: "",
    tokenUrl: "",
    userinfoUrl: "",
  };
}

function formFromProvider(p: AuthProviderInfo): ProviderForm {
  const c = p.config;
  const str = (k: string) => (typeof c[k] === "string" ? (c[k] as string) : "");
  return {
    name: p.name,
    slug: p.slug,
    issuer: str("issuer_url"),
    clientId: str("client_id"),
    clientSecret: "",
    scopes: str("scopes"),
    defaultRole: (c.default_role as Role | undefined) ?? "viewer",
    autoProvision: c.auto_provision !== false,
    allowlist: Array.isArray(c.allowed_domains) ? (c.allowed_domains as string[]).join(", ") : "",
    authorizeUrl: str("authorization_endpoint"),
    tokenUrl: str("token_endpoint"),
    userinfoUrl: str("userinfo_endpoint"),
  };
}

function buildConfig(f: ProviderForm): Record<string, unknown> {
  const config: Record<string, unknown> = {
    client_id: f.clientId.trim(),
    default_role: f.defaultRole,
    auto_provision: f.autoProvision,
  };
  if (f.issuer.trim()) config.issuer_url = f.issuer.trim();
  if (f.authorizeUrl.trim()) config.authorization_endpoint = f.authorizeUrl.trim();
  if (f.tokenUrl.trim()) config.token_endpoint = f.tokenUrl.trim();
  if (f.userinfoUrl.trim()) config.userinfo_endpoint = f.userinfoUrl.trim();
  if (f.scopes.trim()) config.scopes = f.scopes.trim();
  const list = f.allowlist
    .split(",")
    .map((d) => d.trim())
    .filter(Boolean);
  if (list.length > 0) config.allowed_domains = list;
  return config;
}

// The config is usable once client_id is set AND either an issuer (discovery) or all three
// explicit endpoints are present — mirrors the server's validate_provider_config.
function configComplete(f: ProviderForm): boolean {
  return (
    f.clientId.trim() !== "" &&
    (f.issuer.trim() !== "" ||
      (f.authorizeUrl.trim() !== "" && f.tokenUrl.trim() !== "" && f.userinfoUrl.trim() !== ""))
  );
}

function ProviderConfigFields({
  form,
  set,
  advanced,
  setAdvanced,
  includeSlug,
  secretLabel,
  secretPlaceholder,
}: {
  form: ProviderForm;
  set: (next: ProviderForm) => void;
  advanced: boolean;
  setAdvanced: (v: boolean) => void;
  includeSlug: boolean;
  secretLabel: string;
  secretPlaceholder?: string;
}) {
  const upd = (patch: Partial<ProviderForm>) => set({ ...form, ...patch });
  return (
    <>
      <div className="grid gap-2 sm:grid-cols-2">
        <Field label="Name (the button label)">
          <Input
            value={form.name}
            placeholder="Okta"
            onChange={(e) => upd({ name: e.target.value })}
          />
        </Field>
        {includeSlug && (
          <Field label="Slug">
            <Input
              value={form.slug}
              placeholder="okta"
              onChange={(e) => upd({ slug: e.target.value })}
            />
          </Field>
        )}
        <Field label="Issuer URL (OIDC discovery)">
          <Input
            value={form.issuer}
            placeholder="https://accounts.google.com"
            onChange={(e) => upd({ issuer: e.target.value })}
          />
        </Field>
        <Field label="Client ID">
          <Input value={form.clientId} onChange={(e) => upd({ clientId: e.target.value })} />
        </Field>
        <Field label={secretLabel}>
          <Input
            type="password"
            value={form.clientSecret}
            placeholder={secretPlaceholder}
            onChange={(e) => upd({ clientSecret: e.target.value })}
          />
        </Field>
        <Field label="Allowed emails or domains (comma-separated, blank = any)">
          <Input
            value={form.allowlist}
            placeholder="you@gmail.com, acme.com"
            onChange={(e) => upd({ allowlist: e.target.value })}
          />
        </Field>
        <Field label="Role for new sign-ins">
          <Select
            value={form.defaultRole}
            onChange={(e) => upd({ defaultRole: e.target.value as Role })}
          >
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </Select>
        </Field>
        <div className="flex items-end pb-1.5">
          <label className="flex items-center gap-2 text-xs text-foreground">
            <Switch
              checked={form.autoProvision}
              onCheckedChange={(v) => upd({ autoProvision: v })}
            />
            Create accounts on first sign-in
          </label>
        </div>
      </div>

      <button
        type="button"
        className="mt-3 text-xs text-muted-foreground underline-offset-2 hover:underline"
        onClick={() => setAdvanced(!advanced)}
      >
        {advanced
          ? "Hide advanced (endpoints & scopes)"
          : "Advanced: explicit endpoints (plain OAuth2) & scopes"}
      </button>
      {advanced && (
        <div className="mt-2 grid gap-2 sm:grid-cols-3">
          <Field label="Authorization endpoint">
            <Input
              value={form.authorizeUrl}
              onChange={(e) => upd({ authorizeUrl: e.target.value })}
            />
          </Field>
          <Field label="Token endpoint">
            <Input value={form.tokenUrl} onChange={(e) => upd({ tokenUrl: e.target.value })} />
          </Field>
          <Field label="Userinfo endpoint">
            <Input
              value={form.userinfoUrl}
              onChange={(e) => upd({ userinfoUrl: e.target.value })}
            />
          </Field>
          <Field label="Scopes (space-separated)">
            <Input
              value={form.scopes}
              placeholder="openid profile email"
              onChange={(e) => upd({ scopes: e.target.value })}
            />
          </Field>
        </div>
      )}
    </>
  );
}

function EditProviderPanel({
  provider,
  onSaved,
}: {
  provider: AuthProviderInfo;
  onSaved: () => void;
}) {
  const [form, setForm] = useState<ProviderForm>(() => formFromProvider(provider));
  // Open Advanced by default for a plain-OAuth2 provider (no issuer, explicit endpoints) so its
  // endpoints are visible for editing straight away.
  const [advanced, setAdvanced] = useState(
    !provider.config.issuer_url && Boolean(provider.config.authorization_endpoint),
  );
  const save = useMutation({
    mutationFn: () =>
      api.updateAuthProvider(provider.id, {
        name: form.name.trim(),
        config: buildConfig(form),
        // Blank leaves the stored secret untouched; a value rotates it in place.
        client_secret: form.clientSecret || undefined,
      }),
    onSuccess: () => {
      notify.success(`Saved ${form.name.trim()}`);
      onSaved();
    },
    onError: (err) => notify.error(err instanceof Error ? err.message : String(err)),
  });
  const canSave = form.name.trim() !== "" && configComplete(form) && !save.isPending;

  return (
    <div className="mt-3 border-t border-border pt-3">
      <p className="mb-2 text-xs text-muted-foreground">
        Editing <span className="mono">/{provider.slug}</span> — the slug is fixed (sign-in URLs
        depend on it). Leave the client secret blank to keep the current one.
      </p>
      <ProviderConfigFields
        form={form}
        set={setForm}
        advanced={advanced}
        setAdvanced={setAdvanced}
        includeSlug={false}
        secretLabel="Client secret (blank = keep current)"
        secretPlaceholder="leave blank to keep current"
      />
      <div className="mt-3 flex items-center gap-3">
        <Button variant="primary" disabled={!canSave} onClick={() => canSave && save.mutate()}>
          Save changes
        </Button>
        <ErrorBanner error={save.error} />
      </div>
    </div>
  );
}

function AddProviderCard({ onCreated }: { onCreated: () => void }) {
  const [form, setForm] = useState<ProviderForm>(emptyForm);
  const [advanced, setAdvanced] = useState(false);

  const create = useMutation({
    mutationFn: () =>
      api.createAuthProvider({
        name: form.name.trim(),
        slug: form.slug.trim(),
        config: buildConfig(form),
        client_secret: form.clientSecret || undefined,
      }),
    onSuccess: (p) => {
      notify.success(`Added ${p.name}`);
      setForm(emptyForm());
      setAdvanced(false);
      onCreated();
    },
  });

  const canCreate =
    form.name.trim() !== "" && form.slug.trim() !== "" && configComplete(form) && !create.isPending;

  return (
    <Card className="p-4">
      <SectionHeading>Add a provider</SectionHeading>
      <p className="mt-1 text-xs text-muted-foreground">
        For OIDC providers the issuer URL is enough — endpoints are discovered. For plain OAuth2
        (e.g. GitHub) open Advanced and set the three endpoints instead. To allow just yourself,
        list your full email in the allowlist; a bare domain lets everyone at that domain in.
      </p>
      <div className="mt-3">
        <ProviderConfigFields
          form={form}
          set={setForm}
          advanced={advanced}
          setAdvanced={setAdvanced}
          includeSlug
          secretLabel="Client secret (write-only)"
        />
      </div>
      <div className="mt-3 flex items-center gap-3">
        <Button
          variant="primary"
          disabled={!canCreate}
          onClick={() => canCreate && create.mutate()}
        >
          <Plus size={14} />
          Add provider
        </Button>
        <ErrorBanner error={create.error} />
      </div>
    </Card>
  );
}
