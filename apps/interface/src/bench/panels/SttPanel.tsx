// Speech-to-text panel (M18 §2.2) — upload audio → transcript; metric = latency + real-time factor
// (audio-sec ÷ processing-sec). Data plane direct (multipart → {text}), §10.

import { useRef, useState } from "react";
import { Button, Field } from "../../components/ui";
import type { BenchRunInput } from "../../lib/api";
import { ParamForm } from "../ParamForm";
import { transcribe } from "../dataplane";
import { type BenchMetrics, computeSttMetrics } from "../metrics";
import { type ParamSpec, coerceParam, paramsForModality } from "../params";
import type { PanelProps } from "../registry";
import { MetricsView, SaveResultButton, useParams } from "../shared";

// STT params are string form fields forwarded to the multipart body.
function stringParams(specs: ParamSpec[], values: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const spec of specs) {
    const v = coerceParam(spec, values[spec.key] ?? "");
    if (v !== undefined) out[spec.key] = String(v);
  }
  return out;
}

export function SttPanel({ logicalId, caps, binding, modelRef, onRecorded }: PanelProps) {
  const specs = paramsForModality("audio.transcription", caps);
  const form = useParams(specs);
  const [file, setFile] = useState<File | null>(null);
  const [audioSec, setAudioSec] = useState(0);
  const [text, setText] = useState("");
  const [metrics, setMetrics] = useState<BenchMetrics | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  async function run() {
    if (!file) return;
    setRunning(true);
    setError(null);
    setText("");
    setMetrics(null);
    const start = performance.now();
    try {
      const res = await transcribe(logicalId, file, stringParams(specs, form.values));
      const processingMs = performance.now() - start;
      setText(res.text);
      setMetrics(computeSttMetrics(audioSec, processingMs));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  const buildRecord = (): BenchRunInput => ({
    target_kind: "model",
    modality: "audio.transcription",
    logical_id: logicalId,
    model_ref: modelRef,
    binding,
    params: form.params,
    metrics: (metrics ?? {}) as Record<string, number>,
    output: text,
  });

  return (
    <div className="space-y-3">
      <Field label="Audio file">
        <input
          type="file"
          accept="audio/*"
          className="text-sm text-slate-300"
          onChange={(e) => {
            const f = e.target.files?.[0] ?? null;
            setFile(f);
            if (f && audioRef.current) audioRef.current.src = URL.createObjectURL(f);
          }}
        />
      </Field>
      {/* hidden audio element used only to read duration for the RTF metric */}
      <audio
        ref={audioRef}
        className="hidden"
        onLoadedMetadata={(e) => setAudioSec(e.currentTarget.duration || 0)}
      >
        <track kind="captions" />
      </audio>
      <ParamForm specs={specs} values={form.values} onChange={form.set} />
      <div className="flex items-center gap-2">
        <Button variant="primary" onClick={run} disabled={running || !file}>
          {running ? "Transcribing…" : "Transcribe"}
        </Button>
        {metrics && <SaveResultButton build={buildRecord} onSaved={onRecorded} />}
      </div>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {text && (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-[var(--c-surface)] p-3 text-sm text-slate-200">
          {text}
        </pre>
      )}
      {metrics && <MetricsView metrics={metrics} />}
    </div>
  );
}
