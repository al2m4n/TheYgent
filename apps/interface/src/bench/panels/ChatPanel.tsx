// Chat / Vision panel — the headline tester: a real conversation through the unified chat
// shell. Streams via the data plane (DIRECT to the inference base URL) with multi-turn replay,
// separates a reasoning model's thinking into the collapsible block, and turns on image attach
// (upload / camera / URL-less) when the model advertises `vision` — image content blocks into the
// SAME chat endpoint, no separate endpoint. Every exchange carries its own metrics; any exchange
// can be saved to the bench store. Param form stays schema-driven + capability-narrowed.

import { useState } from "react";
import { ChatView } from "../../chat/ChatView";
import type { ChatMessage } from "../../chat/types";
import { type CompletedTurn, useInferenceChat } from "../../chat/useInferenceChat";
import { Field, Textarea } from "../../components/ui";
import type { BenchRunInput } from "../../lib/api";
import { DetectionOverlay, detectionsFromOutput } from "../DetectionOverlay";
import { ParamForm } from "../ParamForm";
import { paramsForModality } from "../params";
import type { PanelProps } from "../registry";
import { MetricsView, SavePresetButton, SaveResultButton, useParams } from "../shared";

/** The image the exchange was about — the nearest user image before this assistant message. */
function precedingImageUrl(messages: ChatMessage[], assistantId: string): string | undefined {
  const idx = messages.findIndex((m) => m.id === assistantId);
  for (let i = idx - 1; i >= 0; i--) {
    const img = messages[i].attachments?.find((a) => a.kind === "image");
    if (messages[i].role === "user" && img) return img.url;
  }
  return undefined;
}

export function ChatPanel({ logicalId, caps, binding, modelRef, onRecorded }: PanelProps) {
  const specs = paramsForModality("chat", caps);
  const form = useParams(specs);
  const [system, setSystem] = useState("");
  const [turns, setTurns] = useState<Record<string, CompletedTurn>>({});

  const chat = useInferenceChat({
    logicalId,
    modality: "chat",
    caps,
    params: form.params,
    system,
    onTurn: (id, turn) => setTurns((t) => ({ ...t, [id]: turn })),
  });

  const buildRecord = (turn: CompletedTurn): BenchRunInput => ({
    target_kind: "model",
    modality: turn.modality,
    logical_id: logicalId,
    model_ref: modelRef,
    binding,
    params: turn.params,
    metrics: turn.metrics,
    output: turn.output,
  });

  return (
    <div className="space-y-3">
      <Field label="System (optional)">
        <Textarea
          rows={2}
          value={system}
          onChange={(e) => setSystem(e.target.value)}
          placeholder="You are…"
        />
      </Field>
      <ParamForm specs={specs} values={form.values} onChange={form.set} />
      <ChatView
        controller={chat}
        emptyHint="Ask something — the conversation replays on every turn."
        renderExtras={(m) => {
          const turn = m.role === "assistant" ? turns[m.id] : undefined;
          if (!turn) return null;
          // Grounding overlay: when a VLM answers with structured detections (JSON boxes), draw
          // them over the image the exchange was about. A plain-text answer renders nothing.
          const img =
            turn.modality === "vision" ? precedingImageUrl(chat.messages, m.id) : undefined;
          const boxes = img ? detectionsFromOutput(turn.output) : [];
          return (
            <div className="space-y-2 pt-1">
              <MetricsView metrics={turn.metrics} />
              {img && boxes.length > 0 && <DetectionOverlay src={img} detections={boxes} />}
              <SaveResultButton build={() => buildRecord(turn)} onSaved={onRecorded} />
            </div>
          );
        }}
      />
      <SavePresetButton modality="chat" logicalId={logicalId} params={form.params} caps={caps} />
    </div>
  );
}
