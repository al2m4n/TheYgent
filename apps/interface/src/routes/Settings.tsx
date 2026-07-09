// App-level settings — the platform's own configuration, reached from the rail's Configuration
// group. Distinct from the profile's USER settings (identity, theme): what lives here concerns
// how this installation talks to its planes, not who is using it. Today that is the local
// endpoints + credentials seam; more platform configuration (retrieval stores, defaults,
// telemetry opt-ins) slots in as sections below.

import { LocalCredentials } from "../components/LocalCredentials";
import { Page } from "../components/ui";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";

export function Settings() {
  return (
    <Page className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">Settings</h1>
        <p className="text-xs text-muted-foreground">
          Platform configuration for this installation. Identity and theme live under your profile,
          bottom-left.
        </p>
      </div>

      <Card>
        <CardHeader className="border-b">
          <CardTitle>Endpoints &amp; credentials</CardTitle>
          <CardDescription>
            Where this installation reaches its planes, and the credential names it holds locally.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <LocalCredentials />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>More to come</CardTitle>
          <CardDescription>
            Retrieval (RAG) stores, run defaults, and telemetry preferences will be configured here.
          </CardDescription>
        </CardHeader>
      </Card>
    </Page>
  );
}
