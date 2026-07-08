// The control-plane chat transport: each turn is a streamed run (POST /runs for a model, POST
// /agents/{id}/runs for a saved agent) carrying the session id, so the SERVER replays prior
// turns and appends the new pair — the client never writes session messages on this path.
// Consumes the run-stream SSE frames: `delta` (answer), `reasoning` (thinking, kept apart),
// `run` (status transitions).

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, streamRun } from "../lib/api";
import type { DeltaFrame, ReasoningFrame, RunFrame } from "../lib/runtypes";
import { newSessionId, openSession } from "./session";
import type { Attachment, ChatController, ChatMessage, ComposerCaps } from "./types";
import { messageId } from "./types";

export type RunChatTarget =
  | { kind: "model"; model: string }
  | { kind: "agent"; agentId: string; agentName?: string; version?: number | string };

export interface RunChatOptions {
  /** Continue an existing session (its id is reused verbatim; no new row is created). */
  sessionId?: string;
  /** Seed the transcript when reopening a stored session. */
  initialMessages?: ChatMessage[];
  /** Model-run params passed through to the inference seam (snake_case). */
  params?: Record<string, unknown>;
  placeholder?: string;
  /** Why the composer is unavailable when `target` is null (a page-specific explanation). */
  disabledNote?: string;
}

export function useRunChat(
  target: RunChatTarget | null,
  opts: RunChatOptions = {},
): ChatController {
  const [messages, setMessages] = useState<ChatMessage[]>(opts.initialMessages ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(opts.sessionId ?? null);
  const queryClient = useQueryClient();

  const targetRef = useRef(target);
  targetRef.current = target;
  const optsRef = useRef(opts);
  optsRef.current = opts;
  const sessionRef = useRef<string | null>(opts.sessionId ?? null);
  const abortRef = useRef<(() => void) | null>(null);
  // Set by Stop (or unmount) BEFORE aborting, so the resulting fetch abort reads as a deliberate
  // stop, never as a failure.
  const stoppedRef = useRef(false);

  // Leaving the surface mid-stream must abort the request — the server cancels the run on
  // disconnect; an orphaned reader would keep a local engine generating with no Stop control left.
  useEffect(
    () => () => {
      stoppedRef.current = true;
      abortRef.current?.();
    },
    [],
  );

  const patchMessage = useCallback((id: string, patch: Partial<ChatMessage>) => {
    setMessages((cur) => cur.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }, []);

  const ensureSession = useCallback(async (): Promise<string | null> => {
    if (sessionRef.current) return sessionRef.current;
    const t = targetRef.current;
    if (!t) return null;
    const meta =
      t.kind === "model"
        ? { kind: "chat" as const, model: t.model }
        : { kind: "bench.agent" as const, agent_id: t.agentId, agent_name: t.agentName };
    const id = await openSession(meta, newSessionId());
    sessionRef.current = id;
    setSessionId(id);
    if (id) queryClient.invalidateQueries({ queryKey: ["sessions"] });
    return id;
  }, [queryClient]);

  const send = useCallback(
    async (text: string, _attachments: Attachment[]) => {
      const t = targetRef.current;
      if (!t) return;
      setError(null);
      setBusy(true);
      stoppedRef.current = false;
      const userMsg: ChatMessage = { id: messageId(), role: "user", content: text };
      const assistantId = messageId();
      setMessages((cur) => [
        ...cur,
        userMsg,
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);

      try {
        const sess = await ensureSession();
        const path = t.kind === "model" ? "/runs" : `/agents/${encodeURIComponent(t.agentId)}/runs`;
        const body =
          t.kind === "model"
            ? {
                input: text,
                model: t.model,
                // Passed through to the inference seam as-is, hence snake_case.
                params: optsRef.current.params ?? { max_tokens: 2048 },
                stream: true,
                session_id: sess,
              }
            : {
                input: text,
                stream: true,
                session_id: sess,
                ...(t.version != null ? { version: t.version } : {}),
              };

        const handle = await streamRun(path, body);
        abortRef.current = handle.abort;
        let content = "";
        let reasoning = "";
        let runId: string | undefined;
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
              patchMessage(assistantId, { content, runId });
            } else if (ev.event === "reasoning") {
              reasoning += (payload as ReasoningFrame).reasoning;
              patchMessage(assistantId, { reasoning, runId });
            } else if (ev.event === "run") {
              const frame = payload as RunFrame;
              if (frame.status === "failed") failed = frame.error ?? "run failed";
              patchMessage(assistantId, { runId });
            }
          }
        } catch (e) {
          // A deliberate Stop aborts the fetch mid-read — that's a stop, not a failure. Anything
          // else is a real transport error.
          if (stoppedRef.current) stopped = true;
          else throw e;
        }
        // A graph whose answer comes from a non-streaming node (the output node's value, not the
        // llm's deltas) ends the stream with no visible content — the persisted run has it.
        if (!failed && !stopped && !content && runId) {
          try {
            const run = await api.getRun(runId);
            if (run.output) {
              content = run.output;
              patchMessage(assistantId, { content });
            }
            if (run.status === "failed") failed = run.error ?? "run failed";
          } catch {
            // The stream already ended; a failed lookup just leaves the honest empty-note below.
          }
        }
        patchMessage(assistantId, {
          streaming: false,
          error:
            failed ??
            (stopped
              ? "stopped"
              : !content
                ? "The run finished without visible content — a reasoning model may have spent the whole token budget thinking."
                : undefined),
        });
        // The server appended the pair (on success) — refresh what lists/details show.
        queryClient.invalidateQueries({ queryKey: ["sessions"] });
        queryClient.invalidateQueries({ queryKey: ["runs"] });
        if (sess) queryClient.invalidateQueries({ queryKey: ["session", sess] });
      } catch (e) {
        // The failed turn wears its own note in the bubble — no duplicate global banner.
        const msg = e instanceof Error ? e.message : String(e);
        patchMessage(assistantId, { streaming: false, error: msg });
      } finally {
        abortRef.current = null;
        setBusy(false);
      }
    },
    [ensureSession, patchMessage, queryClient],
  );

  const stop = useCallback(() => {
    stoppedRef.current = true;
    abortRef.current?.();
  }, []);

  const composer = useMemo<ComposerCaps>(
    () => ({
      placeholder:
        optsRef.current.placeholder ??
        (target?.kind === "agent" ? "Message the agent…" : "Send a message…"),
    }),
    [target?.kind],
  );

  return {
    messages,
    busy,
    error,
    sessionId,
    composer,
    send,
    stop,
    disabled: target == null,
    disabledNote:
      target == null
        ? (opts.disabledNote ??
          "This session's target is unknown — it can be read but not continued.")
        : undefined,
  };
}
