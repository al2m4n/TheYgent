// App-level settings — the platform's own configuration, reached from the rail's Configuration
// group. Distinct from the profile's USER settings (identity, theme): what lives here concerns
// how this installation talks to its planes, not who is using it. Today that is the local
// endpoints + credentials seam; more platform configuration (retrieval stores, defaults,
// telemetry opt-ins) slots in as sections below.

import { LocalCredentials } from "../components/LocalCredentials";
import { Card, Page, SectionHeading } from "../components/ui";

export function Settings() {
  return (
    <Page className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-slate-100">Settings</h1>
        <p className="text-xs text-slate-500">
          Platform configuration for this installation. Identity and theme live under your profile,
          bottom-left.
        </p>
      </div>

      <Card className="space-y-3 p-4">
        <SectionHeading>Endpoints &amp; credentials</SectionHeading>
        <LocalCredentials />
      </Card>

      <Card className="p-4">
        <SectionHeading>More to come</SectionHeading>
        <p className="mt-2 text-sm text-slate-500">
          Retrieval (RAG) stores, run defaults, and telemetry preferences will be configured here.
        </p>
      </Card>
    </Page>
  );
}
