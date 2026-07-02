import { json } from "@codemirror/lang-json";
import { type Diagnostic, linter } from "@codemirror/lint";
import { useNavigate, useSearch } from "@tanstack/react-router";
import CodeMirror from "@uiw/react-codemirror";
import { useMemo, useState } from "react";
import { Button, Card, ErrorBanner, Field, Input, Page, Select, Textarea } from "../components/ui";
import { ApiError } from "../lib/api";
import { validateIR } from "../lib/ir-validate";
import { startLiveRun } from "../lib/live";
import { useTheme } from "../lib/theme";
import { useAgentMutations, useModels, useThreads } from "../queries";

// A known-good trivial IR (input → llm → output) so graph mode starts runnable. Follows the
// standard document envelope; the user edits `models.default.model` to a registered logical id.
const DEFAULT_IR = `{
  "schemaVersion": "1.0",
  "id": "agt_01J9X8COCKPIT",
  "name": "cockpit-demo",
  "version": "0.1.0",
  "models": {
    "default": { "binding": "mlx", "model": "triage-fast", "params": { "maxTokens": 2048 } }
  },
  "tools": {},
  "nodes": [
    { "id": "n_in", "type": "input", "kind": "boundary",
      "ports": { "in": [], "out": [{ "id": "out", "type": "any" }] } },
    { "id": "n_llm", "type": "llm", "kind": "activity",
      "config": { "model": "default", "messages": [{ "role": "user", "content": "$in" }] },
      "ports": { "in": [{ "id": "in", "type": "any" }],
                 "out": [{ "id": "ok", "type": "any" }, { "id": "err", "type": "error" }] } },
    { "id": "n_out", "type": "output", "kind": "boundary",
      "ports": { "in": [{ "id": "in", "type": "any" }], "out": [] } }
  ],
  "edges": [
    { "id": "e1", "source": "n_in", "sourceHandle": "out",
      "target": "n_llm", "targetHandle": "in", "channel": "data" },
    { "id": "e2", "source": "n_llm", "sourceHandle": "ok",
      "target": "n_out", "targetHandle": "in", "channel": "data" }
  ]
}`;

// CodeMirror linter: surface IR issues as diagnostics. The semantic issues have no
// source position, so we anchor them to the document start — the backend stays authoritative.
function irLinter() {
  return linter((view) => {
    const { issues } = validateIR(view.state.doc.toString());
    return issues.map<Diagnostic>((issue) => ({
      from: 0,
      to: Math.min(1, view.state.doc.length),
      severity: issue.severity,
      message: issue.message,
    }));
  });
}

type Mode = "prompt" | "graph";

export function Compose() {
  const search = useSearch({ from: "/compose" });
  const navigate = useNavigate();
  const { resolved } = useTheme();
  const { data: models } = useModels();
  const { data: threads } = useThreads();

  const [mode, setMode] = useState<Mode>("prompt");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // prompt mode
  const [input, setInput] = useState("");
  const [model, setModel] = useState("");
  const [threadId, setThreadId] = useState(search.threadId ?? "");

  // graph mode
  const [ir, setIr] = useState(DEFAULT_IR);
  const [graphInput, setGraphInput] = useState("");
  const irIssues = useMemo(() => validateIR(ir).issues, [ir]);
  const irBlocking = irIssues.some((i) => i.severity === "error");

  // "Save as agent": persist the IR currently in the editor as a saved, versioned agent. A new
  // agent id → create; an existing id → add a version (the IR's `version` is the new coordinate).
  const { create, addVersion } = useAgentMutations();
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const saving = create.isPending || addVersion.isPending;

  async function saveAsAgent() {
    setSaveMsg(null);
    setError(null);
    let doc: { id?: string; version?: string };
    try {
      doc = JSON.parse(ir);
    } catch {
      setError("IR is not valid JSON");
      return;
    }
    try {
      try {
        const detail = await create.mutateAsync({ ir: doc });
        setSaveMsg(`Saved "${detail.name}" v${detail.versions[0]?.version} (new agent)`);
      } catch (e) {
        // An existing agent id → add a version instead (the saved-agent loop's natural second save).
        if (e instanceof ApiError && e.code === "agent_exists" && doc.id) {
          const detail = await addVersion.mutateAsync({ id: doc.id, ir: doc });
          const latest = detail.versions[0]?.version;
          setSaveMsg(`Added version v${latest} to "${detail.name}"`);
        } else {
          throw e;
        }
      }
    } catch (e) {
      setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String((e as Error).message));
    }
  }

  const extensions = useMemo(() => [json(), irLinter()], []);

  async function submit() {
    setError(null);
    setSubmitting(true);
    try {
      let runId: string;
      if (mode === "prompt") {
        if (!model) throw new Error("Pick a model first");
        runId = await startLiveRun("/runs", {
          input,
          model,
          // A generous default so a reasoning model (which spends tokens "thinking" before it
          // answers) isn't truncated to an empty answer out of the box. /runs params pass through
          // to the inference seam as-is, so this is snake_case (the graph IR uses camelCase).
          params: { max_tokens: 2048 },
          stream: true,
          thread_id: threadId || null,
        });
      } else {
        runId = await startLiveRun("/graphs/runs", {
          ir: JSON.parse(ir),
          input: graphInput,
          stream: true,
          thread_id: threadId || null,
        });
      }
      navigate({ to: "/runs/$runId", params: { runId } });
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.code}: ${e.message}` : String((e as Error).message);
      setError(msg);
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Page className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-lg font-semibold text-slate-100">Compose a run</h1>
        <div className="flex items-center gap-2">
          {(["prompt", "graph"] as const).map((m) => (
            <Button
              key={m}
              variant={mode === m ? "primary" : "ghost"}
              className="capitalize"
              aria-pressed={mode === m}
              onClick={() => setMode(m)}
            >
              {m} mode
            </Button>
          ))}
        </div>
      </div>

      <ErrorBanner error={error} />

      <Card className="space-y-4 p-4">
        {mode === "prompt" ? (
          <>
            <Field label="Input">
              <Textarea
                rows={4}
                value={input}
                placeholder="Ask the model something…"
                onChange={(e) => setInput(e.target.value)}
              />
            </Field>
            <Field label="Model (logical id)">
              <Select value={model} onChange={(e) => setModel(e.target.value)}>
                <option value="">— select a registered model —</option>
                {models?.map((m) => (
                  <option key={m.logicalId} value={m.logicalId}>
                    {m.logicalId} · {m.binding.binding}
                  </option>
                ))}
              </Select>
            </Field>
          </>
        ) : (
          <>
            <Field label="Input (binds to the graph input node)">
              <Textarea
                rows={2}
                value={graphInput}
                placeholder="Input passed to the graph…"
                onChange={(e) => setGraphInput(e.target.value)}
              />
            </Field>
            <div className="space-y-1">
              <span className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
                IR document (JSON)
              </span>
              <div className="overflow-hidden rounded-md border border-slate-700">
                <CodeMirror
                  value={ir}
                  height="340px"
                  theme={resolved === "light" ? "light" : "dark"}
                  extensions={extensions}
                  onChange={(v) => {
                    setIr(v);
                    setSaveMsg(null);
                  }}
                />
              </div>
              {irIssues.length > 0 ? (
                <ul className="space-y-0.5 pt-1 text-xs">
                  {irIssues.map((issue) => (
                    <li
                      key={`${issue.severity}:${issue.message}`}
                      className={
                        issue.severity === "error"
                          ? "text-rose-700 dark:text-rose-300"
                          : "text-amber-700 dark:text-amber-300"
                      }
                    >
                      {issue.severity === "error" ? "✗" : "⚠"} {issue.message}
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="pt-1 text-xs text-emerald-700 dark:text-emerald-300">
                  ✓ IR looks structurally valid
                </p>
              )}
            </div>
          </>
        )}

        <Field label="Thread id (optional — enables conversational memory)">
          <Input
            list="thread-ids"
            value={threadId}
            placeholder="leave blank for a one-shot run"
            onChange={(e) => setThreadId(e.target.value)}
          />
          <datalist id="thread-ids">
            {threads?.map((t) => (
              <option key={t.id} value={t.id} />
            ))}
          </datalist>
        </Field>

        <div className="flex items-center gap-3">
          <Button
            variant="primary"
            disabled={
              submitting || (mode === "prompt" && !model) || (mode === "graph" && irBlocking)
            }
            onClick={submit}
          >
            {submitting ? "Starting…" : "Run & stream"}
          </Button>
          {mode === "prompt" && !model && (
            <span className="text-xs text-slate-500">select a model to run</span>
          )}
          {mode === "graph" && (
            <Button variant="default" disabled={saving || irBlocking} onClick={saveAsAgent}>
              {saving ? "Saving…" : "Save as agent"}
            </Button>
          )}
          {mode === "graph" && irBlocking && (
            <span className="text-xs text-rose-700 dark:text-rose-300">
              fix the IR errors to run
            </span>
          )}
          {mode === "graph" && saveMsg && (
            <span className="text-xs text-emerald-700 dark:text-emerald-300">{saveMsg}</span>
          )}
        </div>
      </Card>
    </Page>
  );
}
