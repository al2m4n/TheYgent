// The AI Gateway page: the gateway that sits between coding agents (Claude Code) and their models,
// observing — and later routing — their traffic. Every signed-in user reaches it (developers
// manage their own gateway keys and read their own usage here); tabs that change how the gateway
// behaves are the admin's.

import { OverviewTab } from "../components/gateway/OverviewTab";
import { ReportTab } from "../components/gateway/ReportTab";
import { SetupTab } from "../components/gateway/SetupTab";
import { Page } from "../components/ui";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "report", label: "Report" },
  { value: "setup", label: "Setup & keys" },
] as const;

export function Gateway() {
  return (
    <Page className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">AI Gateway</h1>
        <p className="text-xs text-muted-foreground">
          Sits between Claude Code and its models: records what every exchange cost and what routing
          would have saved. It sees prompts; the control plane receives only metadata.
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
          <OverviewTab />
        </TabsContent>
        <TabsContent value="report">
          <ReportTab />
        </TabsContent>
        <TabsContent value="setup">
          <SetupTab />
        </TabsContent>
      </Tabs>
    </Page>
  );
}
