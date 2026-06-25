// Chat / Vision panel (M18 §2.2) — the headline tester. Streams via the data plane (DIRECT to the
// inference base URL — §10), captures TTFT / tokens-sec / cost from the stream, and turns on image
// attach when the model advertises `vision` (the VLM path: image content blocks into the SAME chat
// endpoint — no separate endpoint, §1.3). Param form is schema-driven + capability-narrowed.

import { useMemo, useState } from "react";
import { Button, Field, Input } from "../../components/ui";
import type { BenchRunInput } from "../../lib/api";
import { DetectionOverlay, detectionsFromOutput } from "../DetectionOverlay";
import { ParamForm } from "../ParamForm";
import { streamChat } from "../dataplane";
import { type BenchMetrics, type ChatSample, computeChatMetrics } from "../metrics";
import { paramsForModality } from "../params";
import type { PanelProps } from "../registry";
import { MetricsView, SavePresetButton, SaveResultButton, useParams } from "../shared";

type Content = string | ({ type: string } & Record<string, unknown>)[];

export function ChatPanel({ logicalId, caps, binding, modelRef, onRecorded }: PanelProps) {
  const specs = paramsForModality("chat", caps);
  const form = useParams(specs);
  const [system, setSystem] = useState("");
  const [prompt, setPrompt] = useState("");
  const [imageUrl, setImageUrl] = useState("");
  const [output, setOutput] = useState("");
  const [metrics, setMetrics] = useState<BenchMetrics | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setRunning(true);
    setError(null);
    setOutput("");
    setMetrics(null);
    const messages: { role: string; content: Content }[] = [];
    if (system.trim()) messages.push({ role: "system", content: system });
    // A vision attach (image URL) builds OpenAI image content blocks (§1.3); else plain text.
    const userContent: Content =
      caps?.vision && imageUrl.trim()
        ? [
            { type: "text", text: prompt },
            { type: "image_url", image_url: { url: imageUrl.trim() } },
          ]
        : prompt;
    messages.push({ role: "user", content: userContent });

    const start = performance.now();
    const samples: ChatSample[] = [];
    let acc = "";
    try {
      for await (const chunk of streamChat(logicalId, messages, form.params)) {
        const atMs = performance.now() - start;
        const delta = chunk.choices?.[0]?.delta?.content ?? "";
        if (delta) acc += delta;
        samples.push({ atMs, content: delta || undefined, usage: chunk.usage });
        if (delta) setOutput(acc);
      }
      setMetrics(computeChatMetrics(samples));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  // §2.2 grounding overlay (no contract change): when a VLM emits bounding boxes as structured output
  // (JSON detections), draw them on the attached image — through the SAME component the tool tester
  // uses for classic-CV detections (§2.6). A plain-text answer parses to no boxes and renders nothing.
  const grounding = useMemo(
    () => (caps?.vision && imageUrl.trim() ? detectionsFromOutput(output) : []),
    [caps?.vision, imageUrl, output],
  );

  const buildRecord = (): BenchRunInput => ({
    target_kind: "model",
    modality: caps?.vision && imageUrl ? "vision" : "chat",
    logical_id: logicalId,
    model_ref: modelRef,
    binding,
    params: form.params,
    metrics: (metrics ?? {}) as Record<string, number>,
    output,
  });

  return (
    <div className="space-y-3">
      <Field label="System (optional)">
        <Input value={system} onChange={(e) => setSystem(e.target.value)} placeholder="You are…" />
      </Field>
      <Field label="Prompt">
        <Input
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Ask something…"
        />
      </Field>
      {caps?.vision && (
        <Field label="Image URL (vision)">
          <Input
            value={imageUrl}
            onChange={(e) => setImageUrl(e.target.value)}
            placeholder="https://… or data:…"
          />
        </Field>
      )}
      <ParamForm specs={specs} values={form.values} onChange={form.set} />
      <div className="flex items-center gap-2">
        <Button variant="primary" onClick={run} disabled={running || !prompt.trim()}>
          {running ? "Running…" : "Run"}
        </Button>
        {metrics && <SaveResultButton build={buildRecord} onSaved={onRecorded} />}
      </div>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {metrics && <MetricsView metrics={metrics} />}
      {grounding.length > 0 && imageUrl.trim() && (
        <DetectionOverlay src={imageUrl.trim()} detections={grounding} />
      )}
      {output && (
        <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-[#0e131c] p-3 text-sm text-slate-200">
          {output}
        </pre>
      )}
      {metrics && (
        <SavePresetButton modality="chat" logicalId={logicalId} params={form.params} caps={caps} />
      )}
    </div>
  );
}
