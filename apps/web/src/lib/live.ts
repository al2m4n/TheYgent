// A tiny in-memory registry of *live* runs (M8 §1.4/§3.2). The run composer creates and
// streams from one POST, then navigates to the run detail — which would lose the in-memory
// stream. So the composer parks the stream here, keyed by runId, and the run detail attaches
// to it. This is local UI state (a module singleton + pub-sub), deliberately NOT TanStack
// Query: Query owns *server* state (the persisted run), this owns the *transient* token feed.

import { useSyncExternalStore } from "react";
import { ApiError, streamRun } from "./api";
import type { DeltaFrame, ReasoningFrame, RunFrame, RunStatus } from "./types";

export interface TimelineEntry {
  /** SSE event name: "run" | "delta" | "reasoning" | sentinel. */
  event: string;
  detail: string;
  ts: number;
}

export interface LiveRun {
  runId: string;
  status: RunStatus;
  output: string;
  /** A reasoning model's streamed thinking (event: reasoning). Progress only — never the answer. */
  reasoning: string;
  model?: string;
  error?: string;
  done: boolean;
  timeline: TimelineEntry[];
  abort: () => void;
}

type Listener = () => void;

const runs = new Map<string, LiveRun>();
const listeners = new Set<Listener>();

function emit() {
  for (const l of listeners) l();
}

function subscribe(l: Listener): () => void {
  listeners.add(l);
  return () => listeners.delete(l);
}

function patch(runId: string, next: Partial<LiveRun>) {
  const cur = runs.get(runId);
  if (!cur) return;
  runs.set(runId, { ...cur, ...next });
  emit();
}

/**
 * POST a run/graph run and stream it into the live registry. Resolves with the runId as soon
 * as the backend's first `run` frame arrives, so the caller can navigate. A pre-stream error
 * (400/404/503 before any frame) rejects, exactly as the backend surfaces it (M3/M5).
 */
export async function startLiveRun(path: string, body: unknown): Promise<string> {
  const handle = await streamRun(path, body);

  return new Promise<string>((resolve, reject) => {
    let resolved = false;

    (async () => {
      try {
        for await (const ev of handle.events) {
          if (ev.data === "[DONE]") continue;
          let payload: RunFrame | DeltaFrame | ReasoningFrame;
          try {
            payload = JSON.parse(ev.data);
          } catch {
            continue;
          }
          const runId = payload.runId;
          if (!runId) continue;

          if (!runs.has(runId)) {
            runs.set(runId, {
              runId,
              status: "streaming",
              output: "",
              reasoning: "",
              done: false,
              timeline: [],
              abort: handle.abort,
            });
          }

          if (ev.event === "delta") {
            const d = payload as DeltaFrame;
            const cur = runs.get(runId)!;
            patch(runId, {
              output: cur.output + d.delta,
              timeline: [...cur.timeline, { event: "delta", detail: d.delta, ts: Date.now() }],
            });
          } else if (ev.event === "reasoning") {
            // The model's thinking — accumulate separately so it's visible progress, never the
            // answer. Keeps the stream from looking frozen during a long reasoning phase.
            const r = payload as ReasoningFrame;
            const cur = runs.get(runId)!;
            patch(runId, { reasoning: cur.reasoning + r.reasoning });
          } else if (ev.event === "run") {
            const r = payload as RunFrame;
            const cur = runs.get(runId)!;
            const status = r.status ?? cur.status;
            patch(runId, {
              status,
              model: r.model ?? cur.model,
              error: r.error ?? cur.error,
              done: status === "completed" || status === "failed",
              timeline: [
                ...cur.timeline,
                {
                  event: "run",
                  detail: `${status}${r.error ? `: ${r.error}` : ""}`,
                  ts: Date.now(),
                },
              ],
            });
          }

          if (!resolved) {
            resolved = true;
            resolve(runId);
          }
        }
        // Stream ended without an explicit terminal frame — mark done defensively.
        for (const [id, r] of runs) {
          if (!r.done && r.abort === handle.abort) patch(id, { done: true });
        }
      } catch (e) {
        if (!resolved) {
          resolved = true;
          reject(e);
        }
      }
    })();
  }).catch((e) => {
    // Surface a pre-stream ApiError to the caller (composer shows it inline).
    throw e instanceof ApiError ? e : new Error(String(e));
  });
}

export function getLiveRun(runId: string): LiveRun | undefined {
  return runs.get(runId);
}

/** React hook: subscribe a component to a live run by id (re-renders on each delta). */
export function useLiveRun(runId: string | undefined): LiveRun | undefined {
  return useSyncExternalStore(
    subscribe,
    () => (runId ? runs.get(runId) : undefined),
    () => undefined,
  );
}
