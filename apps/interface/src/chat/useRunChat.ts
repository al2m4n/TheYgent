// The control-plane chat transport: each turn is a streamed run (POST /runs for a model, POST
// /agents/{id}/runs for a published agent) carrying the session id, so the SERVER replays prior
// turns and appends the new pair — the client never writes session messages on this path.
// Consumes the run-stream SSE frames: `delta` (answer), `reasoning` (thinking, kept apart),
// `run` (status transitions).

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, streamRun } from "../lib/api";
import type { DeltaFrame, ReasoningFrame, RunFrame } from "../lib/runtypes";
import { newSessionId, openSession, recordTurn } from "./session";
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
  /**
   * The agent's input boundary is audio: the composer takes a recorded/uploaded clip, which is
   * uploaded as an artifact and passed as the run input {ref, contentType} instead of text.
   */
  audioInput?: boolean;
  /**
   * The agent's output boundary is audio: run.output is an artifact reference to the spoken
   * answer — download it and attach a playable bubble (the llm's text still streams as content).
   */
  audioOutput?: boolean;
  /**
   * The agent's input boundary is image: the composer takes an image (upload/camera) plus a
   * question, sent as {image: <data URI>, text} the vlm llm node drills into an image_url part.
   */
  imageInput?: boolean;
  /**
   * The agent's output boundary is image: run.output is an artifact reference to a generated image
   * — download it and attach an image bubble (symmetric to audioOutput).
   */
  imageOutput?: boolean;
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
    async (text: string, attachments: Attachment[]) => {
      const t = targetRef.current;
      if (!t) return;
      const { audioInput, audioOutput, imageInput, imageOutput } = optsRef.current;
      // A media agent's turns carry non-text values (audio/image refs, a base64 image) — letting
      // the SERVER append them would store an ugly blob as the session turn and replay it to the
      // llm. So a media agent's runs are session-less; we record a READABLE turn (a label + the
      // answer text) ourselves after the run.
      const isMedia = Boolean(audioInput || audioOutput || imageInput || imageOutput);
      setError(null);
      setBusy(true);
      stoppedRef.current = false;
      // A media agent's user turn shows the clip/image it sent (plus any typed question).
      const audioIn = audioInput ? attachments.find((a) => a.kind === "audio") : undefined;
      const imageIn = imageInput ? attachments.find((a) => a.kind === "image") : undefined;
      const userMsg: ChatMessage = {
        id: messageId(),
        role: "user",
        content: text,
        attachments: audioIn ? [audioIn] : imageIn ? [imageIn] : undefined,
      };
      const assistantId = messageId();
      setMessages((cur) => [
        ...cur,
        userMsg,
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);

      try {
        const sess = await ensureSession();
        const path = t.kind === "model" ? "/runs" : `/agents/${encodeURIComponent(t.agentId)}/runs`;
        // Media-in agents shape the run input from the attachment:
        //  - audio: upload the clip → pass the artifact reference (the walker fetches+transcribes);
        //  - image: pass {image: <data URI>, text} — the vlm llm node drills $in.in.image /
        //    $in.in.text into an image_url content part + the question.
        let agentInput: unknown = text;
        if (audioIn?.blob) {
          const ref = await api.uploadArtifact(audioIn.blob);
          agentInput = { ref: ref.ref, contentType: ref.contentType };
        } else if (imageIn) {
          agentInput = { image: imageIn.url, text };
        }
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
                input: agentInput,
                stream: true,
                // Media agent: session-less run (see above); text agent: server-appended turns.
                session_id: isMedia ? null : sess,
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
        // Media-output agent: run.output is an artifact reference to the produced media (spoken
        // answer or generated image). Fetch the bytes and attach a playable/viewable bubble.
        let mediaNote: string | undefined;
        if ((audioOutput || imageOutput) && !failed && !stopped && runId) {
          const mediaKind = imageOutput ? "image" : "audio";
          try {
            const run = await api.getRun(runId);
            const ref = run.output ? (JSON.parse(run.output) as { ref?: string }) : null;
            if (ref?.ref) {
              const blob = await api.downloadArtifact(ref.ref);
              patchMessage(assistantId, {
                attachments: [
                  { kind: mediaKind, url: URL.createObjectURL(blob), blob, name: "reply" },
                ],
              });
            } else if (run.status === "failed") {
              failed = run.error ?? "run failed";
            } else {
              // Completed but carried no artifact reference — say so instead of a blank bubble.
              mediaNote = imageOutput
                ? "The agent produced no image for this turn."
                : "The agent produced no playable audio for this turn.";
            }
          } catch {
            mediaNote = imageOutput
              ? "The agent produced no image for this turn."
              : "The agent produced no playable audio for this turn.";
          }
        } else if (!failed && !stopped && !content && runId) {
          // A text graph whose answer comes from a non-streaming node (the output node's value,
          // not the llm's deltas) ends the stream with no visible content — the run row has it.
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
        // A media-output agent legitimately streams no text (its answer is the produced clip/image),
        // so the empty-content note applies only to text agents.
        const mediaOutput = Boolean(audioOutput || imageOutput);
        patchMessage(assistantId, {
          streaming: false,
          error:
            failed ??
            mediaNote ??
            (stopped
              ? "stopped"
              : !content && !mediaOutput
                ? "The run finished without visible content — a reasoning model may have spent the whole token budget thinking."
                : undefined),
        });
        // Media agent: the run was session-less, so record a READABLE turn ourselves (a label for
        // the sent media + a label for the answer). A text agent's turns were appended by the
        // server. A media-output agent has no text answer — record a placeholder so the session
        // still shows the exchange.
        // Only a real media result records a turn — a mediaNote means nothing playable/viewable
        // was produced, so the placeholder must not masquerade as a successful exchange.
        const answerLabel = content || (imageOutput ? "🖼 image" : audioOutput ? "🔊 audio" : "");
        if (isMedia && sess && !failed && !stopped && !mediaNote && answerLabel) {
          const userLabel = audioInput ? "🎤 voice message" : imageInput ? text || "🖼 image" : text;
          await recordTurn(sess, userLabel, answerLabel);
          queryClient.invalidateQueries({ queryKey: ["session", sess] });
        }
        // Refresh what lists/details show.
        queryClient.invalidateQueries({ queryKey: ["sessions"] });
        queryClient.invalidateQueries({ queryKey: ["runs"] });
        if (sess && !isMedia) queryClient.invalidateQueries({ queryKey: ["session", sess] });
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

  const composer = useMemo<ComposerCaps>(() => {
    // An audio-input agent takes a recorded/uploaded clip in place of text (like the STT bench).
    if (opts.audioInput) {
      return { audio: true, audioRequired: true, textDisabled: true };
    }
    // A vision agent takes an image (upload/camera) AND a question — text stays enabled.
    if (opts.imageInput) {
      return { images: true, placeholder: "Attach an image and ask about it…" };
    }
    // An image-generation agent (text prompt in → image out) prompts for a description.
    if (opts.imageOutput) {
      return { placeholder: "Describe an image to generate…" };
    }
    return {
      placeholder:
        opts.placeholder ?? (target?.kind === "agent" ? "Message the agent…" : "Send a message…"),
    };
  }, [opts.audioInput, opts.imageInput, opts.imageOutput, opts.placeholder, target?.kind]);

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
