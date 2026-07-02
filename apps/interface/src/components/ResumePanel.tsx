// The human-in-the-loop affordance: a durable run paused at a human node (status "waiting") shows
// which node awaits input and a box to deliver it (POST /runs/{id}/resume). The workflow resumes
// from its durable checkpoint; the caller's poll picks the status change up. A run may pause more
// than once — the panel re-appears for each gate.

import { useState } from "react";
import { api } from "../lib/api";
import { Button, ErrorBanner, Input, Select } from "./ui";

// A typed text input's parse outcome. Text mode passes the raw string through; JSON mode parses
// loudly so a malformed object never leaves the tab as a look-alike string that only errors at
// run time. An empty JSON box means "no input" (null), matching the run endpoints' default.
export type TypedValue = { ok: true; value: unknown } | { ok: false; error: string };

export function parseTyped(mode: "text" | "json", raw: string): TypedValue {
  if (mode === "text") return { ok: true, value: raw };
  if (raw.trim() === "") return { ok: true, value: null };
  try {
    return { ok: true, value: JSON.parse(raw) };
  } catch (e) {
    return {
      ok: false,
      error: `Invalid JSON input: ${e instanceof Error ? e.message : String(e)}`,
    };
  }
}

export function ResumePanel({
  runId,
  awaitingNode,
  onResumed,
}: {
  runId: string;
  awaitingNode: string | null;
  onResumed: () => void;
}) {
  const [value, setValue] = useState("");
  const [mode, setMode] = useState<"text" | "json">("text");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const typed = parseTyped(mode, value);

  async function resume() {
    if (!typed.ok) return;
    setSending(true);
    setError(null);
    try {
      await api.resumeRun(runId, typed.value);
      setValue("");
      onResumed();
    } catch (e) {
      setError(e);
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="space-y-2 rounded-md border border-violet-800/60 bg-violet-950/20 p-3">
      <p className="text-sm text-violet-300">
        Paused — awaiting input at <span className="mono">{awaitingNode ?? "a human node"}</span>
      </p>
      <div className="flex items-start gap-2">
        <Select
          value={mode}
          onChange={(e) => setMode(e.target.value as "text" | "json")}
          className="w-24"
          aria-label="Resume input mode"
        >
          <option value="text">Text</option>
          <option value="json">JSON</option>
        </Select>
        <Input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder={mode === "json" ? '{"approve": true}' : "Reply to the waiting node…"}
          className="flex-1"
          onKeyDown={(e) => {
            // isComposing: committing an IME composition also fires Enter — don't deliver a
            // half-composed value to the waiting node (the send is irrevocable).
            if (e.key === "Enter" && !e.nativeEvent.isComposing && typed.ok && !sending) resume();
          }}
        />
        <Button variant="primary" onClick={resume} disabled={sending || !typed.ok}>
          {sending ? "Resuming…" : "Resume"}
        </Button>
      </div>
      {!typed.ok && <p className="text-xs text-amber-400">{typed.error}</p>}
      <ErrorBanner error={error} />
    </div>
  );
}
