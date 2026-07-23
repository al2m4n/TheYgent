// The direct data-plane chat transport — the bench path. Raw payloads (prompts, images, audio)
// go straight to the inference base URL in the user's trust domain, exactly like the rest of the
// bench; the control plane only receives the finished turn pair for session history (and the
// metrics the bench opts to record). Multi-turn: the full transcript is resent each turn, since
// the data plane is stateless.
//
// Reasoning models: a separate `reasoning_content` delta field is consumed directly; inline
// <think>…</think> in content goes through ThinkParser. Both land in the message's `reasoning`.

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { generateImage, speak, streamChat, transcribe } from "../bench/dataplane";
import {
  type BenchMetrics,
  type ChatSample,
  computeChatMetrics,
  computeSttMetrics,
  computeTtsMetrics,
} from "../bench/metrics";
import type { Capabilities } from "../lib/api";
import { type SessionKind, describeAttachments, openSession, recordTurn } from "./session";
import { ThinkParser } from "./think";
import type { Attachment, ChatController, ChatMessage, ComposerCaps } from "./types";
import { messageId } from "./types";

export type InferenceChatModality =
  | "chat"
  | "audio.transcription"
  | "audio.speech"
  | "images.generation";

/** One finished exchange, in the shape the bench needs to record a result. */
export interface CompletedTurn {
  modality: string;
  output: string;
  metrics: Record<string, number>;
  params: Record<string, unknown>;
}

export interface InferenceChatOptions {
  logicalId: string;
  modality: InferenceChatModality;
  caps?: Capabilities;
  /** Wire-ready request params (the bench's schema-driven form) — read fresh at each send. */
  params: Record<string, unknown>;
  /** Optional system prompt (chat modality). */
  system?: string;
  /** Record the conversation as a session (on by default — every chat lands in Recents). */
  record?: boolean;
  /** Reuse an existing session (continuation) instead of creating one on the first send. */
  sessionId?: string;
  /**
   * Seed the transcript when reopening a stored session. Display-only for the audio modalities
   * (each transcription/speech exchange is independent); the chat modality replays it.
   */
  initialMessages?: ChatMessage[];
  /** Session metadata kind — bench surfaces record bench.model, the chat page records chat. */
  sessionKind?: SessionKind;
  /** A finished exchange, keyed to its assistant message (the bench mounts save/compare on it). */
  onTurn?: (messageIdent: string, turn: CompletedTurn) => void;
}

export type WirePart =
  | { type: "text"; text: string }
  | { type: "image_url"; image_url: { url: string } };
export type WireMessage = { role: string; content: string | WirePart[] };

/**
 * Replay the transcript in OpenAI shape; image attachments become image_url content blocks.
 * A failed exchange is dropped WHOLE — skipping just the empty assistant half would leave two
 * consecutive user messages, which strict chat templates reject. Exported for its tests.
 */
export function toWireMessages(history: ChatMessage[], system: string | undefined): WireMessage[] {
  const wire: WireMessage[] = [];
  if (system?.trim()) wire.push({ role: "system", content: system.trim() });
  for (let i = 0; i < history.length; i++) {
    const m = history[i];
    if (m.role === "assistant") {
      if (m.content) wire.push({ role: "assistant", content: m.content });
      continue;
    }
    // A user turn whose (finished) assistant half produced nothing is a failed pair — skip both.
    const next = history[i + 1];
    if (next && next.role === "assistant" && !next.content && !next.streaming) continue;
    const images = (m.attachments ?? []).filter((a) => a.kind === "image");
    if (images.length > 0) {
      const parts: WirePart[] = [];
      if (m.content) parts.push({ type: "text", text: m.content });
      for (const img of images) parts.push({ type: "image_url", image_url: { url: img.url } });
      wire.push({ role: "user", content: parts });
    } else {
      wire.push({ role: "user", content: m.content });
    }
  }
  return wire;
}

/** The numeric entries only — the message model and bench record both want a plain number map. */
function toMetricRecord(metrics: BenchMetrics): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(metrics)) if (typeof v === "number") out[k] = v;
  return out;
}

function attachmentNotes(attachments: Attachment[]): string[] {
  return attachments.map((a) => {
    if (a.kind === "image") return `image: ${a.name ?? "attached"}`;
    if (a.kind === "file") return `file: ${a.name ?? "attached"}`;
    return `audio: ${a.name ?? "attached"}${a.durationSec ? ` (${a.durationSec.toFixed(1)}s)` : ""}`;
  });
}

export function useInferenceChat(opts: InferenceChatOptions): ChatController {
  const [messages, setMessages] = useState<ChatMessage[]>(opts.initialMessages ?? []);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(opts.sessionId ?? null);
  const queryClient = useQueryClient();

  // Read live values at send time (params/system change between turns without re-binding send).
  const optsRef = useRef(opts);
  optsRef.current = opts;
  // The one create-session promise: the first caller mints it, everyone else (an early send, the
  // turn recorder, an overlapping retry) awaits the SAME promise — never two session rows. A
  // continuation reuses the given id verbatim (the row already exists).
  const sessionPromiseRef = useRef<Promise<string | null> | null>(
    opts.sessionId ? Promise.resolve(opts.sessionId) : null,
  );
  const abortRef = useRef<AbortController | null>(null);

  // Leaving the surface mid-stream must abort the request — an orphaned reader would keep the
  // local engine generating the full completion with no Stop control left anywhere.
  useEffect(() => () => abortRef.current?.abort(), []);

  const patchMessage = useCallback((id: string, patch: Partial<ChatMessage>) => {
    setMessages((cur) => cur.map((m) => (m.id === id ? { ...m, ...patch } : m)));
  }, []);

  const ensureSession = useCallback((): Promise<string | null> => {
    const { record = true, logicalId, modality, sessionKind } = optsRef.current;
    if (!record) return Promise.resolve(null);
    if (!sessionPromiseRef.current) {
      sessionPromiseRef.current = openSession({
        kind: sessionKind ?? "bench.model",
        model: logicalId,
        modality,
      }).then((id) => {
        setSessionId(id);
        if (id) queryClient.invalidateQueries({ queryKey: ["sessions"] });
        return id;
      });
    }
    return sessionPromiseRef.current;
  }, [queryClient]);

  const finishTurn = useCallback(
    async (userText: string, attachments: Attachment[], assistantText: string) => {
      const sess = await ensureSession();
      if (!sess) return;
      await recordTurn(
        sess,
        describeAttachments(userText, attachmentNotes(attachments)),
        assistantText,
      );
      queryClient.invalidateQueries({ queryKey: ["sessions"] });
      queryClient.invalidateQueries({ queryKey: ["session", sess] });
    },
    [ensureSession, queryClient],
  );

  const send = useCallback(
    async (text: string, attachments: Attachment[]) => {
      const { logicalId, modality, params, system, onTurn } = optsRef.current;
      setError(null);
      setBusy(true);
      // Starting to chat IS starting a session — create it now, not after the first success, so
      // the conversation shows under Recents from its first moment.
      void ensureSession();
      const userMsg: ChatMessage = {
        id: messageId(),
        role: "user",
        content: text,
        attachments: attachments.length ? attachments : undefined,
      };
      const assistantId = messageId();
      const assistant: ChatMessage = {
        id: assistantId,
        role: "assistant",
        content: "",
        streaming: true,
      };

      try {
        if (modality === "audio.transcription") {
          const audio = attachments.find((a) => a.kind === "audio");
          if (!audio?.blob) throw new Error("attach or record audio first");
          setMessages((cur) => [...cur, userMsg, assistant]);
          const stringParams: Record<string, string> = {};
          for (const [k, v] of Object.entries(params)) if (v != null) stringParams[k] = String(v);
          const start = performance.now();
          const res = await transcribe(logicalId, audio.blob, stringParams);
          const metrics = toMetricRecord(
            computeSttMetrics(audio.durationSec ?? 0, performance.now() - start),
          );
          patchMessage(assistantId, {
            content: res.text,
            streaming: false,
            metrics,
          });
          onTurn?.(assistantId, {
            modality,
            output: res.text,
            metrics,
            params,
          });
          await finishTurn(text, attachments, res.text);
          return;
        }

        if (modality === "audio.speech") {
          setMessages((cur) => [...cur, userMsg, assistant]);
          const { blob, ttfbMs, totalMs } = await speak(logicalId, text, params);
          const metrics = toMetricRecord(computeTtsMetrics(text.length, ttfbMs, totalMs));
          patchMessage(assistantId, {
            streaming: false,
            metrics,
            attachments: [{ kind: "audio", url: URL.createObjectURL(blob), blob, name: "speech" }],
          });
          onTurn?.(assistantId, {
            modality,
            output: "",
            metrics,
            params,
          });
          await finishTurn(text, attachments, "[synthesized speech]");
          return;
        }

        if (modality === "images.generation") {
          setMessages((cur) => [...cur, userMsg, assistant]);
          const { blob, totalMs } = await generateImage(logicalId, text, params);
          const metrics = { latencyMs: Math.round(totalMs) };
          patchMessage(assistantId, {
            streaming: false,
            metrics,
            attachments: [{ kind: "image", url: URL.createObjectURL(blob), blob, name: "image" }],
          });
          onTurn?.(assistantId, { modality, output: "", metrics, params });
          await finishTurn(text, attachments, "[generated image]");
          return;
        }

        // chat / vision — streaming, with the reasoning split.
        const history = [...messages, userMsg];
        setMessages((cur) => [...cur, userMsg, assistant]);
        const wire = toWireMessages(history, system);
        const parser = new ThinkParser();
        let fieldReasoning = "";
        const samples: ChatSample[] = [];
        const start = performance.now();
        const controller = new AbortController();
        abortRef.current = controller;
        let stopped = false;
        try {
          for await (const chunk of streamChat(logicalId, wire, params, controller.signal)) {
            const atMs = performance.now() - start;
            const delta = chunk.choices?.[0]?.delta;
            if (delta?.reasoning_content) fieldReasoning += delta.reasoning_content;
            // Metrics sample the SPLIT answer, not the raw delta — TTFT is the first visible
            // answer token, so inline thinking doesn't fake an early TTFT (parity with the run
            // path, where thinking streams as separate reasoning events).
            const answeredBefore = parser.content.length;
            if (delta?.content) parser.push(delta.content);
            const released = parser.content.slice(answeredBefore);
            samples.push({ atMs, content: released || undefined, usage: chunk.usage });
            patchMessage(assistantId, {
              content: parser.content,
              reasoning: fieldReasoning + parser.reasoning || undefined,
            });
          }
        } catch (e) {
          if (controller.signal.aborted) stopped = true;
          else throw e;
        }
        parser.flush();
        const metrics = toMetricRecord(computeChatMetrics(samples));
        const reasoning = fieldReasoning + parser.reasoning || undefined;
        const emptyNote =
          !parser.content && !stopped
            ? "The model finished without visible content — a reasoning model may have spent the whole token budget thinking. Try raising max tokens."
            : undefined;
        patchMessage(assistantId, {
          content: parser.content,
          reasoning,
          streaming: false,
          metrics,
          error: stopped ? "stopped" : emptyNote,
        });
        const usedVision = attachments.some((a) => a.kind === "image");
        onTurn?.(assistantId, {
          modality: usedVision ? "vision" : "chat",
          output: parser.content,
          metrics,
          params,
        });
        if (parser.content) await finishTurn(text, attachments, parser.content);
      } catch (e) {
        // The failed turn wears its own note in the bubble — no duplicate global banner.
        const msg = e instanceof Error ? e.message : String(e);
        patchMessage(assistantId, { streaming: false, error: msg });
      } finally {
        abortRef.current = null;
        setBusy(false);
      }
    },
    [messages, patchMessage, finishTurn, ensureSession],
  );

  const stop = useCallback(() => abortRef.current?.abort(), []);

  const composer = useMemo<ComposerCaps>(() => {
    const { modality, caps } = opts;
    if (modality === "audio.transcription") {
      return { audio: true, audioRequired: true, textDisabled: true };
    }
    if (modality === "audio.speech") return { placeholder: "Text to speak…" };
    if (modality === "images.generation") return { placeholder: "Describe an image to generate…" };
    return { images: Boolean(caps?.vision), placeholder: "Ask something…" };
  }, [opts]);

  // Transcription/speech are single request-response calls with nothing to abort — offering a
  // Stop control there would be a dead button.
  return {
    messages,
    busy,
    error,
    sessionId,
    composer,
    send,
    stop: opts.modality === "chat" ? stop : undefined,
  };
}
