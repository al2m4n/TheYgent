// The one switch between the three pre-app worlds and the real app. Renders INSTEAD of the
// router until a session exists, so no route (and no data fetch behind it) ever runs signed
// out. `unreachable` is its own honest state — a login form against a dead control plane
// would just be a confusing 401-less failure.

import type { ReactNode } from "react";
import { Button, Spinner } from "../components/ui";
import { controlPlaneUrl } from "../lib/api";
import { useAuth } from "../lib/auth";
import { AuthShell } from "./AuthShell";
import { LoginScreen } from "./LoginScreen";
import { SetupWizard } from "./SetupWizard";

export function AuthGate({ children }: { children: ReactNode }) {
  const { phase, refresh } = useAuth();

  if (phase === "loading") {
    return (
      <div className="flex h-full items-center justify-center bg-background">
        <Spinner />
      </div>
    );
  }
  if (phase === "unreachable") {
    return (
      <AuthShell footer="Start the stack with `make up`, then retry.">
        <div className="space-y-3 text-center">
          <h1 className="text-base font-semibold text-foreground">Control plane unreachable</h1>
          <p className="text-xs text-muted-foreground">
            Nothing answered at <span className="mono">{controlPlaneUrl()}</span>.
          </p>
          <Button variant="primary" className="w-full" onClick={refresh}>
            Retry
          </Button>
        </div>
      </AuthShell>
    );
  }
  if (phase === "setup") return <SetupWizard />;
  if (phase === "login") return <LoginScreen />;
  return <>{children}</>;
}
