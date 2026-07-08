// One session, reopened as a live conversation. The stored transcript seeds the unified chat;
// the session's metadata names its target (a model or a saved agent), so the composer keeps
// talking to the same thing. Text targets continue through the control-plane run path — each new
// turn is a streamed run carrying this session id, and the server appends the pair. A VOICE
// session (a transcription/speech model) continues through the direct data-plane transport,
// same as it was recorded — audio never transits the control plane. A session whose target is
// unknown (recorded before targets were stored) stays readable, just not continuable.

import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { ChatView } from "../chat/ChatView";
import type { SessionKind } from "../chat/session";
import type { ChatMessage } from "../chat/types";
import { type InferenceChatModality, useInferenceChat } from "../chat/useInferenceChat";
import { type RunChatTarget, useRunChat } from "../chat/useRunChat";
import { Badge, Button, ConfirmDialog, ErrorBanner, Page, Spinner } from "../components/ui";
import { api } from "../lib/api";
import { relativeTime, shortId } from "../lib/format";
import { notify } from "../lib/notify";
import type { SessionDetail as SessionDetailWire } from "../lib/runtypes";
import { useSession } from "../queries";

function isAudioSession(metadata: Record<string, unknown> | null): boolean {
  return typeof metadata?.modality === "string" && metadata.modality.startsWith("audio.");
}

function targetOf(metadata: Record<string, unknown> | null): RunChatTarget | null {
  if (!metadata) return null;
  if (metadata.kind === "bench.agent" && typeof metadata.agent_id === "string") {
    return {
      kind: "agent",
      agentId: metadata.agent_id,
      agentName: typeof metadata.agent_name === "string" ? metadata.agent_name : undefined,
    };
  }
  // A transcription/speech session continues through the data-plane transport, not the text
  // run path — a chat run against a speech model could only fail.
  if (isAudioSession(metadata)) return null;
  if (typeof metadata.model === "string") return { kind: "model", model: metadata.model };
  return null;
}

function targetLabel(metadata: Record<string, unknown> | null): string | null {
  if (!metadata) return null;
  if (typeof metadata.agent_name === "string") return `agent · ${metadata.agent_name}`;
  if (typeof metadata.agent_id === "string") return `agent · ${shortId(metadata.agent_id)}`;
  if (typeof metadata.model === "string") return String(metadata.model);
  return null;
}

// Keyed by session id from the parent so the transcript seeds exactly once per loaded session.
function SessionChat({ detail }: { detail: SessionDetailWire }) {
  const meta = detail.metadata;
  const audio = isAudioSession(meta);
  const voiceModel = audio && typeof meta?.model === "string" ? meta.model : "";
  const initialMessages: ChatMessage[] = detail.messages.map((m) => ({
    id: m.id,
    role: m.role === "assistant" ? "assistant" : "user",
    content: m.content,
    runId: m.run_id ?? undefined,
  }));
  const runChat = useRunChat(audio ? null : targetOf(meta), {
    sessionId: detail.id,
    initialMessages,
  });
  const voiceChat = useInferenceChat({
    logicalId: voiceModel,
    modality: (audio && typeof meta?.modality === "string"
      ? meta.modality
      : "chat") as InferenceChatModality,
    params: {},
    sessionId: detail.id,
    sessionKind: (typeof meta?.kind === "string" ? meta.kind : "chat") as SessionKind,
    initialMessages,
  });
  const chat = audio && voiceModel ? voiceChat : runChat;
  return (
    <ChatView
      controller={chat}
      listClassName="max-h-[62vh]"
      emptyHint="No messages in this session yet — say something below."
    />
  );
}

export function SessionDetail() {
  const { sessionId } = useParams({ from: "/sessions/$sessionId" });
  // fresh: the chat below seeds its transcript once from this response — it must reflect the
  // server, not a cache from an earlier visit.
  const { data: session, isFetchedAfterMount, error } = useSession(sessionId, { fresh: true });
  const navigate = useNavigate();
  const [confirmDelete, setConfirmDelete] = useState(false);

  if (!isFetchedAfterMount && !error)
    return (
      <Page>
        <Spinner />
      </Page>
    );
  if (error)
    return (
      <Page>
        <ErrorBanner error={error} />
      </Page>
    );
  if (!session)
    return (
      <Page>
        <ErrorBanner error="session not found" />
      </Page>
    );

  const label = targetLabel(session.metadata);
  return (
    <Page className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link to="/sessions" className="text-sm text-slate-400 hover:text-slate-200">
          ← Chats
        </Link>
        <h1 className="mono text-sm font-semibold text-slate-100">{session.id}</h1>
        {label && <Badge tone="blue">{label}</Badge>}
        <span className="text-xs text-slate-500">
          {session.messages.length} messages · created {relativeTime(session.created_at)}
        </span>
        <Button
          variant="danger"
          className="ml-auto shrink-0"
          onClick={() => setConfirmDelete(true)}
        >
          Delete
        </Button>
      </div>

      <SessionChat key={session.id} detail={session} />

      {confirmDelete && (
        <ConfirmDialog
          title="Delete session"
          message={`Delete session ${shortId(session.id)} and its ${session.messages.length} messages? Runs stay; they just lose the session link.`}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={async () => {
            try {
              await api.deleteSession(session.id);
              navigate({ to: "/sessions" });
            } catch (e) {
              notify.error(e instanceof Error ? e.message : String(e));
              setConfirmDelete(false);
            }
          }}
        />
      )}
    </Page>
  );
}
