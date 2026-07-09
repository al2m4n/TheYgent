// Agent bench — the body of the per-agent bench MODAL opened from an Agents row. Reuses the
// registry invoke path (no new run path): pin a version, run an input, read the persisted output,
// render the run trace as a per-node waterfall AND overlay it on the canvas (hover a span → the node
// flashes, via the span.name == node.id join). Degrades to output-only if observability is absent.
// The tune→ship loop: apply a saved preset's LITERAL params to a binding and save a new version
// through the registry (the server hashes; the preset name never enters the IR).

import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { IRDocument } from "@theygent/ir-types";
import { ChevronDown } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Selection } from "../adapter";
import { ChatView } from "../chat/ChatView";
import { Markdown } from "../chat/Markdown";
import { ThinkingBlock } from "../chat/ThinkingBlock";
import { useRunChat } from "../chat/useRunChat";
import { GraphCanvas } from "../components/GraphCanvas";
import { ResumePanel, parseTyped } from "../components/ResumePanel";
import { Button, Card, ErrorBanner, Field, Input, NoteBanner, Select } from "../components/ui";
import { Bubble, BubbleContent } from "../components/ui/bubble";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { RunWaterfall } from "../components/waterfall";
import { type AgentDetail, ApiError, api, streamRun } from "../lib/api";
import { isDurableOnly } from "../lib/durable";
import { shortId } from "../lib/format";
import type { DeltaFrame, ReasoningFrame, RunFrame } from "../lib/runtypes";
import { keys, useRun } from "../queries";
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
  // An agent whose graph drills `$in.in.<field>` takes an OBJECT input: JSON mode parses the text
  // client-side (loudly — an unparsable payload never leaves the tab as a look-alike string).
  const [inputMode, setInputMode] = useState<"text" | "json">("text");
  const [result, setResult] = useState<{
    runId: string;
    output?: string;
    reasoning?: string;
    error?: string;
    streaming?: boolean;
  } | null>(null);
  // The durable path enqueues a run and polls its id (the endpoint returns no terminal result).
  const [durableRunId, setDurableRunId] = useState<null | string>(null);
  // Set when a durable run is attempted but the server isn't in durable mode (400 durable_required).
  const [durableUnavailable, setDurableUnavailable] = useState(false);
  const [highlight, setHighlight] = useState<Selection>(null);
  const [running, setRunning] = useState(false);
  const queryClient = useQueryClient();

  const ir = stored.data?.ir as IRDocument | undefined;
  // A durable-only agent (loop/map/subgraph/human) can ONLY run durably. Any other agent can run
  // either way — a normal streaming run, or a durable run that checkpoints each step and resumes
  // after a crash — so we offer both. While the pinned IR is still loading we can't KNOW which,
  // so the run buttons wait for it (else a durable-only agent would briefly offer a plain Run
  // that can only 400).
  const durableOnly = ir ? isDurableOnly(ir) : false;
  // Gate the run buttons only while the fetch is IN FLIGHT — a failed fetch surfaces its error
  // below and leaves the buttons usable (the server re-checks everything anyway), rather than
  // sticking them disabled with no feedback.
  const irLoading = Boolean(version) && stored.isPending;

  // Poll the durable run (the endpoint returns a run id, not a terminal result). A `waiting` run
  // is paused at a human node — the resume panel below delivers the awaited input.
  const durablePoll = useRun(durableRunId ?? "", { live: true, enabled: Boolean(durableRunId) });
  const durableRun = durablePoll.data;
  const durableTerminal = durableRun?.status === "completed" || durableRun?.status === "failed";
  const durableWaiting = durableRun?.status === "waiting";

  const typedInput = parseTyped(inputMode, input);

  // Leaving the modal mid-stream must abort the request — the server cancels the run on
  // disconnect; an orphaned reader would keep a local engine generating with no Stop control left.
  const abortRef = useRef<(() => void) | null>(null);
  const stoppedRef = useRef(false);
  useEffect(
    () => () => {
      stoppedRef.current = true;
      abortRef.current?.();
    },
    [],
  );

  async function run(durable: boolean) {
    if (!typedInput.ok) return;
    setRunning(true);
    setResult(null);
    setDurableRunId(null);
    setDurableUnavailable(false);
    setHighlight(null);
    stoppedRef.current = false;
    try {
      if (durable) {
        const { run_id } = await api.runAgentDurable(agent.id, {
          input: typedInput.value,
          version,
        });
        setDurableRunId(run_id);
      } else {
        // The interactive Run STREAMS, exactly like every chat surface: the model's thinking
        // arrives as `reasoning` frames and the answer as `delta` frames, so the result renders
        // live through the same thinking-block + markdown presentation instead of a silent wait
        // for the terminal payload.
        const handle = await streamRun(`/agents/${encodeURIComponent(agent.id)}/runs`, {
          input: typedInput.value,
          stream: true,
          version,
        });
        abortRef.current = handle.abort;
        let content = "";
        let reasoning = "";
        let runId = "";
        let failed: string | undefined;
        let stopped = false;
        try {
          for await (const ev of handle.events) {
            if (ev.data === "[DONE]") continue;
            let payload: RunFrame | DeltaFrame | ReasoningFrame;
            try {
              payload = JSON.parse(ev.data);
            } catch {
              continue;
            }
            runId = payload.runId ?? runId;
            if (ev.event === "delta") {
              content += (payload as DeltaFrame).delta;
            } else if (ev.event === "reasoning") {
              reasoning += (payload as ReasoningFrame).reasoning;
            } else if (ev.event === "run") {
              const frame = payload as RunFrame;
              if (frame.status === "failed") failed = frame.error ?? "run failed";
            }
            setResult({
              runId,
              output: content || undefined,
              reasoning: reasoning || undefined,
              streaming: true,
            });
          }
        } catch (e) {
          // A deliberate Stop aborts the fetch mid-read — a stop, not a failure.
          if (stoppedRef.current) stopped = true;
          else throw e;
        }
        // The persisted run row carries the CANONICAL output — a graph whose answer comes from a
        // non-streaming node (tool/router-terminal) emits no deltas at all, and even an llm graph's
        // output node may post-process the streamed text. Prefer it; the streamed text was the
        // live preview. It also carries the honest empty-output note (run.error on `completed`).
        if (!stopped && runId) {
          try {
            const run = await api.getRun(runId);
            if (run.output) content = run.output;
            if (run.status === "failed") failed = failed ?? run.error ?? "run failed";
            else if (!content && run.error) failed = failed ?? run.error;
          } catch {
            /* keep the streamed view — the run row will still show it under Runs */
          }
        }
        setResult({
          runId,
          output: content || undefined,
          reasoning: reasoning || undefined,
          error: failed ?? (stopped ? "stopped" : undefined),
          streaming: false,
        });
        // The waterfall's live polling stops with the stream; one invalidation pulls the trace
        // as persisted at terminal (the final spans can land just after the last live poll).
        if (runId) queryClient.invalidateQueries({ queryKey: keys.trace(runId) });
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
      abortRef.current = null;
      setRunning(false);
    }
  }

  // One result view over both paths: the interactive stream (live content + reasoning), or the
  // durable poll's Run (no streaming — the durable queue journals results, it doesn't relay
  // tokens, so a durable run has output only).
  const view = durableRunId
    ? {
        runId: durableRunId,
        status: durableRun?.status as string | undefined,
        output: durableRun?.output ?? undefined,
        reasoning: undefined as string | undefined,
        error: durableRun?.error ?? undefined,
        streaming: false,
      }
    : result
      ? {
          runId: result.runId,
          status: undefined as string | undefined,
          output: result.output,
          reasoning: result.reasoning,
          error: result.error,
          streaming: Boolean(result.streaming),
        }
      : null;
  // A durable run keeps the buttons busy until the poll reaches a terminal status — except while
  // it is `waiting` at a human gate: the wait can outlive this tab, so the resume panel takes
  // over and the run buttons stay usable (starting a new run leaves the paused one waiting
  // server-side, visible under Runs).
  const busy = running || (Boolean(durableRunId) && !durableTerminal && !durableWaiting);
  const inProgress = view?.status && view.status !== "completed" && view.status !== "failed";
  const runDisabled = busy || irLoading || !typedInput.ok;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="text-sm font-medium text-slate-200">{agent.name}</span>
        <Field label="Version">
          <Select
            value={version}
            onChange={(e) => setPicked(e.target.value)}
            className="w-48"
            aria-label="Version"
            title="Which saved version to run — new runs use the version picked here"
          >
            {agent.versions.map((v) => (
              <option key={v.version} value={v.version}>
                v{v.version} · {shortId(v.content_hash, 12)}
              </option>
            ))}
          </Select>
        </Field>
      </div>

      <Field label="Input">
        <div className="flex items-start gap-2">
          <Select
            value={inputMode}
            onChange={(e) => setInputMode(e.target.value as "text" | "json")}
            className="w-24"
            aria-label="Input mode"
          >
            <option value="text">Text</option>
            <option value="json">JSON</option>
          </Select>
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder={inputMode === "json" ? '{"field": "value"}' : "Run input…"}
            className="flex-1"
            onKeyDown={(e) => {
              // Enter runs, exactly like the chat composer sends. A durable-only agent's only
              // path is durable; anything else takes the normal streaming run.
              if (e.key === "Enter" && !e.nativeEvent.isComposing && !runDisabled) {
                e.preventDefault();
                void run(durableOnly);
              }
            }}
          />
        </div>
      </Field>
      {!typedInput.ok && <p className="text-xs text-amber-400">{typedInput.error}</p>}
      <div className="flex items-center gap-2">
        {durableOnly ? (
          // Durable-only agents can't run any other way — a single button, no choice.
          <>
            <Button variant="primary" onClick={() => run(true)} disabled={runDisabled}>
              {busy ? "Running…" : "Run durably"}
            </Button>
            <span className="text-[11px] text-slate-500">
              durable-only — runs on the durable runtime
            </span>
          </>
        ) : (
          // Any other agent can run either way — a split button: the primary segment runs the
          // normal streaming path immediately, the caret opens a menu with the durable choice
          // (which checkpoints each step and resumes after a crash).
          <RunMenu busy={busy} disabled={runDisabled} onRun={run} />
        )}
        {result?.streaming && (
          <Button
            variant="ghost"
            onClick={() => {
              stoppedRef.current = true;
              abortRef.current?.();
            }}
          >
            Stop
          </Button>
        )}
      </div>

      {durableUnavailable && (
        <NoteBanner>
          The control-plane isn’t running in durable mode — start it with{" "}
          <span className="mono">THEYGENT_DURABLE=1</span> to run durably.
          {!durableOnly && " You can still use Run for a normal (non-resumable) run."}
        </NoteBanner>
      )}
      {stored.isError && <ErrorBanner error={stored.error} />}
      {durablePoll.isError && <ErrorBanner error={durablePoll.error} />}

      {inProgress && !durableWaiting && (
        <p className="text-sm text-slate-400">{`Running… (${view?.status})`}</p>
      )}
      {durableWaiting && durableRunId && (
        <ResumePanel
          runId={durableRunId}
          awaitingNode={durableRun?.awaiting_node ?? null}
          onResumed={() => durablePoll.refetch()}
        />
      )}
      {view?.error && <ErrorBanner error={view.error} />}
      {/* The result presents exactly like a chat answer everywhere: the model's thinking in the
          collapsible block (open while it streams), then the answer as markdown. */}
      {view?.reasoning && (
        <ThinkingBlock reasoning={view.reasoning} streaming={view.streaming && !view.output} />
      )}
      {view?.output && (
        <Bubble variant="secondary" className="max-w-full">
          <BubbleContent className="max-h-64 overflow-y-auto">
            <Markdown text={view.output} />
            {view.streaming && <span className="animate-pulse text-muted-foreground">▍</span>}
          </BubbleContent>
        </Bubble>
      )}
      {view?.runId && (
        <div className="space-y-3">
          <RunWaterfall
            runId={view.runId}
            // A durable run mounts the waterfall the moment it is enqueued — keep it live (1s
            // trace poll + the /trace/stream overlay) until the poll reaches a terminal status.
            // An interactive run now streams too, so it is live until its stream ends.
            isLive={(Boolean(durableRunId) && !durableTerminal) || Boolean(view.streaming)}
            onHoverNode={(id) => setHighlight(id ? { kind: "node", id } : null)}
          />
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

      {/* Talk to the agent: each turn is a streamed run with session memory (the conversation is
          recorded as a session and shows under Recents). Durable-only agents run through the
          durable queue, which has no streaming/session path — the single-shot Run above covers
          them. */}
      {!durableOnly && !irLoading && (
        <div className="space-y-2 border-t border-slate-800 pt-4">
          <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Chat</p>
          <AgentChat agentId={agent.id} agentName={agent.name} version={version} />
        </div>
      )}

      {ir && <ApplyPreset agentId={agent.id} ir={ir} />}
    </div>
  );
}

function AgentChat({
  agentId,
  agentName,
  version,
}: {
  agentId: string;
  agentName: string;
  version: string;
}) {
  const chat = useRunChat(
    { kind: "agent", agentId, agentName, version },
    { placeholder: "Message the agent…" },
  );
  return (
    <ChatView
      controller={chat}
      listClassName="max-h-[40vh]"
      emptyHint="Message the agent — turns share session memory, so it remembers the conversation."
    />
  );
}

// The Run split button: the primary segment runs the normal streaming path IMMEDIATELY; the caret
// segment opens a menu with the durable choice. Used for agents that can run either way (a
// durable-only agent gets a single button). The menu is a dropdown primitive: it portals and
// positions itself (so the scrollable bench modal can't clip it), owns focus/arrow-key/Escape
// handling, and composes with the dialog's layer stack — clicking it never dismisses the modal.
function RunMenu({
  busy,
  disabled,
  onRun,
}: {
  busy: boolean;
  disabled: boolean;
  onRun: (durable: boolean) => void;
}) {
  return (
    <div className="inline-flex">
      <Button
        variant="primary"
        className="rounded-r-none"
        onClick={() => onRun(false)}
        disabled={disabled}
        title="A normal, streaming run on the interactive path"
      >
        {busy ? "Running…" : "Run"}
      </Button>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="primary"
            className="rounded-l-none border-l-blue-400 px-1.5"
            disabled={disabled}
            aria-label="Run options"
          >
            <ChevronDown size={14} aria-hidden />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem
            onSelect={() => onRun(false)}
            title="A normal, streaming run on the interactive path"
          >
            Run
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => onRun(true)}
            title="Runs on the durable runtime — checkpoints each step so it resumes after a crash (no token streaming)"
          >
            Run durably
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </div>
  );
}

// Apply a saved preset's LITERAL params to one of this agent's bindings, then save a new version
// through the registry. Modality is matched so a chat preset can't land on a TTS binding.
function ApplyPreset({ agentId, ir }: { agentId: string; ir: IRDocument }) {
  const presets = useQuery({ queryKey: ["presets"], queryFn: () => api.listPresets() });
  const bindings = Object.keys(ir.models ?? {});
  const [presetId, setPresetId] = useState("");
  const [binding, setBinding] = useState(bindings[0] ?? "");
  // Success and failure are separate states so a failed save never reads like a quiet note.
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<unknown>(null);

  if (!presets.data || presets.data.length === 0) return null;

  async function apply() {
    const preset = presets.data?.find((p) => p.id === presetId);
    if (!preset || !binding) return;
    setSaved(null);
    setError(null);
    try {
      const next = applyPresetToBinding(ir, binding, preset.params);
      const updated = await api.addAgentVersion(agentId, { ir: next });
      const hash = updated.versions[0]?.content_hash;
      setSaved(`applied → new ${hash ? shortId(hash, 12) : "version"}`);
    } catch (e) {
      setError(e);
    }
  }

  return (
    <Card className="space-y-2 p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Apply a preset</p>
      <div className="flex flex-wrap items-center gap-2">
        <Select
          value={presetId}
          onChange={(e) => setPresetId(e.target.value)}
          className="w-48"
          aria-label="Preset"
        >
          <option value="">preset…</option>
          {presets.data.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name} ({p.modality})
            </option>
          ))}
        </Select>
        <Select
          value={binding}
          onChange={(e) => setBinding(e.target.value)}
          className="w-40"
          aria-label="Model binding"
        >
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
      {saved && <span className="text-xs text-slate-400">{saved}</span>}
      <ErrorBanner error={error} />
    </Card>
  );
}
