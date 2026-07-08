// The New Chat page: pick what to talk to — a registered model or a saved agent — and go.
// Every conversation is a session (created on the first message, listed under Recents and Chats).
// Transports by target:
//   - a text model / agent → streamed control-plane runs (server replays history, appends turns);
//   - a voice MODEL (transcription/speech) → DIRECTLY to the inference data plane like the bench
//     (raw audio never transits the control plane; the turn is recorded as text);
//   - a voice AGENT (input/output boundary is audio) → the control-plane run path, which the
//     walker orchestrates (transcribe → llm → speak): the recorded clip uploads to the artifact
//     store as the run input, and the spoken-answer artifact downloads into a playable bubble.
// The target pin freezes once the conversation starts; "New chat" starts a fresh session.

import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { ChatView } from "../chat/ChatView";
import { type InferenceChatModality, useInferenceChat } from "../chat/useInferenceChat";
import { type RunChatTarget, useRunChat } from "../chat/useRunChat";
import { Badge, Button, Card, ErrorBanner, Page, Select, linkClass } from "../components/ui";
import { api } from "../lib/api";
import { shortId } from "../lib/format";
import { useModels } from "../queries";

type TargetKind = "model" | "agent";

// A boundary node in an agent's IR — we only read its declared payload modality.
interface AgentBoundaryNode {
  type: string;
  config?: { modality?: string };
}

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
  // Null = follow latest (recomputed from fresh data — never captured once, mirroring the bench).
  const [pickedVersion, setPickedVersion] = useState<string | null>(null);
  const modelView = models.data?.find(
    (m) => m.logicalId === (pickedModel || models.data?.[0]?.logicalId),
  );
  const model = modelView?.logicalId ?? "";
  const agent = agents.data?.find((a) => a.id === (pickedAgent || agents.data?.[0]?.id));

  // The picked agent's full version list (newest first) — so a specific version can be pinned to
  // test, not only the latest.
  const agentDetail = useQuery({
    queryKey: ["agent", agent?.id],
    queryFn: () => api.getAgent(agent?.id ?? ""),
    enabled: kind === "agent" && Boolean(agent?.id),
  });
  const versions = agentDetail.data?.versions ?? [];
  const version = pickedVersion ?? versions[0]?.version ?? agent?.latest_version ?? "";
  // A fresh agent selection follows that agent's latest again (a pin can't carry across agents).
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset the pin only when the agent changes
  useEffect(() => setPickedVersion(null), [agent?.id]);

  // Read the picked version's boundary modalities from its IR — an audio input/output agent gets
  // the voice composer and a spoken-reply bubble instead of text (the run path is the same).
  const agentVersion = useQuery({
    queryKey: ["agentver", agent?.id, version],
    queryFn: () => api.getAgentVersion(agent?.id ?? "", version),
    enabled: kind === "agent" && Boolean(agent?.id && version),
  });
  const agentNodes = (agentVersion.data?.ir as { nodes?: AgentBoundaryNode[] } | undefined)?.nodes;
  const boundaryModality = (nodeType: string) =>
    agentNodes?.find((n) => n.type === nodeType)?.config?.modality ?? "text";
  const agentAudioIn = kind === "agent" && boundaryModality("input") === "audio";
  const agentAudioOut = kind === "agent" && boundaryModality("output") === "audio";
  const agentImageIn = kind === "agent" && boundaryModality("input") === "image";
  const agentImageOut = kind === "agent" && boundaryModality("output") === "image";

  // The registered modality decides a model's transport. Voice and VISION models talk straight to
  // the inference data plane (like the bench): a voice model has no run path, and a vision model
  // needs image content parts the direct transport already builds. A plain chat model runs through
  // the control plane. (Vision rides /v1/chat/completions — it's a chat sub-capability.)
  const modality = modelView?.binding.modality ?? "chat";
  const isVoice = kind === "model" && VOICE_MODALITIES.has(modality);
  const isVision = kind === "model" && modality === "vision";
  const isImageGen = kind === "model" && modality === "images.generation";
  const isEmbeddings = kind === "model" && modality === "embeddings";
  const useDirect = isVoice || isVision || isImageGen;

  const runTarget: RunChatTarget | null =
    kind === "model"
      ? model && !useDirect && !isEmbeddings
        ? { kind: "model", model }
        : null
      : agent
        ? { kind: "agent", agentId: agent.id, agentName: agent.name, version: version || undefined }
        : null;

  const runChat = useRunChat(runTarget, {
    placeholder: kind === "agent" ? "Message the agent…" : "Send a message…",
    audioInput: agentAudioIn,
    audioOutput: agentAudioOut,
    imageInput: agentImageIn,
    imageOutput: agentImageOut,
    disabledNote:
      kind === "agent"
        ? "No saved agents yet — build one on the canvas first."
        : isEmbeddings
          ? "Embeddings models don't chat — bench them from Registries."
          : "No models registered yet — install one under Registries first.",
  });
  const directChat = useInferenceChat({
    logicalId: model,
    // Voice + image-generation ride their own modality; vision rides the chat modality with image
    // attach turned on by caps.vision.
    modality: (isVoice || isImageGen ? modality : "chat") as InferenceChatModality,
    caps: isVision ? { vision: true } : undefined,
    params: {},
    sessionKind: "chat",
  });

  const chat = useDirect ? directChat : runChat;
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
        {/* Which version to test — defaults to latest; pinned once the conversation starts. */}
        {kind === "agent" && versions.length > 0 && (
          <Select
            value={version}
            onChange={(e) => setPickedVersion(e.target.value)}
            disabled={started}
            aria-label="Version"
            className="w-44"
            title={started ? "The version is pinned once the conversation starts" : undefined}
          >
            {versions.map((v, i) => (
              <option key={v.version} value={v.version}>
                v{v.version}
                {i === 0 ? " · latest" : ""} · {shortId(v.content_hash, 10)}
              </option>
            ))}
          </Select>
        )}
        {kind === "agent" && (agentAudioIn || agentAudioOut) && (
          <Badge tone="violet">{agentAudioIn && agentAudioOut ? "voice" : "audio"}</Badge>
        )}
        {kind === "agent" && agentImageIn && <Badge tone="violet">vision</Badge>}
        {kind === "agent" && agentImageOut && <Badge tone="violet">image</Badge>}
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
