// The full-screen chrome for the pre-app surfaces (first-run wizard, sign-in): brand mark over
// a centered card on the plain app background. No sidebar, no router — these render INSTEAD of
// the shell until a session exists.

import type { ReactNode } from "react";
import { Card } from "../components/ui/card";
import { useTheme } from "../lib/theme";

export function AuthShell({ children, footer }: { children: ReactNode; footer?: ReactNode }) {
  const { resolved } = useTheme();
  const mark = resolved === "dark" ? "/logo/theygent-logo-dark.svg" : "/logo/theygent-logo.svg";
  return (
    <div className="flex h-full min-h-0 flex-col items-center justify-center gap-6 overflow-y-auto bg-background px-4 py-10">
      <div className="flex items-center gap-2.5">
        <img src={mark} alt="" className="h-8 w-auto" />
        <span className="text-xl font-semibold tracking-tight text-foreground">TheYgent</span>
      </div>
      <Card className="w-full max-w-sm p-6">{children}</Card>
      <div className="text-xs text-muted-foreground">{footer}</div>
    </div>
  );
}
