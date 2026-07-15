// One chat message, composed from the message + bubble primitives: user turns right-aligned in
// the primary bubble, assistant turns left on the secondary surface. The assistant side composes
// the pieces every transport can produce — thinking block, markdown answer, attachments (image
// thumbs, playable audio), a degraded-turn note, and caller-provided extras (the bench mounts
// per-turn metrics + save here).

import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { Bubble, BubbleContent } from "../components/ui/bubble";
import { Message, MessageContent, MessageFooter, MessageHeader } from "../components/ui/message";
import { shortId } from "../lib/format";
import { Markdown } from "./Markdown";
import { RunTraceBlock } from "./RunTraceBlock";
import { ThinkingBlock } from "./ThinkingBlock";
import type { Attachment, ChatMessage } from "./types";

function AttachmentView({ attachment }: { attachment: Attachment }) {
  if (attachment.kind === "image") {
    return (
      <img
        src={attachment.url}
        alt={attachment.name ?? "attached image"}
        className="max-h-48 max-w-full rounded-xl border object-contain"
      />
    );
  }
  return (
    <div className="flex flex-col gap-0.5">
      {/* biome-ignore lint/a11y/useMediaCaption: user-recorded / synthesized audio, no caption source */}
      <audio controls src={attachment.url} className="h-9 w-full min-w-56" />
      {(attachment.name || attachment.durationSec) && (
        <span className="text-[10px] text-muted-foreground">
          {attachment.name}
          {attachment.durationSec ? ` · ${attachment.durationSec.toFixed(1)}s` : ""}
        </span>
      )}
    </div>
  );
}

export function MessageBubble({ message, extras }: { message: ChatMessage; extras?: ReactNode }) {
  const isUser = message.role === "user";
  return (
    <Message align={isUser ? "end" : "start"}>
      <MessageContent>
        <MessageHeader className="gap-2">
          <span>{isUser ? "You" : "Assistant"}</span>
          {message.runId && (
            <Link
              to="/runs/$runId"
              params={{ runId: message.runId }}
              className="font-normal text-blue-600 hover:text-blue-500 dark:text-blue-400"
              title={message.runId}
            >
              run {shortId(message.runId)}
            </Link>
          )}
        </MessageHeader>
        {message.attachments && message.attachments.length > 0 && (
          // data-slot so the row follows the message alignment (end-aligned on user turns).
          <div data-slot="message-attachments" className="flex max-w-[80%] flex-wrap gap-2">
            {message.attachments.map((a, i) => (
              <AttachmentView key={`${a.url}-${i}`} attachment={a} />
            ))}
          </div>
        )}
        {message.reasoning !== undefined && message.reasoning !== "" && (
          <ThinkingBlock
            reasoning={message.reasoning}
            streaming={message.streaming && !message.content}
          />
        )}
        {isUser ? (
          message.content && (
            <Bubble variant="default">
              <BubbleContent>
                <div className="whitespace-pre-wrap break-words">{message.content}</div>
              </BubbleContent>
            </Bubble>
          )
        ) : message.content ? (
          <Bubble variant="secondary">
            <BubbleContent>
              <Markdown text={message.content} />
              {message.streaming && <span className="animate-pulse text-muted-foreground">▍</span>}
            </BubbleContent>
          </Bubble>
        ) : (
          message.streaming &&
          !message.reasoning && <span className="animate-pulse px-3 text-muted-foreground">▍</span>
        )}
        {message.error && (
          <p className="px-3 text-xs text-amber-700 dark:text-amber-300">{message.error}</p>
        )}
        {/* The run behind this turn, inspectable in place — folded by default; only turns the
            control-plane ran carry a runId (direct data-plane turns have no run to trace). */}
        {!isUser && message.runId && (
          <RunTraceBlock runId={message.runId} streaming={message.streaming} />
        )}
        {extras && <MessageFooter className="flex-col items-start gap-1">{extras}</MessageFooter>}
      </MessageContent>
    </Message>
  );
}
