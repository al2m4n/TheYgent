// One chat message: user turns right-aligned on the blue accent, assistant turns left on the
// surface ramp. The assistant side composes the pieces every transport can produce — thinking
// block, markdown answer, attachments (image thumbs, playable audio), a degraded-turn note, and
// caller-provided extras (the bench mounts per-turn metrics + save here).

import { Link } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { shortId } from "../lib/format";
import { Markdown } from "./Markdown";
import { ThinkingBlock } from "./ThinkingBlock";
import type { Attachment, ChatMessage } from "./types";

function AttachmentView({ attachment }: { attachment: Attachment }) {
  if (attachment.kind === "image") {
    return (
      <img
        src={attachment.url}
        alt={attachment.name ?? "attached image"}
        className="max-h-48 max-w-full rounded-md border border-slate-800 object-contain"
      />
    );
  }
  return (
    <div className="flex flex-col gap-0.5">
      {/* biome-ignore lint/a11y/useMediaCaption: user-recorded / synthesized audio, no caption source */}
      <audio controls src={attachment.url} className="h-9 w-full min-w-56" />
      {(attachment.name || attachment.durationSec) && (
        <span className="text-[10px] text-slate-500">
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
    <div className={`flex ${isUser ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[85%] space-y-2 rounded-lg border px-3 py-2 ${
          isUser ? "border-blue-500/30 bg-blue-500/10" : "border-slate-800 bg-[var(--c-surface-2)]"
        }`}
      >
        <div className="flex items-baseline gap-2 text-[10px] uppercase tracking-wide text-slate-500">
          <span>{message.role}</span>
          {message.runId && (
            <Link
              to="/runs/$runId"
              params={{ runId: message.runId }}
              className="lowercase text-blue-600 hover:text-blue-500 dark:text-blue-400"
            >
              run {shortId(message.runId)}
            </Link>
          )}
        </div>
        {message.attachments && message.attachments.length > 0 && (
          <div className="flex flex-wrap gap-2">
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
            <pre className="whitespace-pre-wrap break-words font-sans text-sm text-slate-200">
              {message.content}
            </pre>
          )
        ) : message.content ? (
          <div>
            <Markdown text={message.content} />
            {message.streaming && <span className="animate-pulse text-slate-400">▍</span>}
          </div>
        ) : (
          message.streaming &&
          !message.reasoning && <span className="animate-pulse text-slate-500">▍</span>
        )}
        {message.error && (
          <p className="text-xs text-amber-700 dark:text-amber-300">{message.error}</p>
        )}
        {extras}
      </div>
    </div>
  );
}
