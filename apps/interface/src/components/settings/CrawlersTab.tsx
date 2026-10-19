// Settings → Crawlers: the platform ceiling on what one crawler call may charge. Every crawler
// node's own charge cap applies below it, so no agent can raise its own spend past this value.

import { Card, ErrorBanner, SectionHeading, Spinner } from "../ui";
import { SaveBar } from "./SaveBar";
import { SettingRow } from "./SettingField";
import type { PlatformSettingsForm } from "./useSettingsForm";

export function CrawlersTab({ form }: { form: PlatformSettingsForm }) {
  // Gate on data, not isLoading — retries against an unreachable plane must not flash an empty
  // success layout.
  if (form.loadError && !form.data) return <ErrorBanner error={form.loadError} />;
  if (!form.data) return <Spinner label="Loading settings…" />;
  const entry = form.entry("crawler.max_charge_usd");

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionHeading>Spend</SectionHeading>
        <p className="mt-1 text-xs text-muted-foreground">
          Crawler nodes run hosted actors that bill the account behind their connection's token.
          Each run is capped at the node's own charge cap, held under this ceiling.
        </p>
        <div className="mt-2">
          {entry && <SettingRow entry={entry} form={form} label="max charge per call (USD)" />}
        </div>
      </Card>
      <SaveBar form={form} group="crawler" />
    </div>
  );
}
