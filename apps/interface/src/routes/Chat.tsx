// The New Chat page: pick what to talk to — a registered model or a saved agent — and go.
// Every conversation is a session (created on the first message, listed under Recents and
// Chats). Text targets run through streamed control-plane runs (the server replays history and
// appends the turns). Voice targets — a transcription model (speak or attach audio, the reply is
// the transcript) or a speech model (type text, the reply is playable audio) — go DIRECTLY to
// the inference data plane, exactly like the bench: raw audio never transits the control plane;
// the finished turn pair is recorded to the session as text. The target pin freezes once the
// conversation starts; "New chat" starts a fresh session.

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { ChatView } from "../chat/ChatView";
import { type InferenceChatModality, useInferenceChat } from "../chat/useInferenceChat";
import { type RunChatTarget, useRunChat } from "../chat/useRunChat";
import { Badge, Button, Card, ErrorBanner, Page, Select, linkClass } from "../components/ui";
import { api } from "../lib/api";
import { shortId } from "../lib/format";
import { useModels } from "../queries";

type TargetKind = "model" | "agent";

const VOICE_MODALITIES = new Set<string>(["audio.transcription", "audio.speech"]);

function ChatSurface({ onNewChat }: { onNewChat: () => void }) {
  const models = useModels();
  // Same key + shape as the canvas node inspector's agent picker, so the two share one cache.
  const agents = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.listAgents({ limit: 200 }),
    retry: false,
    staleTime: 30_000,
  });

  const [kind, setKind] = useState<TargetKind>("model");
  const [pickedModel, setPickedModel] = useState("");
  const [pickedAgent, setPickedAgent] = useState("");
  const modelView = models.data?.find(
    (m) => m.logicalId === (pickedModel || models.data?.[0]?.logicalId),
  );
  const model = modelView?.logicalId ?? "";
  const agent = agents.data?.find((a) => a.id === (pickedAgent || agents.data?.[0]?.id));

  // The registered modality decides the transport: chat/vision talk through control-plane runs;
  // a voice model talks straight to the data plane (there is no run path for audio).
  const modality = modelView?.binding.modality ?? "chat";
  const isVoice = kind === "model" && VOICE_MODALITIES.has(modality);
  const isEmbeddings = kind === "model" && modality === "embeddings";

  const runTarget: RunChatTarget | null =
    kind === "model"
      ? model && !isVoice && !isEmbeddings
        ? { kind: "model", model }
        : null
      : agent
        ? { kind: "agent", agentId: agent.id, agentName: agent.name }
        : null;

  const runChat = useRunChat(runTarget, {
    placeholder: kind === "agent" ? "Message the agent…" : "Send a message…",
    disabledNote:
      kind === "agent"
        ? "No saved agents yet — build one on the canvas first."
        : isEmbeddings
          ? "Embeddings models don't chat — bench them from Registries."
          : "No models registered yet — install one under Registries first.",
  });
  const voiceChat = useInferenceChat({
    logicalId: model,
    modality: (isVoice ? modality : "chat") as InferenceChatModality,
    params: {},
    sessionKind: "chat",
  });

  const chat = isVoice ? voiceChat : runChat;
  const started = chat.messages.length > 0;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold text-slate-100">New Chat</h1>
        {/* What to talk to. Both pins freeze once the conversation starts (the session's target
            is fixed); "New chat" starts over. */}
        <Select
          value={kind}
          onChange={(e) => setKind(e.target.value as TargetKind)}
          disabled={started}
          aria-label="Target kind"
          className="w-28"
        >
          <option value="model">Model</option>
          <option value="agent">Agent</option>
        </Select>
        {kind === "model" ? (
          <>
            <Select
              value={model}
              onChange={(e) => setPickedModel(e.target.value)}
              disabled={started}
              aria-label="Model"
              className="w-64"
              title={started ? "The model is pinned once the conversation starts" : undefined}
            >
              {(models.data ?? []).map((m) => (
                <option key={m.logicalId} value={m.logicalId}>
                  {m.logicalId}
                </option>
              ))}
            </Select>
            {modality !== "chat" && <Badge tone="violet">{modality}</Badge>}
          </>
        ) : (
          <Select
            value={agent?.id ?? ""}
            onChange={(e) => setPickedAgent(e.target.value)}
            disabled={started}
            aria-label="Agent"
            className="w-64"
            title={started ? "The agent is pinned once the conversation starts" : undefined}
          >
            {(agents.data ?? []).map((a) => (
              <option key={a.id} value={a.id}>
                {a.name}
              </option>
            ))}
          </Select>
        )}
        {started && (
          <Button variant="ghost" onClick={onNewChat}>
            New chat
          </Button>
        )}
        {chat.sessionId && (
          <Link
            to="/sessions/$sessionId"
            params={{ sessionId: chat.sessionId }}
            className={`ml-auto text-xs ${linkClass}`}
          >
            session {shortId(chat.sessionId)}
          </Link>
        )}
      </div>
      {models.error && kind === "model" && <ErrorBanner error={models.error} />}
      {agents.error && kind === "agent" && <ErrorBanner error={agents.error} />}
      <Card className="p-4">
        <ChatView
          controller={chat}
          listClassName="max-h-[calc(100vh-320px)] min-h-40"
          emptyHint={null}
        />
      </Card>
    </div>
  );
}

export function Chat() {
  // Remount the surface to start a fresh conversation (new session, clean transcript).
  const [nonce, setNonce] = useState(0);
  return (
    <Page className="space-y-4">
      <ChatSurface key={nonce} onNewChat={() => setNonce((n) => n + 1)} />
    </Page>
  );
}
