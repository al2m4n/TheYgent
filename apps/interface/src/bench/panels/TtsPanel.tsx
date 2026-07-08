// Text-to-speech panel — chat-shaped: type text, send, and the synthesized audio comes back as a
// playable reply bubble. Data plane direct (json → bytes); metric = TTFB (first audio byte),
// total latency, chars/sec per exchange.

import { useState } from "react";
import { ChatView } from "../../chat/ChatView";
import { type CompletedTurn, useInferenceChat } from "../../chat/useInferenceChat";
import type { BenchRunInput } from "../../lib/api";
import { ParamForm } from "../ParamForm";
import { paramsForModality } from "../params";
import type { PanelProps } from "../registry";
import { MetricsView, SavePresetButton, SaveResultButton, useParams } from "../shared";

export function TtsPanel({ logicalId, caps, binding, modelRef, onRecorded }: PanelProps) {
  const specs = paramsForModality("audio.speech", caps);
  const form = useParams(specs);
  const [turns, setTurns] = useState<Record<string, CompletedTurn>>({});

  const chat = useInferenceChat({
    logicalId,
    modality: "audio.speech",
    caps,
    params: form.params,
    onTurn: (id, turn) => setTurns((t) => ({ ...t, [id]: turn })),
  });

  const buildRecord = (turn: CompletedTurn): BenchRunInput => ({
    target_kind: "model",
    modality: "audio.speech",
    logical_id: logicalId,
    model_ref: modelRef,
    binding,
    params: turn.params,
    metrics: turn.metrics,
  });

  return (
    <div className="space-y-3">
      <ParamForm specs={specs} values={form.values} onChange={form.set} />
      <ChatView
        controller={chat}
        emptyHint="Type text and send — the reply is the synthesized audio."
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
        modality="audio.speech"
        logicalId={logicalId}
        params={form.params}
        caps={caps}
      />
    </div>
  );
}
