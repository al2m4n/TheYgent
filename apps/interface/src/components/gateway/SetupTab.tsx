// The AI Gateway page's Setup & keys tab: everything a developer needs to send Claude Code through
// the gateway — their gateway keys, how they are billed, and copy-ready setup per client.
//
// A key's raw token exists only in the response that minted it, so it is shown once, in the
// dialog that created it, alongside complete setups that already carry it. Everywhere else the
// setups show a placeholder.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Plus } from "lucide-react";
import { type ReactNode, useState } from "react";
import {
  type CredentialProfile,
  type GatewayKey,
  api,
  gatewayUrl,
  getEndpointOverrides,
  setEndpointOverride,
} from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { type Setup, setupFor } from "../../lib/gatewaySetup";
import { notify } from "../../lib/notify";
import { CopySnippet } from "../CopySnippet";
import { TimeAgo } from "../TimeAgo";
import { Badge, ConfirmDialog, ErrorBanner, Field, Input, Modal, NoteBanner } from "../ui";
import { Button } from "../ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../ui/card";
import { Item, ItemActions, ItemContent, ItemDescription, ItemGroup, ItemTitle } from "../ui/item";
import { Skeleton } from "../ui/skeleton";
import { Switch } from "../ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../ui/tabs";

const PROFILE_TEXT: Record<CredentialProfile, { badge: string; detail: string }> = {
  passthrough: {
    badge: "your own Claude login or API key",
    detail:
      "Your traffic is billed to the claude.ai login or Anthropic API key you already use; the " +
      "gateway key only tells the gateway who you are.",
  },
  org_key: {
    badge: "the organization's key",
    detail:
      "Your traffic is billed to the organization's Anthropic key, which the gateway adds; the " +
      "gateway key is the only credential you need.",
  },
};

export function SetupTab() {
  const me = useQuery({ queryKey: ["gatewayMe"], queryFn: () => api.getGatewayMe() });
  const [address, setAddress] = useState(() => gatewayUrl());
  const profile: CredentialProfile = me.data?.credential_profile ?? "passthrough";
  const setup = setupFor({ gatewayUrl: address, profile, key: null });

  return (
    <div className="space-y-4">
      <ErrorBanner error={me.error} />
      <Card>
        <CardHeader className="border-b">
          <CardTitle>How you are billed</CardTitle>
          <CardDescription>
            {me.isPending ? (
              <Skeleton className="h-4 w-64" />
            ) : (
              <>
                <Badge tone={profile === "org_key" ? "violet" : "blue"}>
                  {PROFILE_TEXT[profile].badge}
                </Badge>{" "}
                {PROFILE_TEXT[profile].detail} An admin sets this per developer.
              </>
            )}
          </CardDescription>
        </CardHeader>
        <CardContent className="pt-4">
          <GatewayAddress value={address} onApplied={setAddress} />
        </CardContent>
      </Card>
      <KeysCard address={address} profile={profile} />
      <SetupCard setup={setup} profile={profile} />
    </div>
  );
}

function GatewayAddress({
  value,
  onApplied,
}: {
  value: string;
  onApplied: (url: string) => void;
}) {
  const [draft, setDraft] = useState(getEndpointOverrides().gateway ?? "");
  const apply = (next: string | null) => {
    setEndpointOverride("gateway", next);
    setDraft(getEndpointOverrides().gateway ?? "");
    onApplied(gatewayUrl());
    notify.success(next ? "Gateway address saved for this browser" : "Gateway address reset");
  };
  return (
    <div className="flex items-end gap-2">
      <div className="flex-1">
        <Field label="Gateway address — where Claude Code reaches the gateway">
          <Input
            aria-label="Gateway address"
            value={draft}
            placeholder={value}
            onChange={(e) => setDraft(e.target.value)}
          />
        </Field>
      </div>
      <Button
        disabled={draft.trim() === (getEndpointOverrides().gateway ?? "")}
        onClick={() => apply(draft.trim() || null)}
      >
        Save
      </Button>
      <Button
        variant="outline"
        disabled={getEndpointOverrides().gateway == null}
        onClick={() => apply(null)}
      >
        Reset
      </Button>
    </div>
  );
}

// ── keys ─────────────────────────────────────────────────────────────────────

function KeysCard({ address, profile }: { address: string; profile: CredentialProfile }) {
  const qc = useQueryClient();
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  const [everyone, setEveryone] = useState(false);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<GatewayKey | null>(null);
  const keys = useQuery({
    queryKey: ["gatewayKeys", everyone],
    queryFn: () => api.listGatewayKeys(everyone),
  });
  const people = useQuery({
    queryKey: ["gatewayDevelopers"],
    queryFn: () => api.listGatewayDevelopers(),
    enabled: everyone,
  });
  const names = new Map((people.data ?? []).map((d) => [d.user_id, d.username]));
  const revoke = useMutation({
    mutationFn: (key: GatewayKey) => api.revokeGatewayKey(key.id),
    onSuccess: () => {
      notify.success("Gateway key revoked — gateways refuse it within one key sync");
      qc.invalidateQueries({ queryKey: ["gatewayKeys"] });
      qc.invalidateQueries({ queryKey: ["gatewayMe"] });
    },
    onError: (e) => notify.error(e instanceof Error ? e.message : String(e)),
  });

  return (
    <Card>
      <CardHeader className="border-b">
        <div className="flex items-start justify-between gap-3">
          <div>
            <CardTitle>{everyone ? "Everyone's gateway keys" : "Your gateway keys"}</CardTitle>
            <CardDescription>
              A gateway key identifies you to the gateway. It opens nothing else in TheYgent.
            </CardDescription>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            {isAdmin && (
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <Switch
                  checked={everyone}
                  onCheckedChange={setEveryone}
                  aria-label="Everyone's keys"
                />
                Everyone's
              </label>
            )}
            <Button onClick={() => setCreating(true)}>
              <Plus size={14} /> New key
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="pt-4">
        <ErrorBanner error={keys.error} />
        {keys.isPending ? (
          <Skeleton className="h-12 rounded-lg" />
        ) : (keys.data ?? []).length === 0 ? (
          <p className="text-xs text-muted-foreground">
            No keys yet. Create one, then copy a setup below.
          </p>
        ) : (
          <ItemGroup className="gap-2" aria-label="Gateway keys">
            {(keys.data ?? []).map((key) => (
              <Item key={key.id} variant="outline" size="sm">
                <ItemContent>
                  <ItemTitle className="flex flex-wrap items-center gap-2">
                    <KeyRound size={13} className="opacity-60" />
                    {key.name}
                    <span className="font-mono text-[11px] text-muted-foreground">
                      {key.token_prefix}…
                    </span>
                    {key.revoked_at && <Badge tone="slate">revoked</Badge>}
                    {everyone && (
                      <Badge tone="slate">{names.get(key.user_id) ?? key.user_id}</Badge>
                    )}
                  </ItemTitle>
                  <ItemDescription>
                    created <TimeAgo iso={key.created_at} />
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
                    <Button variant="outline" size="sm" onClick={() => setRevoking(key)}>
                      Revoke
                    </Button>
                  </ItemActions>
                )}
              </Item>
            ))}
          </ItemGroup>
        )}
      </CardContent>
      {creating && (
        <CreateKeyDialog
          address={address}
          profile={profile}
          onClose={() => {
            setCreating(false);
            qc.invalidateQueries({ queryKey: ["gatewayKeys"] });
            qc.invalidateQueries({ queryKey: ["gatewayMe"] });
          }}
        />
      )}
      {revoking && (
        <ConfirmDialog
          title="Revoke this gateway key?"
          message={
            <>
              Claude Code set up with <b>{revoking.name}</b> stops working through the gateway
              within one key sync. This cannot be undone.
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

function CreateKeyDialog({
  address,
  profile,
  onClose,
}: {
  address: string;
  profile: CredentialProfile;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const create = useMutation({ mutationFn: (keyName: string) => api.createGatewayKey(keyName) });
  const token = create.data?.token ?? null;
  return (
    <Modal title={token ? "Your new gateway key" : "New gateway key"} onClose={onClose}>
      {token ? (
        <div className="space-y-4">
          <NoteBanner>
            Copy this key now — it is shown only once. The setups below already contain it.
          </NoteBanner>
          <CopySnippet label="gateway key" code={token} />
          <SetupSnippets
            setup={setupFor({ gatewayUrl: address, profile, key: token })}
            profile={profile}
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
          <Field label="Name — which machine or setup it is for">
            <Input
              aria-label="Key name"
              value={name}
              placeholder="work laptop"
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

// ── setup per client ─────────────────────────────────────────────────────────

function SetupCard({ setup, profile }: { setup: Setup; profile: CredentialProfile }) {
  return (
    <Card>
      <CardHeader className="border-b">
        <CardTitle>Set up Claude Code</CardTitle>
        <CardDescription>
          The key appears here as a placeholder — paste yours, or create a key to get these filled
          in. The routing hints and tool search are always on: without tool search, Claude Code
          sends every tool's schema on every request through any gateway.
        </CardDescription>
      </CardHeader>
      <CardContent className="pt-4">
        <SetupSnippets setup={setup} profile={profile} />
      </CardContent>
    </Card>
  );
}

function SetupSnippets({ setup, profile }: { setup: Setup; profile: CredentialProfile }) {
  const { hasRole } = useAuth();
  return (
    <Tabs defaultValue="shell">
      <TabsList>
        <TabsTrigger value="shell">Terminal &amp; IDEs</TabsTrigger>
        <TabsTrigger value="settings">settings.json</TabsTrigger>
        <TabsTrigger value="desktop">Desktop app</TabsTrigger>
        {hasRole("admin") && <TabsTrigger value="managed">Managed settings</TabsTrigger>}
      </TabsList>
      <TabsContent value="shell" className="space-y-2">
        <Hint>Add to your shell profile; IDEs started from that shell pick it up too.</Hint>
        <CopySnippet label="shell setup" code={setup.shell} />
      </TabsContent>
      <TabsContent value="settings" className="space-y-2">
        <Hint>
          The same as the <code>env</code> block of <code>~/.claude/settings.json</code> (or a
          project's <code>.claude/settings.local.json</code>).
        </Hint>
        <CopySnippet label="settings.json" code={setup.settingsJson} />
      </TabsContent>
      <TabsContent value="desktop" className="space-y-2">
        {setup.desktop ? (
          <>
            <Hint>
              The desktop app ignores the settings above. In it, open Developer → Configure
              Third-Party Inference and enter:
            </Hint>
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-xs">
              {setup.desktop.map(({ label, value }) => (
                <div key={label} className="contents">
                  <dt className="text-muted-foreground">{label}</dt>
                  <dd className="font-mono break-all">{value}</dd>
                </div>
              ))}
            </dl>
          </>
        ) : (
          <NoteBanner>
            The desktop app reaches a gateway with an API key, never a claude.ai login, so it works
            through the gateway only when you are billed to the organization's key. Your profile is{" "}
            {profile}; ask an admin to change it if you need the desktop app.
          </NoteBanner>
        )}
      </TabsContent>
      {hasRole("admin") && (
        <TabsContent value="managed" className="space-y-2">
          <Hint>
            For every developer at once (managed settings). It carries no key: each developer adds
            their own, as in the other setups.
          </Hint>
          <CopySnippet label="managed settings" code={setup.managedJson} />
        </TabsContent>
      )}
    </Tabs>
  );
}

function Hint({ children }: { children: ReactNode }) {
  return <p className="text-xs text-muted-foreground">{children}</p>;
}
