// The AI Gateway page: the gateway that sits between coding agents (Claude Code) and their models,
// observing — and later routing — their traffic. Every signed-in user reaches it (developers
// manage their own gateway keys and read their own usage here); tabs that change how the gateway
// behaves (Catalog, Settings) are the admin's, as are the writes behind them.

import { CatalogTab } from "../components/gateway/CatalogTab";
import { OverviewTab } from "../components/gateway/OverviewTab";
import { ReportTab } from "../components/gateway/ReportTab";
import { SessionsTab } from "../components/gateway/SessionsTab";
import { SettingsTab } from "../components/gateway/SettingsTab";
import { SetupTab } from "../components/gateway/SetupTab";
import { Page } from "../components/ui";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { useAuth } from "../lib/auth";

const TABS = [
  { value: "overview", label: "Overview", admin: false },
  { value: "report", label: "Report", admin: false },
  { value: "sessions", label: "Sessions", admin: false },
  { value: "setup", label: "Setup & keys", admin: false },
  { value: "catalog", label: "Catalog", admin: true },
  { value: "settings", label: "Settings", admin: true },
] as const;

export function Gateway() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");
  return (
    <Page className="space-y-4">
      <div>
        <h1 className="text-lg font-semibold text-foreground">AI Gateway</h1>
        <p className="text-xs text-muted-foreground">
          Sits between Claude Code and its models: records what every exchange cost and what routing
          would save — and, in router mode, routes to cheaper models. It sees prompts; the control
          plane receives only metadata.
        </p>
      </div>

      <Tabs defaultValue="overview">
        {/* Six tabs outgrow a phone: the list scrolls within itself, never the page. */}
        <TabsList className="max-w-full justify-start overflow-x-auto">
          {TABS.filter((t) => isAdmin || !t.admin).map((t) => (
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
        <TabsContent value="sessions">
          <SessionsTab />
        </TabsContent>
        <TabsContent value="setup">
          <SetupTab />
        </TabsContent>
        {isAdmin && (
          <>
            <TabsContent value="catalog">
              <CatalogTab />
            </TabsContent>
            <TabsContent value="settings">
              <SettingsTab />
            </TabsContent>
          </>
        )}
      </Tabs>
    </Page>
  );
}
