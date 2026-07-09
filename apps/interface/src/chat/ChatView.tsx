// The one chat shell: transcript + composer, driven by any transport that satisfies
// ChatController. Every chat in the app (model bench, agent bench, session continuation, new
// chat) renders through this component so bubbles, thinking blocks, attachments, and streaming
// behave identically everywhere. The transcript rides the message-scroller primitive: it follows
// the live edge while streaming, stops when the reader scrolls up, and offers a jump-to-latest
// control.

import type { ReactNode } from "react";
import { ErrorBanner } from "../components/ui";
import {
  MessageScroller,
  MessageScrollerButton,
  MessageScrollerContent,
  MessageScrollerItem,
  MessageScrollerProvider,
  MessageScrollerViewport,
} from "../components/ui/message-scroller";
import { cn } from "../lib/utils";
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
  return (
    // h-full so a page that grants this chat a fixed region (session detail, new chat) gets the
    // standard chat layout — transcript filling the space, composer pinned below it. In an
    // auto-height context (the bench modal body) h-full resolves to auto and the max-h caps apply.
    <div className="flex h-full min-h-0 flex-col gap-3">
      <MessageScrollerProvider autoScroll>
        <MessageScroller className={cn("min-h-24", listClassName)}>
          <MessageScrollerViewport className="pr-1">
            <MessageScrollerContent className="gap-3">
              {controller.messages.length === 0 && emptyHint !== null && (
                <div className="flex flex-1 items-center justify-center py-8 text-sm text-muted-foreground">
                  {emptyHint ?? "No messages yet — say something below."}
                </div>
              )}
              {controller.messages.map((m) => (
                <MessageScrollerItem key={m.id} messageId={m.id} scrollAnchor={m.role === "user"}>
                  <MessageBubble message={m} extras={renderExtras?.(m)} />
                </MessageScrollerItem>
              ))}
            </MessageScrollerContent>
          </MessageScrollerViewport>
          <MessageScrollerButton />
        </MessageScroller>
      </MessageScrollerProvider>
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
