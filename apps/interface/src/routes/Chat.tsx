// The New Chat page: pick what to talk to — a registered model or a published agent — and go.
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
import { SearchableSelect } from "../components/SearchableSelect";
import { ErrorBanner, Page, linkClass } from "../components/ui";
import { Badge } from "../components/ui/badge";
import { Button } from "../components/ui/button";
import { Card, CardContent } from "../components/ui/card";
import { NativeSelect, NativeSelectOption } from "../components/ui/native-select";
import { api } from "../lib/api";
import { toneOf } from "../lib/categories";
import { shortId } from "../lib/format";
import { boundaryOf, modalityLabel } from "../lib/modality";
import { useModels } from "../queries";

type TargetKind = "model" | "agent";

const VOICE_MODALITIES = new Set<string>(["audio.transcription", "audio.speech"]);

// A combobox option whose displayed label differs from the selected value (agent name vs id,
// version label vs version string). The `{ value, label }` shape is what the combobox filters
// and displays natively.
interface PickItem {
  value: string;
  label: string;
}

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

  // Read the picked version's boundary from its IR — an audio/image/json boundary gets the matching
  // composer and payload shape (the run path is the same). Same query key as the bench and the
  // session-continuation surface, so all three share one cache entry.
  const agentVersion = useQuery({
    queryKey: ["agentver", agent?.id, version],
    queryFn: () => api.getAgentVersion(agent?.id ?? "", version),
    enabled: kind === "agent" && Boolean(agent?.id && version),
  });
  const boundary = boundaryOf(kind === "agent" ? agentVersion.data?.ir : undefined);
  const boundaryBadge = kind === "agent" ? modalityLabel(boundary) : null;
  // Until the version IR resolves we do not KNOW the boundary — offering the text composer would
  // let a voice agent's first turn go out as a bare string. Hold the composer instead of guessing.
  // A FAILED fetch is the same state, not a text agent: "we could not read it" must never read as
  // "it takes text", or the composer and the run body are both silently wrong.
  const boundaryUnknown =
    kind === "agent" &&
    Boolean(agent?.id && version) &&
    (agentVersion.isPending || agentVersion.isError);

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
      : agent && !boundaryUnknown
        ? { kind: "agent", agentId: agent.id, agentName: agent.name, version: version || undefined }
        : null;

  const runChat = useRunChat(runTarget, {
    placeholder: kind === "agent" ? "Message the agent…" : "Send a message…",
    boundary,
    disabledNote:
      kind === "agent"
        ? boundaryUnknown
          ? agentVersion.isError
            ? "This agent's version could not be read, so its input boundary is unknown — the error is above."
            : "Reading this agent's input boundary…"
          : "No published agents yet — build one on the canvas first."
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

  // Searchable picker options. Models select by logical id (which is also the label); agents and
  // versions carry a label distinct from the selected value.
  const modelItems = (models.data ?? []).map((m) => m.logicalId);
  const agentItems: PickItem[] = (agents.data ?? []).map((a) => ({ value: a.id, label: a.name }));
  const versionItems: PickItem[] = versions.map((v, i) => ({
    value: v.version,
    label: `v${v.version}${i === 0 ? " · latest" : ""} · ${shortId(v.content_hash, 10)}`,
  }));

  return (
    // Full-height chat layout: the picker header shrinks to content, the conversation card owns
    // the rest, and the composer stays pinned at the bottom of the card.
    <div className="flex h-full min-h-0 flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-lg font-semibold text-foreground">New Chat</h1>
        {/* What to talk to — a model or a published agent. This and the target below both freeze once
            the conversation starts (the session's target is fixed); "New chat" starts over. */}
        <NativeSelect
          className="w-32"
          value={kind}
          onChange={(e) => setKind(e.target.value as TargetKind)}
          disabled={started}
          aria-label="Target kind"
          title={started ? "The target is pinned once the conversation starts" : undefined}
        >
          <NativeSelectOption value="model">Model</NativeSelectOption>
          <NativeSelectOption value="agent">Agent</NativeSelectOption>
        </NativeSelect>
        {kind === "model" ? (
          <>
            <SearchableSelect
              options={modelItems.map((id) => ({ value: id, label: id }))}
              value={model}
              onChange={setPickedModel}
              placeholder="Select a model…"
              searchPlaceholder="Search models…"
              emptyText="No matching models."
              className="w-64"
              disabled={started}
              ariaLabel="Model"
              title={started ? "The model is pinned once the conversation starts" : undefined}
            />
            {modality !== "chat" && (
              <Badge variant="secondary" className={toneOf("violet").badge}>
                {modality}
              </Badge>
            )}
          </>
        ) : (
          <SearchableSelect
            options={agentItems}
            value={agent?.id ?? ""}
            onChange={setPickedAgent}
            placeholder="Select an agent…"
            searchPlaceholder="Search agents…"
            emptyText="No matching agents."
            className="w-64"
            disabled={started}
            ariaLabel="Agent"
            title={started ? "The agent is pinned once the conversation starts" : undefined}
          />
        )}
        {/* Which version to test — defaults to latest; pinned once the conversation starts. */}
        {kind === "agent" && versions.length > 0 && (
          <SearchableSelect
            options={versionItems}
            value={version}
            onChange={(v) => setPickedVersion(v)}
            placeholder="Version…"
            searchPlaceholder="Search versions…"
            emptyText="No matching versions."
            className="w-56"
            disabled={started}
            ariaLabel="Version"
            title={started ? "The version is pinned once the conversation starts" : undefined}
          />
        )}
        {boundaryBadge && (
          <Badge variant="secondary" className={toneOf("violet").badge}>
            {boundaryBadge}
          </Badge>
        )}
        {started && (
          <Button type="button" variant="ghost" onClick={onNewChat}>
            New chat
          </Button>
        )}
        {chat.sessionId && (
          <Link
            to="/sessions/$sessionId"
            params={{ sessionId: chat.sessionId }}
            className={`ml-auto text-xs ${linkClass}`}
            title={chat.sessionId}
          >
            session {shortId(chat.sessionId)}
          </Link>
        )}
      </div>
      {models.error && kind === "model" && <ErrorBanner error={models.error} />}
      {agents.error && kind === "agent" && <ErrorBanner error={agents.error} />}
      {/* A failed version fetch must not read as "an ordinary text agent" — the composer would be
          wrong and the run body with it. */}
      {agentVersion.error && kind === "agent" && <ErrorBanner error={agentVersion.error} />}
      <Card className="flex min-h-0 flex-1 flex-col overflow-visible">
        <CardContent className="flex min-h-0 flex-1 flex-col py-4">
          <ChatView controller={chat} listClassName="min-h-24 flex-1" emptyHint={null} />
        </CardContent>
      </Card>
    </div>
  );
}

export function Chat() {
  // Remount the surface to start a fresh conversation (new session, clean transcript).
  const [nonce, setNonce] = useState(0);
  return (
    <Page className="flex h-full min-h-0 flex-col">
      <ChatSurface key={nonce} onNewChat={() => setNonce((n) => n + 1)} />
    </Page>
  );
}
