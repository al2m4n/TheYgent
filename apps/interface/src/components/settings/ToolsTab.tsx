// Settings → Tools: knobs that apply to every tool an llm calls, whatever its kind (builtin,
// REST, MCP, retrieval) — today the cap on how much of one tool result the model is shown.

import { Card, ErrorBanner, SectionHeading, Spinner } from "../ui";
import { SaveBar } from "./SaveBar";
import { SettingRow } from "./SettingField";
import type { PlatformSettingsForm } from "./useSettingsForm";

export function ToolsTab({ form }: { form: PlatformSettingsForm }) {
  // Gate on data, not isLoading — see BootConfig: retries against an unreachable plane must not
  // flash an empty success layout.
  if (form.loadError && !form.data) return <ErrorBanner error={form.loadError} />;
  if (!form.data) return <Spinner label="Loading settings…" />;

  const entry = form.entry("tools.result_max_chars");

  return (
    <div className="space-y-4">
      <Card className="p-4">
        <SectionHeading>Tool results</SectionHeading>
        <p className="mt-1 text-xs text-muted-foreground">
          A longer result is cut before the model sees it, with a note saying how much was left out.
          An llm node can set its own limit in its Tools section.
        </p>
        <div className="mt-2">
          {entry && <SettingRow entry={entry} form={form} label="max characters per tool result" />}
        </div>
      </Card>

      <SaveBar form={form} group="tools" />
    </div>
  );
}
