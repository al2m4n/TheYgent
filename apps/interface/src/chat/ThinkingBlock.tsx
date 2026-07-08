// A reasoning model's thinking, rendered as a collapsible block above the answer. Open while
// the model is still thinking (visible progress — a long reasoning phase must not look frozen),
// folding away once the answer lands; the reader can always reopen it.

import { Brain, ChevronDown, ChevronRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";

export function ThinkingBlock({
  reasoning,
  streaming,
}: { reasoning: string; streaming?: boolean }) {
  const [open, setOpen] = useState(Boolean(streaming));
  const pinnedRef = useRef(false);

  // Auto-collapse when the turn finishes — unless the reader toggled it themselves.
  useEffect(() => {
    if (!streaming && !pinnedRef.current) setOpen(false);
    if (streaming && !pinnedRef.current) setOpen(true);
  }, [streaming]);

  if (!reasoning) return null;
  const Chevron = open ? ChevronDown : ChevronRight;
  return (
    <div className="rounded-md border border-slate-800 bg-[var(--c-surface)]">
      <button
        type="button"
        className="flex w-full items-center gap-1.5 px-2 py-1.5 text-xs text-slate-500 hover:text-slate-300"
        aria-expanded={open}
        onClick={() => {
          pinnedRef.current = true;
          setOpen((o) => !o);
        }}
      >
        <Brain size={13} />
        <span className="font-medium">{streaming ? "Thinking…" : "Thinking"}</span>
        <Chevron size={13} className="ml-auto" />
      </button>
      {open && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words border-t border-slate-800 px-2.5 py-2 text-xs leading-relaxed text-slate-400">
          {reasoning}
        </pre>
      )}
    </div>
  );
}
