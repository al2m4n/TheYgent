// Sign-in: local username/password plus one button per configured SSO provider. Providers come
// from the unauthenticated /auth/status probe (slug + label only); clicking one NAVIGATES to
// the control plane's OIDC start URL — the flow is a redirect chain, not a fetch.

import { useMutation } from "@tanstack/react-query";
import { KeyRound, LogIn } from "lucide-react";
import { useState } from "react";
import { Button, ErrorBanner, Field, Input } from "../components/ui";
import { Separator } from "../components/ui/separator";
import { oidcStartUrl } from "../lib/api";
import { useAuth } from "../lib/auth";
import { AuthShell } from "./AuthShell";

export function LoginScreen() {
  const { signIn, providers, oidcError } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const login = useMutation({ mutationFn: () => signIn(username, password) });
  const canSubmit = username.trim() !== "" && password !== "" && !login.isPending;

  return (
    <AuthShell footer="Local-first agents · your models, your machine">
      <div className="space-y-4">
        <div>
          <h1 className="text-base font-semibold text-foreground">Sign in</h1>
          <p className="text-xs text-muted-foreground">
            Welcome back — pick up where you left off.
          </p>
        </div>

        {oidcError && !login.error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive">
            {oidcError}
          </div>
        )}

        <div className="space-y-3">
          <Field label="Username">
            <Input
              value={username}
              autoFocus
              autoComplete="username"
              placeholder="your-username"
              onChange={(e) => setUsername(e.target.value)}
            />
          </Field>
          <Field label="Password">
            <Input
              type="password"
              value={password}
              autoComplete="current-password"
              placeholder="••••••••"
              onChange={(e) => setPassword(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && canSubmit) login.mutate();
              }}
            />
          </Field>
          <Button
            variant="primary"
            className="w-full"
            disabled={!canSubmit}
            onClick={() => canSubmit && login.mutate()}
          >
            <LogIn size={15} />
            {login.isPending ? "Signing in…" : "Sign in"}
          </Button>
          <ErrorBanner error={login.error} />
        </div>

        {providers.length > 0 && (
          <>
            <div className="flex items-center gap-3">
              <Separator className="flex-1" />
              <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
                or continue with
              </span>
              <Separator className="flex-1" />
            </div>
            <div className="space-y-2">
              {providers.map((p) => (
                <Button
                  key={p.slug}
                  className="w-full"
                  onClick={() => {
                    window.location.assign(oidcStartUrl(p.slug));
                  }}
                >
                  <KeyRound size={15} />
                  {p.name}
                </Button>
              ))}
            </div>
          </>
        )}
      </div>
    </AuthShell>
  );
}
