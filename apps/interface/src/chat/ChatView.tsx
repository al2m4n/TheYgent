// The one chat shell: transcript + composer, driven by any transport that satisfies
// ChatController. Every chat in the app (model bench, agent bench, session continuation, new
// chat) renders through this component so bubbles, thinking blocks, attachments, and streaming
// behave identically everywhere.

import { type ReactNode, useEffect, useRef, useState } from "react";
import { ErrorBanner } from "../components/ui";
import { Composer } from "./Composer";
import { MessageBubble } from "./MessageBubble";
import type { ChatController, ChatMessage } from "./types";

export function ChatView({
  controller,
  emptyHint,
  renderExtras,
  listClassName = "max-h-[55vh]",
}: {
  controller: ChatController;
  /** Shown while the transcript is empty; pass `null` to show nothing at all. */
  emptyHint?: ReactNode;
  /** Per-message add-ons below the bubble body (the bench mounts metrics + save-result here). */
  renderExtras?: (message: ChatMessage) => ReactNode;
  /** Height constraint for the transcript region (the page decides how tall the chat is). */
  listClassName?: string;
}) {
  const listRef = useRef<HTMLDivElement | null>(null);
  const [stick, setStick] = useState(true);
  const last = controller.messages[controller.messages.length - 1];

  // Follow the stream while the reader is at the bottom; stop following the moment they scroll
  // up to reread something (and resume when they come back down). Streaming/attachment/error
  // transitions change height without changing text, so they are dependencies too.
  // biome-ignore lint/correctness/useExhaustiveDependencies: scroll follows content growth
  useEffect(() => {
    if (stick && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight;
  }, [
    stick,
    controller.messages.length,
    last?.content,
    last?.reasoning,
    last?.streaming,
    last?.error,
    last?.attachments?.length,
  ]);

  return (
    <div className="flex min-h-0 flex-col gap-3">
      <div
        ref={listRef}
        className={`min-h-24 space-y-3 overflow-y-auto pr-1 ${listClassName}`}
        onScroll={(e) => {
          const el = e.currentTarget;
          setStick(el.scrollHeight - el.scrollTop - el.clientHeight < 80);
        }}
      >
        {controller.messages.length === 0 && emptyHint !== null && (
          <div className="flex h-24 items-center justify-center text-sm text-slate-500">
            {emptyHint ?? "No messages yet — say something below."}
          </div>
        )}
        {controller.messages.map((m) => (
          <MessageBubble key={m.id} message={m} extras={renderExtras?.(m)} />
        ))}
      </div>
      <ErrorBanner error={controller.error} />
      <Composer
        caps={controller.composer}
        busy={controller.busy}
        disabled={controller.disabled}
        disabledNote={controller.disabledNote}
        onSend={controller.send}
        onStop={controller.stop}
      />
    </div>
  );
}
