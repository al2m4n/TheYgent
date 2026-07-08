// Speech-to-text panel — a chat-shaped tester: record from the microphone or attach an audio
// file, send, and the transcript comes back as the reply. Data plane direct (multipart → {text});
// metric = latency + real-time factor (audio-sec ÷ processing-sec) per exchange.

import { useState } from "react";
import { ChatView } from "../../chat/ChatView";
import { type CompletedTurn, useInferenceChat } from "../../chat/useInferenceChat";
import type { BenchRunInput } from "../../lib/api";
import { ParamForm } from "../ParamForm";
import { paramsForModality } from "../params";
import type { PanelProps } from "../registry";
import { MetricsView, SavePresetButton, SaveResultButton, useParams } from "../shared";

export function SttPanel({ logicalId, caps, binding, modelRef, onRecorded }: PanelProps) {
  const specs = paramsForModality("audio.transcription", caps);
  const form = useParams(specs);
  const [turns, setTurns] = useState<Record<string, CompletedTurn>>({});

  const chat = useInferenceChat({
    logicalId,
    modality: "audio.transcription",
    caps,
    params: form.params,
    onTurn: (id, turn) => setTurns((t) => ({ ...t, [id]: turn })),
  });

  const buildRecord = (turn: CompletedTurn): BenchRunInput => ({
    target_kind: "model",
    modality: "audio.transcription",
    logical_id: logicalId,
    model_ref: modelRef,
    binding,
    params: turn.params,
    metrics: turn.metrics,
    output: turn.output,
  });

  return (
    <div className="space-y-3">
      <ParamForm specs={specs} values={form.values} onChange={form.set} />
      <ChatView
        controller={chat}
        emptyHint="Record from the microphone or attach an audio file, then send to transcribe."
        renderExtras={(m) => {
          const turn = m.role === "assistant" ? turns[m.id] : undefined;
          if (!turn) return null;
          return (
            <div className="space-y-2 pt-1">
              <MetricsView metrics={turn.metrics} />
              <SaveResultButton build={() => buildRecord(turn)} onSaved={onRecorded} />
            </div>
          );
        }}
      />
      <SavePresetButton
        modality="audio.transcription"
        logicalId={logicalId}
        params={form.params}
        caps={caps}
      />
    </div>
  );
}
