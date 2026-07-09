// A relative timestamp ("2h ago") that reveals the exact moment on hover. Every surface that
// shows a relative time renders through this, so the hover affordance (dotted underline) and the
// absolute format stay identical app-wide.

import { relativeTime } from "../lib/format";
import { HoverCard, HoverCardContent, HoverCardTrigger } from "./ui/hover-card";

export function TimeAgo({
  iso,
  label,
}: {
  iso: string | null | undefined;
  /** Override the visible text (e.g. a coarser "3mo ago") — the hover still shows the exact moment. */
  label?: string;
}) {
  const rel = label ?? relativeTime(iso);
  if (!iso || rel === "—") return <>{rel}</>;
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return <>{rel}</>;
  return (
    <HoverCard openDelay={200} closeDelay={50}>
      <HoverCardTrigger asChild>
        <span className="cursor-default underline decoration-dotted decoration-muted-foreground/50 underline-offset-2">
          {rel}
        </span>
      </HoverCardTrigger>
      <HoverCardContent side="top" className="w-auto px-3 py-1.5 text-xs">
        {at.toLocaleString(undefined, { dateStyle: "full", timeStyle: "medium" })}
      </HoverCardContent>
    </HoverCard>
  );
}
