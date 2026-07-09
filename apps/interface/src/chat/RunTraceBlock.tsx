// The per-turn run trace, folded under each run-backed answer — the same collapsible shape as the
// thinking block, but an inspection tool rather than progress, so it starts FOLDED and stays out
// of the reading flow. The waterfall mounts only when opened: an unopened turn costs zero trace
// fetches, and opening a still-streaming turn attaches the live overlay for growing bars.

import { Activity, ChevronRight } from "lucide-react";
import { useState } from "react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../components/ui/collapsible";
import { RunWaterfall } from "../components/waterfall";

export function RunTraceBlock({ runId, streaming }: { runId: string; streaming?: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <Collapsible open={open} onOpenChange={setOpen} className="rounded-lg border bg-card">
      <CollapsibleTrigger className="flex w-full items-center gap-1.5 px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground [&[data-state=open]>svg:last-child]:rotate-90">
        <Activity size={13} />
        <span className="font-medium">Trace</span>
        <ChevronRight size={13} className="ml-auto transition-transform" />
      </CollapsibleTrigger>
      <CollapsibleContent className="border-t p-2">
        <RunWaterfall runId={runId} isLive={Boolean(streaming)} compact />
      </CollapsibleContent>
    </Collapsible>
  );
}
