// Text-to-speech panel (M18 §2.2) — text → audio playback + download; metric = TTFB (first audio
// byte), total latency, chars/sec. Data plane direct (json → bytes), §10.

import { useState } from "react";
import { Button, Field, Input } from "../../components/ui";
import type { BenchRunInput } from "../../lib/api";
import { ParamForm } from "../ParamForm";
import { speak } from "../dataplane";
import { type BenchMetrics, computeTtsMetrics } from "../metrics";
import { paramsForModality } from "../params";
import type { PanelProps } from "../registry";
import { MetricsView, SavePresetButton, SaveResultButton, useParams } from "../shared";

export function TtsPanel({ logicalId, caps, binding, modelRef, onRecorded }: PanelProps) {
  const specs = paramsForModality("audio.speech", caps);
  const form = useParams(specs);
  const [text, setText] = useState("");
  const [audioUrl, setAudioUrl] = useState<string | null>(null);
  const [metrics, setMetrics] = useState<BenchMetrics | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setRunning(true);
    setError(null);
    setAudioUrl(null);
    setMetrics(null);
    try {
      const { blob, ttfbMs, totalMs } = await speak(logicalId, text, form.params);
      setAudioUrl(URL.createObjectURL(blob));
      setMetrics(computeTtsMetrics(text.length, ttfbMs, totalMs));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  const buildRecord = (): BenchRunInput => ({
    target_kind: "model",
    modality: "audio.speech",
    logical_id: logicalId,
    model_ref: modelRef,
    binding,
    params: form.params,
    metrics: (metrics ?? {}) as Record<string, number>,
  });

  return (
    <div className="space-y-3">
      <Field label="Text">
        <Input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Text to speak…"
        />
      </Field>
      <ParamForm specs={specs} values={form.values} onChange={form.set} />
      <div className="flex items-center gap-2">
        <Button variant="primary" onClick={run} disabled={running || !text.trim()}>
          {running ? "Synthesizing…" : "Speak"}
        </Button>
        {metrics && <SaveResultButton build={buildRecord} onSaved={onRecorded} />}
      </div>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {audioUrl && (
        // biome-ignore lint/a11y/useMediaCaption: synthesized speech preview, no caption source
        <audio controls src={audioUrl} className="w-full" />
      )}
      {metrics && <MetricsView metrics={metrics} />}
      {metrics && (
        <SavePresetButton
          modality="audio.speech"
          logicalId={logicalId}
          params={form.params}
          caps={caps}
        />
      )}
    </div>
  );
}
