// The per-tab dirty-state footer: appears once anything in the tab's group is edited, shows the
// change count, and bulk-PATCHes just that group. Client-side blockers (invalid numbers, bad
// registry URLs) hold the button — the server validates the batch atomically, so one bad key
// would reject everything.

import type { SettingGroup } from "../../lib/api";
import { Button, ErrorBanner } from "../ui";
import type { PlatformSettingsForm } from "./useSettingsForm";

export function SaveBar({ form, group }: { form: PlatformSettingsForm; group: SettingGroup }) {
  const dirty = form.dirtyKeys(group).length;
  const blocked = form.blockerKeys(group).length;
  if (dirty === 0 && blocked === 0) return null;

  return (
    <div className="sticky bottom-0 z-10 -mx-1 space-y-2 border-t border-border bg-background/95 px-1 py-3 backdrop-blur">
      <div className="flex items-center gap-3">
        <span className="text-xs text-muted-foreground">
          {dirty} unsaved {dirty === 1 ? "change" : "changes"}
        </span>
        {blocked > 0 && (
          <span className="text-xs text-destructive">
            {blocked} invalid {blocked === 1 ? "field" : "fields"} — fix before saving
          </span>
        )}
        <Button
          variant="primary"
          className="ml-auto"
          disabled={dirty === 0 || blocked > 0 || form.saving}
          onClick={() => form.saveGroup(group)}
        >
          {form.saving ? "Saving…" : `Save ${dirty === 1 ? "change" : "changes"}`}
        </Button>
      </div>
      <ErrorBanner error={form.saveError} />
    </div>
  );
}
