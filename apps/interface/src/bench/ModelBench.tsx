// Model bench — the body of the per-model bench MODAL opened from a Registries row.
// Capability-routed: probe `capabilities`, render the panel(s) the model's `modalities` declare
// (data-driven), capture metrics, and compare two recorded results side by side (the binding-swap
// headline). No model picker here — the model is the one whose row opened the modal.

import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Badge, Button, Empty, ErrorBanner, Select, Spinner } from "../components/ui";
import { type BenchRunRecord, type ModelView, api } from "../lib/api";
import { engineTone } from "../lib/categories";
import { shortId } from "../lib/format";
import { notify } from "../lib/notify";
import { PANEL_REGISTRY, type PanelProps, panelsFor } from "./registry";

export function ModelBench({ model }: { model: ModelView }) {
  const caps = useQuery({
    queryKey: ["caps", model.logicalId],
    queryFn: () => api.getModelCapabilities(model.logicalId),
  });
  const [warming, setWarming] = useState(false);
  const [warmed, setWarmed] = useState(false);
  const [recorded, setRecorded] = useState<BenchRunRecord[]>([]);

  const panels = panelsFor(caps.data?.modalities);
  const binding = model.binding.binding;
  const modelRef = typeof model.binding.model === "string" ? model.binding.model : undefined;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="mono text-sm text-slate-200">{model.logicalId}</span>
        <Badge tone={engineTone(binding)}>{binding}</Badge>
        {caps.data?.modalities?.map((m) => (
          <Badge key={m} tone="green">
            {m}
          </Badge>
        ))}
        {caps.data?.maxContext ? (
          <Badge tone="slate">{caps.data.maxContext.toLocaleString()} ctx</Badge>
        ) : null}
        {caps.data?.approximate && <Badge tone="amber">approximate</Badge>}
        <Button
          variant="ghost"
          className="ml-auto"
          disabled={warming}
          onClick={async () => {
            setWarming(true);
            setWarmed(false);
            try {
              await api.warmModel(model.logicalId);
              setWarmed(true);
            } catch (e) {
              // Surface a failed warm — a silent revert to "Warm" reads exactly like success.
              notify.error(e instanceof Error ? e.message : String(e));
            } finally {
              setWarming(false);
            }
          }}
        >
          {warming ? "Warming…" : warmed ? "Warmed ✓" : "Warm"}
        </Button>
      </div>

      {caps.isLoading && <Spinner label="Probing capabilities…" />}
      {caps.error && <ErrorBanner error={caps.error} />}
      {caps.data && panels.length === 0 && (
        <Empty>No compatible tester for this model's modalities.</Empty>
      )}

      {panels.map((modality) => {
        const Panel = PANEL_REGISTRY[modality];
        const props: PanelProps = {
          logicalId: model.logicalId,
          caps: caps.data,
          binding,
          modelRef,
          onRecorded: (rec) => setRecorded((r) => [...r, rec]),
        };
        return (
          <div key={modality} className="space-y-2 border-t border-slate-800 pt-4">
            <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">
              {modality}
            </p>
            <Panel {...props} />
          </div>
        );
      })}

      <CompareView recorded={recorded} />
    </div>
  );
}

// ── compare (binding-swap / A-B) — over the results recorded in THIS modal session ───────────────

function CompareView({ recorded }: { recorded: BenchRunRecord[] }) {
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  useEffect(() => {
    // Default to the two most-recent recordings (the common "ran A, ran B, compare" flow).
    if (recorded.length >= 2) {
      setA((cur) => cur || recorded[recorded.length - 2].id);
      setB((cur) => cur || recorded[recorded.length - 1].id);
    }
  }, [recorded]);
  const compare = useQuery({
    queryKey: ["compare", a, b],
    queryFn: () => api.compareBenchRuns(a, b),
    enabled: Boolean(a && b && a !== b),
  });

  if (recorded.length < 2) {
    return (
      <p className="border-t border-slate-800 pt-3 text-xs text-slate-500">
        Save two results (e.g. the same prompt under two bindings) to compare them side by side.
      </p>
    );
  }
  const opt = (r: BenchRunRecord) =>
    `${r.label ?? r.modality} · ${r.binding ?? r.logical_id} · ${shortId(r.id)}`;
  return (
    <div className="space-y-3 border-t border-slate-800 pt-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Compare</p>
      <div className="grid grid-cols-2 gap-3">
        <Select value={a} onChange={(e) => setA(e.target.value)} aria-label="Compare result A">
          <option value="">A…</option>
          {recorded.map((r) => (
            <option key={r.id} value={r.id}>
              {opt(r)}
            </option>
          ))}
        </Select>
        <Select value={b} onChange={(e) => setB(e.target.value)} aria-label="Compare result B">
          <option value="">B…</option>
          {recorded.map((r) => (
            <option key={r.id} value={r.id}>
              {opt(r)}
            </option>
          ))}
        </Select>
      </div>
      {a && b && a === b && (
        <p className="text-xs text-slate-500">Pick two different results to compare.</p>
      )}
      {compare.isLoading && <Spinner label="Comparing…" />}
      {compare.error && <ErrorBanner error={compare.error} />}
      {compare.data && (
        <div className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-sm">
          {Object.entries(compare.data.metric_deltas).map(([k, v]) => (
            <div key={k} className="contents">
              <span className="text-slate-400">{k} (B−A)</span>
              <span className={deltaClass(k, v)}>
                {v > 0 ? "+" : ""}
                {Math.round(v * 1000) / 1000}
              </span>
            </div>
          ))}
          <span className="text-slate-400">outputs match</span>
          <span>{compare.data.outputs_match ? "yes" : "no"}</span>
        </div>
      )}
    </div>
  );
}

// Metrics where a bigger number is the improvement (throughput, real-time factor) — everything else
// (latencies, cost, token counts) improves downward. Zero is neutral, never "worse".
const HIGHER_IS_BETTER = new Set(["tokensPerSec", "charsPerSec", "rtf"]);

function deltaClass(metric: string, delta: number): string {
  if (delta === 0) return "text-slate-400";
  const good = HIGHER_IS_BETTER.has(metric) ? delta > 0 : delta < 0;
  return good ? "text-emerald-700 dark:text-emerald-300" : "text-amber-700 dark:text-amber-300";
}
