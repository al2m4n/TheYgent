// Embeddings panel (M18 §2.2) — embed one or many inputs, show vector dims, cosine similarity
// between two inputs. No contract change (§1.1 — /v1/embeddings already exists). Data plane direct.

import { useState } from "react";
import { Button, Field, Input } from "../../components/ui";
import type { BenchRunInput } from "../../lib/api";
import { ParamForm } from "../ParamForm";
import { embed } from "../dataplane";
import { type BenchMetrics, computeChatMetrics } from "../metrics";
import { paramsForModality } from "../params";
import type { PanelProps } from "../registry";
import { MetricsView, SavePresetButton, SaveResultButton, useParams } from "../shared";

function cosine(a: number[], b: number[]): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  return na && nb ? dot / (Math.sqrt(na) * Math.sqrt(nb)) : 0;
}

export function EmbeddingsPanel({ logicalId, caps, binding, modelRef, onRecorded }: PanelProps) {
  const specs = paramsForModality("embeddings", caps);
  const form = useParams(specs);
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [dims, setDims] = useState<number | null>(null);
  const [similarity, setSimilarity] = useState<number | null>(null);
  const [metrics, setMetrics] = useState<BenchMetrics | null>(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setRunning(true);
    setError(null);
    setSimilarity(null);
    setMetrics(null);
    const inputs = [a, b].filter((s) => s.trim());
    if (inputs.length === 0) return setRunning(false);
    const start = performance.now();
    try {
      const res = await embed(logicalId, inputs, form.params);
      const totalMs = performance.now() - start;
      const vectors = res.data.map((d) => d.embedding);
      setDims(vectors[0]?.length ?? null);
      if (vectors.length >= 2) setSimilarity(cosine(vectors[0], vectors[1]));
      setMetrics({ ...computeChatMetrics([{ atMs: totalMs, usage: res.usage }]), totalMs });
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  }

  const buildRecord = (): BenchRunInput => ({
    target_kind: "model",
    modality: "embeddings",
    logical_id: logicalId,
    model_ref: modelRef,
    binding,
    params: form.params,
    metrics: (metrics ?? {}) as Record<string, number>,
  });

  return (
    <div className="space-y-3">
      <Field label="Input A">
        <Input value={a} onChange={(e) => setA(e.target.value)} placeholder="first text" />
      </Field>
      <Field label="Input B (optional — for cosine similarity)">
        <Input value={b} onChange={(e) => setB(e.target.value)} placeholder="second text" />
      </Field>
      <ParamForm specs={specs} values={form.values} onChange={form.set} />
      <div className="flex items-center gap-2">
        <Button variant="primary" onClick={run} disabled={running || !a.trim()}>
          {running ? "Embedding…" : "Embed"}
        </Button>
        {metrics && <SaveResultButton build={buildRecord} onSaved={onRecorded} />}
      </div>
      {error && <p className="text-sm text-red-400">{error}</p>}
      {dims !== null && <p className="text-sm text-slate-300">Vector dims: {dims}</p>}
      {similarity !== null && (
        <p className="text-sm text-slate-300">Cosine similarity: {similarity.toFixed(4)}</p>
      )}
      {metrics && <MetricsView metrics={metrics} />}
      {metrics && (
        <SavePresetButton
          modality="embeddings"
          logicalId={logicalId}
          params={form.params}
          caps={caps}
        />
      )}
    </div>
  );
}
