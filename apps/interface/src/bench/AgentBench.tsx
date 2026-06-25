// Agent bench (M18 §2.3/§2.7) — the body of the per-agent bench MODAL opened from an Agents row.
// Reuses M11 invoke (no new run path, §1.5): pin a version, run an input, read the persisted output,
// render the run trace as a per-node waterfall AND overlay it on the canvas (hover a span → the node
// flashes, via the span.name == node.id join). Degrades to output-only if observability is absent.
// The tune→ship loop (§2.7): apply a saved preset's LITERAL params to a binding and save a new
// version through M11 (the server hashes; the preset name never enters the IR — §1.7).

import { useQuery } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { useState } from "react";
import type { Selection } from "../adapter";
import { GraphCanvas } from "../components/GraphCanvas";
import { Button, Card, Field, Input, Select } from "../components/ui";
import { type AgentDetail, api } from "../lib/api";
import { applyPresetToBinding } from "./preset";

export function AgentBench({ agent }: { agent: AgentDetail }) {
  const [version, setVersion] = useState(agent.versions[0]?.version ?? "");
  const stored = useQuery({
    queryKey: ["agentver", agent.id, version],
    queryFn: () => api.getAgentVersion(agent.id, version),
    enabled: Boolean(version),
  });
  const [input, setInput] = useState("");
  const [result, setResult] = useState<{ runId: string; output?: string; error?: string } | null>(
    null,
  );
  const [highlight, setHighlight] = useState<Selection>(null);
  const [running, setRunning] = useState(false);

  const trace = useQuery({
    queryKey: ["trace", result?.runId],
    queryFn: () => api.getRunTrace(result?.runId ?? ""),
    enabled: Boolean(result?.runId),
    retry: false, // observability may be absent → degrade, don't hammer
  });

  async function run() {
    setRunning(true);
    setResult(null);
    setHighlight(null);
    try {
      setResult(await api.runAgent(agent.id, { input, version }));
    } catch (e) {
      setResult({ runId: "", error: e instanceof Error ? e.message : String(e) });
    } finally {
      setRunning(false);
    }
  }

  const ir = stored.data?.ir as IRDocument | undefined;
  const nodeSpans = (trace.data?.spans ?? []).filter((s) => s.node_id && !s.phase);

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-slate-200">{agent.name}</span>
        <Field label="Pin">
          <Select value={version} onChange={(e) => setVersion(e.target.value)} className="w-48">
            {agent.versions.map((v) => (
              <option key={v.version} value={v.version}>
                v{v.version} · {v.content_hash.slice(0, 12)}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <Field label="Input">
        <Input value={input} onChange={(e) => setInput(e.target.value)} placeholder="run input…" />
      </Field>
      <Button variant="primary" onClick={run} disabled={running}>
        {running ? "Running…" : "Run"}
      </Button>

      {result?.error && <p className="text-sm text-red-400">{result.error}</p>}
      {result?.output && (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-[#0e131c] p-3 text-sm text-slate-200">
          {result.output}
        </pre>
      )}
      {result?.runId && trace.isError && (
        <p className="text-xs text-slate-500">
          Trace unavailable (observability not enabled) — showing persisted output only.
        </p>
      )}
      {nodeSpans.length > 0 && (
        <div className="grid grid-cols-2 gap-4">
          <Waterfall spans={nodeSpans} onHover={setHighlight} />
          {ir && (
            <div className="h-64 overflow-hidden rounded-md border border-slate-800">
              <GraphCanvas
                ir={ir}
                onChange={() => {}}
                selection={null}
                onSelect={() => {}}
                highlight={highlight}
              />
            </div>
          )}
        </div>
      )}

      {ir && <ApplyPreset agentId={agent.id} ir={ir} />}
    </div>
  );
}

function Waterfall({
  spans,
  onHover,
}: {
  spans: {
    id: string;
    node_id?: string | null;
    name: string;
    start_ns: number;
    end_ns?: number | null;
    status: string;
  }[];
  onHover: (s: Selection) => void;
}) {
  const t0 = Math.min(...spans.map((s) => s.start_ns));
  const tEnd = Math.max(...spans.map((s) => s.end_ns ?? s.start_ns));
  const span = Math.max(tEnd - t0, 1);
  return (
    <div className="space-y-1" data-testid="agent-waterfall">
      {spans.map((s) => {
        const left = ((s.start_ns - t0) / span) * 100;
        const width = (((s.end_ns ?? s.start_ns) - s.start_ns) / span) * 100;
        return (
          <button
            type="button"
            key={s.id}
            className="block w-full text-left"
            onMouseEnter={() => onHover(s.node_id ? { kind: "node", id: s.node_id } : null)}
            onMouseLeave={() => onHover(null)}
          >
            <span className="text-[11px] text-slate-400">{s.name}</span>
            <span className="block h-2 rounded bg-slate-800">
              <span
                className="block h-full rounded bg-blue-500"
                style={{ marginLeft: `${left}%`, width: `${Math.max(width, 2)}%` }}
              />
            </span>
          </button>
        );
      })}
    </div>
  );
}

// Apply a saved preset's LITERAL params to one of this agent's bindings, then save a new version
// through M11 (§2.7 / §1.7). Modality is matched so a chat preset can't land on a TTS binding.
function ApplyPreset({ agentId, ir }: { agentId: string; ir: IRDocument }) {
  const presets = useQuery({ queryKey: ["presets"], queryFn: () => api.listPresets() });
  const bindings = Object.keys(ir.models ?? {});
  const [presetId, setPresetId] = useState("");
  const [binding, setBinding] = useState(bindings[0] ?? "");
  const [status, setStatus] = useState<string | null>(null);

  if (!presets.data || presets.data.length === 0) return null;

  async function apply() {
    const preset = presets.data?.find((p) => p.id === presetId);
    if (!preset || !binding) return;
    setStatus(null);
    try {
      const next = applyPresetToBinding(ir, binding, preset.params);
      const updated = await api.addAgentVersion(agentId, { ir: next });
      setStatus(`applied → new ${updated.versions[0]?.content_hash.slice(0, 16)}`);
    } catch (e) {
      setStatus(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <Card className="space-y-2 border-t border-slate-800">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Apply a preset</p>
      <div className="flex flex-wrap items-center gap-2">
        <Select value={presetId} onChange={(e) => setPresetId(e.target.value)} className="w-48">
          <option value="">preset…</option>
          {presets.data.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.modality})
            </option>
          ))}
        </Select>
        <Select value={binding} onChange={(e) => setBinding(e.target.value)} className="w-40">
          {bindings.map((b) => (
            <option key={b} value={b}>
              {b}
            </option>
          ))}
        </Select>
        <Button variant="ghost" onClick={apply} disabled={!presetId || !binding}>
          Apply → new version
        </Button>
      </div>
      {status && <span className="text-xs text-slate-400">{status}</span>}
    </Card>
  );
}
