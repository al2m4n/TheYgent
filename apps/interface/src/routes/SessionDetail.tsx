// One session, reopened as a live conversation. The stored transcript seeds the unified chat;
// the session's metadata names its target (a model or a published agent), so the composer keeps
// talking to the same thing. Text targets continue through the control-plane run path — each new
// turn is a streamed run carrying this session id, and the server appends the pair. A VOICE
// session (a transcription/speech model) continues through the direct data-plane transport,
// same as it was recorded — audio never transits the control plane. A session whose target is
// unknown (recorded before targets were stored) stays readable, just not continuable.
//
// An AGENT session re-derives its I/O boundary from the recorded version's IR, so a voice or
// vision conversation reopens with the same composer it was started with — continuing one must
// not silently hand a bare string to a media boundary.

import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate, useParams } from "@tanstack/react-router";
import { ChevronLeft } from "lucide-react";
import { useState } from "react";
import { ChatView } from "../chat/ChatView";
import type { SessionKind } from "../chat/session";
import type { ChatMessage } from "../chat/types";
import { type InferenceChatModality, useInferenceChat } from "../chat/useInferenceChat";
import { type RunChatTarget, useRunChat } from "../chat/useRunChat";
import { TimeAgo } from "../components/TimeAgo";
import { ConfirmDialog, ErrorBanner, Page, Spinner } from "../components/ui";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { api } from "../lib/api";
import { toneOf } from "../lib/categories";
import { shortId } from "../lib/format";
import { boundaryOf } from "../lib/modality";
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
      // Sessions recorded before the version was stored continue on latest — the only honest
      // fallback, since nothing else records what they ran.
      version: typeof metadata.agent_version === "string" ? metadata.agent_version : undefined,
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
  const target = audio ? null : targetOf(meta);
  const agentId = target?.kind === "agent" ? target.agentId : "";
  const pinned = target?.kind === "agent" && target.version != null ? String(target.version) : "";
  // An agent target's composer and run payload are properties of its BOUNDARY, so the version's IR
  // has to be read back before the conversation can continue. A session recorded before the version
  // was stored resolves the agent's latest — the only honest fallback, and the same version the
  // continued run will execute.
  const agentVersion = useQuery({
    queryKey: ["agentver", agentId, pinned || "latest"],
    queryFn: async () => {
      if (pinned) {
        try {
          return await api.getAgentVersion(agentId, pinned);
        } catch {
          // The pinned version was deleted since this conversation was recorded. Continuing on
          // latest is the honest fallback — refusing outright would strand a readable session
          // that the agent can still answer.
        }
      }
      const latest = (await api.getAgent(agentId)).versions[0]?.version;
      return latest ? api.getAgentVersion(agentId, latest) : null;
    },
    enabled: Boolean(agentId),
  });
  // Until it resolves we do not KNOW the boundary; offering the text composer would let a voice
  // session's next turn go out as a bare string. A failed read is the same state, not a text agent.
  const boundaryUnknown = Boolean(agentId) && (agentVersion.isPending || agentVersion.isError);
  // Whatever version actually resolved is what the continued run must execute — a pin that fell
  // back to latest must not keep sending the id that 404'd.
  const resolvedVersion = agentVersion.data?.version;
  const runChat = useRunChat(
    boundaryUnknown || !target
      ? null
      : target.kind === "agent" && resolvedVersion
        ? { ...target, version: resolvedVersion }
        : target,
    {
      sessionId: detail.id,
      initialMessages,
      boundary: boundaryOf(agentVersion.data?.ir),
      disabledNote: boundaryUnknown
        ? agentVersion.isError
          ? "This agent's version could not be read, so its input boundary is unknown."
          : "Reading this agent's input boundary…"
        : undefined,
    },
  );
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
      listClassName="flex-1"
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
    // Full-height chat layout: header shrinks to content, the conversation owns the rest, and the
    // composer stays pinned at the bottom — the page itself never scrolls behind it.
    <Page className="flex h-full min-h-0 flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <Link
          to="/sessions"
          className="inline-flex shrink-0 items-center gap-1 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground"
        >
          <ChevronLeft size={16} /> Chats
        </Link>
        <span className="mono break-all text-sm font-semibold text-foreground">{session.id}</span>
        {label && (
          <Badge variant="secondary" className={toneOf("blue").badge}>
            {label}
          </Badge>
        )}
        <span className="text-xs text-muted-foreground">
          {session.messages.length} messages · created <TimeAgo iso={session.created_at} />
        </span>
        <Button
          type="button"
          variant="destructive"
          className="ml-auto shrink-0"
          onClick={() => setConfirmDelete(true)}
        >
          Delete
        </Button>
      </div>

      <div className="min-h-0 flex-1">
        <SessionChat key={session.id} detail={session} />
      </div>

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
