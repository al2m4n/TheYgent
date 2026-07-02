// Agent bench (M18 §2.3/§2.7) — the body of the per-agent bench MODAL opened from an Agents row.
// Reuses M11 invoke (no new run path, §1.5): pin a version, run an input, read the persisted output,
// render the run trace as a per-node waterfall AND overlay it on the canvas (hover a span → the node
// flashes, via the span.name == node.id join). Degrades to output-only if observability is absent.
// The tune→ship loop (§2.7): apply a saved preset's LITERAL params to a binding and save a new
// version through M11 (the server hashes; the preset name never enters the IR — §1.7).

import { useQuery } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import type { Selection } from "../adapter";
import { GraphCanvas } from "../components/GraphCanvas";
import { Button, Card, Field, Input, Select } from "../components/ui";
import { type AgentDetail, ApiError, api } from "../lib/api";
import { isDurableOnly } from "../lib/durable";
import { useRun } from "../queries";
import { TraceWaterfall } from "./TraceWaterfall";
import { applyPresetToBinding } from "./preset";

export function AgentBench({ agent }: { agent: AgentDetail }) {
  // Default the pin to the LATEST version (the detail endpoint returns versions seq-desc, newest
  // first) and KEEP following it as fresh data arrives — until the user deliberately picks one.
  // Capturing `versions[0]` once in useState was the "model change didn't apply" bug: if react-query
  // served stale cached detail at mount (then re-fetched the new version), the pin stuck to the old
  // version. `picked ?? latest` recomputes from the freshest `agent` every render.
  const latest = agent.versions[0]?.version ?? "";
  const [picked, setPicked] = useState<string | null>(null);
  const version = picked ?? latest;
  const stored = useQuery({
    queryKey: ["agentver", agent.id, version],
    queryFn: () => api.getAgentVersion(agent.id, version),
    enabled: Boolean(version),
  });
  const [input, setInput] = useState("");
  const [result, setResult] = useState<{ runId: string; output?: string; error?: string } | null>(
    null,
  );
  // The durable path enqueues a run and polls its id (the endpoint returns no terminal result).
  const [durableRunId, setDurableRunId] = useState<string | null>(null);
  // Set when a durable run is attempted but the server isn't in durable mode (400 durable_required).
  const [durableUnavailable, setDurableUnavailable] = useState(false);
  const [highlight, setHighlight] = useState<Selection>(null);
  const [running, setRunning] = useState(false);

  const ir = stored.data?.ir as IRDocument | undefined;
  // A durable-only agent (loop/map/subgraph/human) can ONLY run durably. Any other agent can run
  // either way — a normal streaming run, or a durable run that checkpoints each step and resumes
  // after a crash — so we offer both.
  const durableOnly = ir ? isDurableOnly(ir) : false;

  // Poll the durable run to completion (the endpoint returns a run id, not a terminal result).
  const durablePoll = useRun(durableRunId ?? "", { live: true, enabled: Boolean(durableRunId) });
  const durableRun = durablePoll.data;
  const durableTerminal = durableRun?.status === "completed" || durableRun?.status === "failed";

  async function run(durable: boolean) {
    setRunning(true);
    setResult(null);
    setDurableRunId(null);
    setDurableUnavailable(false);
    setHighlight(null);
    try {
      if (durable) {
        const { run_id } = await api.runAgentDurable(agent.id, { input, version });
        setDurableRunId(run_id);
      } else {
        setResult(await api.runAgent(agent.id, { input, version }));
      }
    } catch (e) {
      // Only the durable endpoint's 400 means "server isn't in durable mode" — surface that as an
      // amber config note, not a scary red error. Any other failure is a real run error.
      if (durable && e instanceof ApiError && e.code === "durable_required") {
        setDurableUnavailable(true);
      } else {
        setResult({ runId: "", error: e instanceof Error ? e.message : String(e) });
      }
    } finally {
      setRunning(false);
    }
  }

  // One result view over both paths: the interactive terminal result, or the durable poll's Run.
  const view = durableRunId
    ? {
        runId: durableRunId,
        status: durableRun?.status as string | undefined,
        output: durableRun?.output ?? undefined,
        error: durableRun?.error ?? undefined,
      }
    : result
      ? {
          runId: result.runId,
          status: undefined as string | undefined,
          output: result.output,
          error: result.error,
        }
      : null;
  // A durable run keeps the button busy until the poll reaches a terminal status.
  const busy = running || (Boolean(durableRunId) && !durableTerminal);
  const inProgress = view?.status && view.status !== "completed" && view.status !== "failed";

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-slate-200">{agent.name}</span>
        <Field label="Pin">
          <Select value={version} onChange={(e) => setPicked(e.target.value)} className="w-48">
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
      <div className="flex items-center gap-2">
        {durableOnly ? (
          // Durable-only agents can't run any other way — a single button, no choice.
          <>
            <Button variant="primary" onClick={() => run(true)} disabled={busy}>
              {busy ? "Running…" : "Durable-Run"}
            </Button>
            <span className="text-[11px] text-slate-500">
              durable-only — runs on the durable runtime
            </span>
          </>
        ) : (
          // Any other agent can run either way — the primary button opens a menu to pick a normal
          // streaming run or a durable run (which checkpoints each step and resumes after a crash).
          <RunMenu busy={busy} onRun={run} />
        )}
      </div>

      {durableUnavailable && (
        <div className="rounded-md border border-amber-700/50 bg-amber-950/30 px-2.5 py-2 text-[11px] leading-relaxed text-amber-200/90">
          The control-plane isn’t running in durable mode — start it with{" "}
          <span className="mono">THEYGENT_DURABLE=1</span> to run durably.
          {!durableOnly && " You can still use Run for a normal (non-resumable) run."}
        </div>
      )}

      {inProgress && (
        <p className="text-sm text-slate-400">
          {view?.status === "waiting"
            ? `Paused — awaiting ${durableRun?.awaiting_node ?? "input"} (resume not yet wired)`
            : `Running… (${view?.status})`}
        </p>
      )}
      {view?.error && <p className="text-sm text-red-400">{view.error}</p>}
      {view?.output && (
        <pre className="max-h-48 overflow-auto whitespace-pre-wrap rounded-md border border-slate-800 bg-[var(--c-surface)] p-3 text-sm text-slate-200">
          {view.output}
        </pre>
      )}
      {view?.runId && (
        <div className="space-y-3">
          <TraceWaterfall runId={view.runId} onHover={setHighlight} />
          {ir && (
            <div className="h-64 overflow-hidden rounded-md border border-slate-800">
              <GraphCanvas
                ir={ir}
                onChange={() => {}}
                selection={null}
                onSelect={() => {}}
                highlight={highlight}
                minimal
              />
            </div>
          )}
        </div>
      )}

      {ir && <ApplyPreset agentId={agent.id} ir={ir} />}
    </div>
  );
}

// The Run button as a dropdown: the primary button opens a menu to pick a normal streaming run or a
// durable run. Used for agents that can run either way (a durable-only agent gets a single button).
// The menu is PORTALED to the body with fixed positioning so the scrollable bench modal (overflow-
// auto) can't clip it.
function RunMenu({ busy, onRun }: { busy: boolean; onRun: (durable: boolean) => void }) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  const toggle = () => {
    const r = wrapRef.current?.getBoundingClientRect();
    if (r) setPos({ left: r.left, top: r.bottom + 4 });
    setOpen((o) => !o);
  };

  useEffect(() => {
    if (!open) return;
    const close = () => setOpen(false);
    const onDown = (e: PointerEvent) => {
      const t = e.target as Node;
      if (wrapRef.current?.contains(t) || menuRef.current?.contains(t)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onDown);
    document.addEventListener("keydown", onKey);
    // A fixed-positioned menu can't follow scroll/resize — close it rather than let it drift.
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    return () => {
      document.removeEventListener("pointerdown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", close, true);
      window.removeEventListener("resize", close);
    };
  }, [open]);

  const pick = (durable: boolean) => {
    setOpen(false);
    onRun(durable);
  };
  const item =
    "block w-full px-3 py-1.5 text-left text-sm text-slate-200 hover:bg-[var(--c-hover)]";

  return (
    <div ref={wrapRef} className="relative inline-block">
      <Button
        variant="primary"
        onClick={toggle}
        disabled={busy}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        {busy ? "Running…" : "Run"}
        {!busy && <ChevronDown size={14} aria-hidden />}
      </Button>
      {open &&
        pos &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            style={{ position: "fixed", left: pos.left, top: pos.top, zIndex: 60 }}
            className="min-w-[168px] overflow-hidden rounded-md border border-slate-700 bg-[var(--c-elev)] py-1 shadow-xl"
          >
            <button
              type="button"
              role="menuitem"
              onClick={() => pick(false)}
              title="A normal, streaming run on the interactive path"
              className={item}
            >
              Run
            </button>
            <button
              type="button"
              role="menuitem"
              onClick={() => pick(true)}
              title="Runs on the durable runtime — checkpoints each step so it resumes after a crash (no token streaming)"
              className={item}
            >
              Durable-Run
            </button>
          </div>,
          document.body,
        )}
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
