// The profile (USER settings) modal, opened from the rail's footer: who you are, your theme,
// your password, your API keys, and the way out. App-level configuration stays on /settings —
// this modal is strictly per-account surface, which is also why every role gets all of it.

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Copy, KeyRound, LogOut, Monitor, Moon, Plus, Sun } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { useState } from "react";
import {
  Badge,
  Button,
  ConfirmDialog,
  ErrorBanner,
  Field,
  Input,
  Modal,
  SectionHeading,
} from "../components/ui";
import { Avatar, AvatarFallback, AvatarImage } from "../components/ui/avatar";
import { ToggleGroup, ToggleGroupItem } from "../components/ui/toggle-group";
import { type ApiKeyInfo, api } from "../lib/api";
import { useAuth } from "../lib/auth";
import { notify } from "../lib/notify";
import { type ThemePref, useTheme } from "../lib/theme";

const THEME_OPTIONS: { pref: ThemePref; icon: LucideIcon; label: string }[] = [
  { pref: "light", icon: Sun, label: "Light" },
  { pref: "dark", icon: Moon, label: "Dark" },
  { pref: "system", icon: Monitor, label: "System" },
];

function initialsOf(name: string): string {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  const letters = parts.slice(0, 2).map((p) => p[0]?.toUpperCase() ?? "");
  return letters.join("") || "?";
}

export function ProfileModal({ onClose }: { onClose: () => void }) {
  const { user, signOut, refresh } = useAuth();
  const { pref, setTheme } = useTheme();

  if (!user) return null;

  return (
    <Modal title="Your profile" width="max-w-lg" onClose={onClose}>
      <div className="max-h-[70vh] space-y-5 overflow-y-auto pr-1">
        <IdentitySection onSaved={refresh} />

        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <SectionHeading>Theme</SectionHeading>
            <ToggleGroup
              type="single"
              variant="outline"
              value={pref}
              onValueChange={(next) => {
                // Radix reports "" when the active item is re-clicked — a theme is never unset.
                if (next) setTheme(next as ThemePref);
              }}
              aria-label="Theme"
            >
              {THEME_OPTIONS.map(({ pref: p, icon: Icon, label }) => (
                <ToggleGroupItem
                  key={p}
                  value={p}
                  aria-label={`${label} theme`}
                  title={`${label} theme`}
                  className="data-[state=on]:bg-primary/10 data-[state=on]:text-primary"
                >
                  <Icon size={16} strokeWidth={2} />
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </div>
        </section>

        <PasswordSection />
        <ApiKeysSection />

        <div className="flex justify-end border-t border-border pt-3">
          <Button
            variant="ghost"
            onClick={() => {
              void signOut();
              onClose();
            }}
          >
            <LogOut size={15} />
            Sign out
          </Button>
        </div>
      </div>
    </Modal>
  );
}

function IdentitySection({ onSaved }: { onSaved: () => void }) {
  const { user } = useAuth();
  const [name, setName] = useState(user?.display_name ?? "");
  const save = useMutation({
    mutationFn: () => api.updateMe({ display_name: name.trim() }),
    onSuccess: () => {
      onSaved();
      notify.success("Display name updated");
    },
  });
  if (!user) return null;
  const dirty = name.trim() !== user.display_name && name.trim() !== "";
  return (
    <section className="space-y-3">
      <div className="flex items-center gap-3">
        <Avatar size="lg">
          {user.avatar_url && <AvatarImage src={user.avatar_url} alt="" />}
          <AvatarFallback className="bg-primary text-sm font-semibold text-primary-foreground">
            {initialsOf(user.display_name || user.username)}
          </AvatarFallback>
        </Avatar>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="truncate text-sm font-medium text-foreground">
              {user.display_name}
            </span>
            <Badge tone="blue">{user.role}</Badge>
          </div>
          <div className="truncate text-xs text-muted-foreground">
            @{user.username}
            {user.email ? ` · ${user.email}` : ""}
          </div>
        </div>
      </div>
      <div className="flex items-end gap-2">
        <div className="flex-1">
          <Field label="Display name">
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
        <Button variant="primary" disabled={!dirty || save.isPending} onClick={() => save.mutate()}>
          Save
        </Button>
      </div>
      <ErrorBanner error={save.error} />
    </section>
  );
}

function PasswordSection() {
  const { user, refresh } = useAuth();
  const [open, setOpen] = useState(false);
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const change = useMutation({
    mutationFn: () =>
      api.changeMyPassword({
        // An SSO-only account setting its FIRST password has no current one to present.
        current_password: user?.has_password ? current : undefined,
        new_password: next,
      }),
    onSuccess: () => {
      setOpen(false);
      setCurrent("");
      setNext("");
      refresh();
      notify.success("Password updated — other sessions were signed out");
    },
  });
  if (!user) return null;
  const canSave = next.length >= 8 && (!user.has_password || current !== "") && !change.isPending;
  return (
    <section className="space-y-2">
      <div className="flex items-center justify-between">
        <SectionHeading>Password</SectionHeading>
        <Button onClick={() => setOpen((o) => !o)}>
          {user.has_password ? "Change password" : "Set a password"}
        </Button>
      </div>
      {open && (
        <div className="space-y-2 rounded-md border border-border p-3">
          {user.has_password && (
            <Field label="Current password">
              <Input
                type="password"
                value={current}
                autoComplete="current-password"
                onChange={(e) => setCurrent(e.target.value)}
              />
            </Field>
          )}
          <Field label="New password (min 8 characters)">
            <Input
              type="password"
              value={next}
              autoComplete="new-password"
              onChange={(e) => setNext(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && canSave) change.mutate();
              }}
            />
          </Field>
          <div className="flex justify-end">
            <Button variant="primary" disabled={!canSave} onClick={() => change.mutate()}>
              Update password
            </Button>
          </div>
          <ErrorBanner error={change.error} />
        </div>
      )}
    </section>
  );
}

// Personal API keys — the programmatic path into the control plane (external frontends, CI,
// curl). The raw token is shown exactly once, right after minting.
function ApiKeysSection() {
  const qc = useQueryClient();
  const keys = useQuery({
    queryKey: ["auth", "api-keys"],
    queryFn: () => api.listApiKeys(),
  });
  const [name, setName] = useState("");
  const [freshToken, setFreshToken] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<ApiKeyInfo | null>(null);
  const [copied, setCopied] = useState(false);

  const mint = useMutation({
    mutationFn: () => api.createApiKey({ name: name.trim() }),
    onSuccess: (res) => {
      setName("");
      setFreshToken(res.token);
      setCopied(false);
      qc.invalidateQueries({ queryKey: ["auth", "api-keys"] });
    },
  });
  const revoke = useMutation({
    mutationFn: (keyId: string) => api.revokeApiKey(keyId),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["auth", "api-keys"] }),
  });

  const active = (keys.data?.api_keys ?? []).filter((k) => !k.revoked_at);
  const canMint = name.trim() !== "" && !mint.isPending;

  return (
    <section className="space-y-2">
      <SectionHeading>API keys</SectionHeading>
      <p className="text-xs text-muted-foreground">
        Bearer tokens for calling the control-plane API from your own code — they carry your role
        and open the unattended <span className="mono">/agents/&#123;id&#125;/invoke</span>{" "}
        endpoint.
      </p>

      {freshToken && (
        <div className="space-y-1.5 rounded-md border border-primary/40 bg-primary/5 p-3">
          <div className="text-xs font-medium text-foreground">
            Copy this key now — it won't be shown again.
          </div>
          <div className="flex items-center gap-2">
            <code className="mono min-w-0 flex-1 truncate rounded bg-muted px-2 py-1 text-xs">
              {freshToken}
            </code>
            <Button
              onClick={() => {
                void navigator.clipboard?.writeText(freshToken).then(() => setCopied(true));
              }}
              aria-label="Copy API key"
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </Button>
            <Button variant="ghost" onClick={() => setFreshToken(null)}>
              Done
            </Button>
          </div>
        </div>
      )}

      <ul className="space-y-1.5">
        {active.map((k) => (
          <li
            key={k.id}
            className="flex items-center gap-2 rounded-md border border-border px-3 py-2"
          >
            <KeyRound size={14} className="shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <div className="truncate text-xs font-medium text-foreground">{k.name}</div>
              <div className="mono truncate text-[11px] text-muted-foreground">
                {k.token_prefix}… · {k.role}
              </div>
            </div>
            <Button variant="ghost" onClick={() => setRevoking(k)}>
              Revoke
            </Button>
          </li>
        ))}
        {keys.isSuccess && active.length === 0 && !freshToken && (
          <li className="text-xs text-muted-foreground">No API keys yet.</li>
        )}
      </ul>

      <div className="flex items-end gap-2">
        <div className="flex-1">
          <Field label="New key name">
            <Input
              value={name}
              placeholder="e.g. my-frontend"
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && canMint) mint.mutate();
              }}
            />
          </Field>
        </div>
        <Button variant="primary" disabled={!canMint} onClick={() => mint.mutate()}>
          <Plus size={14} />
          Create
        </Button>
      </div>
      <ErrorBanner error={mint.error ?? revoke.error} />

      {revoking && (
        <ConfirmDialog
          title={`Revoke "${revoking.name}"?`}
          message="Anything calling the API with this key stops working immediately."
          confirmLabel="Revoke key"
          onConfirm={() => {
            revoke.mutate(revoking.id);
            setRevoking(null);
          }}
          onCancel={() => setRevoking(null)}
        />
      )}
    </section>
  );
}
