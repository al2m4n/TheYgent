// Shared bits across the modality panels (M18 §2.1/§2.4/§2.7): the metrics readout, the param-value
// hook, "save result" (record metrics to the bench store), and "save as preset" (a literal param
// snippet — §1.7). Kept here so each panel stays a thin modality-specific shell.

import { useState } from "react";
import { Badge, Button, Input } from "../components/ui";
import { api } from "../lib/api";
import type { BenchRunInput, BenchRunRecord, Capabilities } from "../lib/api";
import type { BenchMetrics } from "./metrics";
import { type ParamSpec, coerceParam } from "./params";

/** Hold raw string form state; expose the coerced, wire-ready param object. */
export function useParams(specs: ParamSpec[]) {
  const [values, setValues] = useState<Record<string, string>>({});
  const set = (key: string, raw: string) => setValues((v) => ({ ...v, [key]: raw }));
  const built: Record<string, unknown> = {};
  for (const spec of specs) {
    const coerced = coerceParam(spec, values[spec.key] ?? "");
    if (coerced !== undefined) built[spec.key] = coerced;
  }
  return { values, set, params: built };
}

const FMT: Record<string, (n: number) => string> = {
  ttftMs: (n) => `${Math.round(n)} ms`,
  totalMs: (n) => `${Math.round(n)} ms`,
  ttfbMs: (n) => `${Math.round(n)} ms`,
  tokensPerSec: (n) => `${n.toFixed(1)} tok/s`,
  promptTokens: (n) => String(n),
  completionTokens: (n) => String(n),
  cost: (n) => `$${n.toFixed(5)}`,
  rtf: (n) => `${n.toFixed(1)}×`,
};
const LABEL: Record<string, string> = {
  ttftMs: "TTFT",
  totalMs: "Total",
  ttfbMs: "TTFB",
  tokensPerSec: "Throughput",
  promptTokens: "Prompt tok",
  completionTokens: "Completion tok",
  cost: "Cost",
  rtf: "Real-time factor",
};

export function MetricsView({ metrics }: { metrics: BenchMetrics }) {
  const entries = Object.entries(metrics).filter(([, v]) => typeof v === "number") as [
    string,
    number,
  ][];
  if (entries.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2" data-testid="metrics">
      {entries.map(([k, v]) => (
        <Badge key={k} tone="blue">
          <span className="text-slate-400">{LABEL[k] ?? k}:</span> {(FMT[k] ?? String)(v)}
        </Badge>
      ))}
    </div>
  );
}

/** Record a captured result to the bench store (metrics + digests; capture opt-in — §1.6). */
export function SaveResultButton({
  build,
  onSaved,
}: {
  build: () => BenchRunInput;
  onSaved?: (rec: BenchRunRecord) => void;
}) {
  const [saved, setSaved] = useState<BenchRunRecord | null>(null);
  return (
    <Button
      variant="ghost"
      onClick={async () => {
        const rec = await api.recordBenchRun(build());
        setSaved(rec);
        onSaved?.(rec);
      }}
    >
      {saved ? "Saved ✓" : "Save result"}
    </Button>
  );
}

/** Save the current params as a named, modality-scoped LITERAL preset (§1.7). */
export function SavePresetButton({
  modality,
  logicalId,
  params,
  caps,
}: {
  modality: string;
  logicalId: string;
  params: Record<string, unknown>;
  caps?: Capabilities;
}) {
  void caps;
  const [name, setName] = useState("");
  const [saved, setSaved] = useState(false);
  return (
    <div className="flex items-center gap-2">
      <Input
        placeholder="preset name"
        value={name}
        onChange={(e) => {
          setName(e.target.value);
          setSaved(false);
        }}
        className="w-40"
      />
      <Button
        variant="ghost"
        disabled={!name.trim()}
        onClick={async () => {
          await api.createPreset({ name: name.trim(), modality, logical_id: logicalId, params });
          setSaved(true);
        }}
      >
        {saved ? "Preset saved ✓" : "Save as preset"}
      </Button>
    </div>
  );
}
