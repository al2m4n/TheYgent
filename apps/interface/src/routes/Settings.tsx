// App-level settings — the platform-configuration surface, reached from the rail's Configuration
// group. Distinct from the profile's USER settings (identity, theme — those stay in the profile
// modal): what lives here concerns how this installation behaves, not who is using it.
//
// One dirty-state form (the control-plane settings catalog) lives HERE and is shared by the
// Telemetry / RAG / MCP tabs, so staged edits survive tab switches; each tab saves only its own
// group. The Inference tab talks to the inference plane's own settings/diagnostics resources —
// a separate trust domain the browser reaches directly.

import { LocalCredentials } from "../components/LocalCredentials";
import { ImportExportTab } from "../components/settings/ImportExportTab";
import { InferenceTab } from "../components/settings/InferenceTab";
import { McpTab } from "../components/settings/McpTab";
import { OverviewTab } from "../components/settings/OverviewTab";
import { RagTab } from "../components/settings/RagTab";
import { SignInTab } from "../components/settings/SignInTab";
import { TelemetryTab } from "../components/settings/TelemetryTab";
import { UsersTab } from "../components/settings/UsersTab";
import { usePlatformSettingsForm } from "../components/settings/useSettingsForm";
import { Page } from "../components/ui";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "../components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "users", label: "Users" },
  { value: "signin", label: "Sign-in" },
  { value: "inference", label: "Inference" },
  { value: "telemetry", label: "Telemetry" },
  { value: "rag", label: "RAG" },
  { value: "mcp", label: "MCP" },
  { value: "credentials", label: "Credentials" },
  { value: "transfer", label: "Import / Export" },
] as const;

export function Settings() {
  const form = usePlatformSettingsForm();

  return (
    <Page className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">Settings</h1>
        <p className="text-xs text-muted-foreground">
          Platform configuration for this installation. Identity and theme live under your profile,
          bottom-left.
        </p>
      </div>

      <Tabs defaultValue="overview">
        <TabsList>
          {TABS.map((t) => (
            <TabsTrigger key={t.value} value={t.value}>
              {t.label}
            </TabsTrigger>
          ))}
        </TabsList>

        <TabsContent value="overview">
          <OverviewTab form={form} />
        </TabsContent>
        <TabsContent value="users">
          <UsersTab />
        </TabsContent>
        <TabsContent value="signin">
          <SignInTab form={form} />
        </TabsContent>
        <TabsContent value="inference">
          <InferenceTab />
        </TabsContent>
        <TabsContent value="telemetry">
          <TelemetryTab form={form} />
        </TabsContent>
        <TabsContent value="rag">
          <RagTab form={form} />
        </TabsContent>
        <TabsContent value="mcp">
          <McpTab form={form} />
        </TabsContent>
        <TabsContent value="credentials">
          <Card>
            <CardHeader className="border-b">
              <CardTitle>Local credentials — inference plane</CardTitle>
              <CardDescription>
                Named secrets for hosted-model registrations (referenced as secret://NAME). They
                live in the INFERENCE plane's machine-local store — on the machine that runs your
                models, write-only, never sent to theygent.
              </CardDescription>
            </CardHeader>
            <CardContent>
              <LocalCredentials />
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="transfer">
          <ImportExportTab />
        </TabsContent>
      </Tabs>
    </Page>
  );
}
