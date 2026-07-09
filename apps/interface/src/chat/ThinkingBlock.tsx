// A reasoning model's thinking, rendered as a collapsible block above the answer. Open while
// the model is still thinking (visible progress — a long reasoning phase must not look frozen),
// folding away once the answer lands; the reader can always reopen it. While streaming, the
// block follows its live edge (long thinking exceeds the capped height fast) — scrolling up
// unpins it, scrolling back to the bottom re-pins it.

import { Brain, ChevronRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "../components/ui/collapsible";

export function ThinkingBlock({
  reasoning,
  streaming,
}: { reasoning: string; streaming?: boolean }) {
  const [open, setOpen] = useState(Boolean(streaming));
  const pinnedRef = useRef(false);
  const preRef = useRef<HTMLPreElement>(null);
  // Follow the live edge until the reader scrolls up; reaching the bottom again re-sticks.
  const stickRef = useRef(true);

  // Auto-collapse when the turn finishes — unless the reader toggled it themselves.
  useEffect(() => {
    if (!streaming && !pinnedRef.current) setOpen(false);
    if (streaming && !pinnedRef.current) setOpen(true);
  }, [streaming]);

  // Keep the newest thinking in view while it streams (and when the block [re]opens mid-stream).
  // biome-ignore lint/correctness/useExhaustiveDependencies: reasoning growth is the scroll trigger
  useEffect(() => {
    const el = preRef.current;
    if (!el || !streaming || !stickRef.current) return;
    el.scrollTop = el.scrollHeight;
  }, [reasoning, streaming, open]);

  if (!reasoning) return null;
  return (
    <Collapsible
      open={open}
      onOpenChange={(next) => {
        pinnedRef.current = true;
        setOpen(next);
      }}
      className="rounded-lg border bg-card"
    >
      <CollapsibleTrigger className="flex w-full items-center gap-1.5 px-2 py-1.5 text-xs text-muted-foreground hover:text-foreground [&[data-state=open]>svg:last-child]:rotate-90">
        <Brain size={13} />
        <span className="font-medium">{streaming ? "Thinking…" : "Thinking"}</span>
        <ChevronRight size={13} className="ml-auto transition-transform" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <pre
          ref={preRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          }}
          className="max-h-64 overflow-auto whitespace-pre-wrap break-words border-t px-2.5 py-2 text-xs leading-relaxed text-muted-foreground"
        >
          {reasoning}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}
