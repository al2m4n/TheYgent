// The control-plane chat transport: each turn is a streamed run (POST /runs for a model, POST
// /agents/{id}/runs for a published agent) carrying the session id, so the SERVER replays prior
// turns and appends the new pair — the client never writes session messages on this path.
// Consumes the run-stream SSE frames: `delta` (answer), `reasoning` (thinking, kept apart),
// `run` (status transitions).

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, streamRun } from "../lib/api";
import {
  type Boundary,
  TEXT_BOUNDARY,
  attachmentKindFor,
  buildRunInput,
  classifyRunOutput,
  composerCapsFor,
} from "../lib/modality";
import type { DeltaFrame, ReasoningFrame, RunFrame } from "../lib/runtypes";
import { newSessionId, openSession, recordTurn } from "./session";
import type { Attachment, ChatController, ChatMessage, ComposerCaps } from "./types";
import { messageId } from "./types";

export type RunChatTarget =
  | { kind: "model"; model: string }
  | { kind: "agent"; agentId: string; agentName?: string; version?: number | string };

/** Placeholder answer labels for a session-recorded media turn (the bytes never persist). */
const MEDIA_LABEL = { image: "🖼 image", audio: "🔊 audio" } as const;

/** How a client-recorded turn describes what the user sent, when it wasn't plain text. */
function userTurnLabel(input: string, text: string): string {
  if (input === "audio") return "🎤 voice message";
  if (input === "image") return text || "🖼 image";
  if (input === "video") return text || "🎬 video";
  if (input === "file") return text || "📎 file";
  return text;
}

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
   * The agent's declared I/O boundary, from `lib/modality.ts`. It decides what the composer offers
   * and what shape the run body's `input` takes; absent means the plain text boundary. Every
   * surface derives it the same way, so a voice agent behaves identically in New Chat, in the
   * per-agent Run modal, and when a stored session is reopened.
   */
  boundary?: Boundary;
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

  // Object URLs minted for downloaded media replies — each pins its blob until revoked, so a long
  // voice conversation would leak steadily without this.
  const objectUrlsRef = useRef<string[]>([]);

  // Leaving the surface mid-stream must abort the request — the server cancels the run on
  // disconnect; an orphaned reader would keep a local engine generating with no Stop control left.
  useEffect(
    () => () => {
      stoppedRef.current = true;
      abortRef.current?.();
      for (const url of objectUrlsRef.current) URL.revokeObjectURL(url);
      objectUrlsRef.current = [];
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
        : {
            kind: "bench.agent" as const,
            agent_id: t.agentId,
            agent_name: t.agentName,
            // Recorded so reopening the session runs the SAME version — and therefore re-derives
            // the same I/O boundary — instead of silently continuing on latest.
            agent_version: t.version != null ? String(t.version) : undefined,
          };
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
      const boundary = optsRef.current.boundary ?? TEXT_BOUNDARY;
      // A non-textual turn carries values the session cannot usefully store — an artifact ref, a
      // base64 image. Letting the SERVER append them would journal a blob as the turn and replay it
      // to the llm, so those runs go session-less and we record a READABLE turn (a label + the
      // answer) ourselves afterwards. A `json` boundary stays on the server path: its turns are
      // legible text, and it keeps the conversation memory a chat needs.
      const mediaIn = boundary.input !== "text" && boundary.input !== "json";
      const isMedia = mediaIn || boundary.mediaOut;
      setError(null);
      setBusy(true);
      stoppedRef.current = false;
      const userMsg: ChatMessage = {
        id: messageId(),
        role: "user",
        content: text,
        attachments: attachments.length > 0 ? attachments : undefined,
      };
      const assistantId = messageId();
      setMessages((cur) => [
        ...cur,
        userMsg,
        { id: assistantId, role: "assistant", content: "", streaming: true },
      ]);

      try {
        // The run body's `input` is shaped by the boundary's declared modality — one builder for
        // every surface (see lib/modality.ts). A payload that can't be built (no clip staged,
        // malformed JSON) fails HERE, in the tab, instead of reaching the graph as a look-alike
        // string that only errors mid-run.
        const built = await buildRunInput(
          boundary.input,
          { text, attachments },
          api.uploadArtifact,
        );
        if (!built.ok) {
          patchMessage(assistantId, { streaming: false, error: built.error });
          setBusy(false);
          return;
        }
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
                input: built.value,
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
        // What the run actually returned is decided from the PERSISTED output, not from the
        // declared modality: a graph ends on whichever boundary fired, so a voice agent whose
        // transcription failed hands back prose on its error branch and must render as prose.
        // Reading the run row also covers the text case where the answer comes from a
        // non-streaming node (the output node's value, not the llm's deltas).
        let mediaNote: string | undefined;
        let mediaKind: "image" | "audio" = "audio";
        if (!failed && !stopped && runId && (boundary.mediaOut || !content)) {
          try {
            const run = await api.getRun(runId);
            const resolved = classifyRunOutput(run.output, boundary);
            // A FAILED run can still have persisted an answer — show both, exactly as before the
            // boundary refactor. Recording the failure must not discard what the graph produced.
            if (run.status === "failed") failed = run.error ?? "run failed";
            if (resolved.kind === "artifact") {
              const blob = await api.downloadArtifact(resolved.ref);
              mediaKind = attachmentKindFor(blob.type, boundary);
              const url = URL.createObjectURL(blob);
              objectUrlsRef.current.push(url);
              patchMessage(assistantId, {
                attachments: [{ kind: mediaKind, url, blob, name: "reply" }],
              });
            } else if (resolved.kind === "empty") {
              // Completed but carried nothing — say so instead of a blank bubble.
              if (boundary.mediaOut && !failed) {
                mediaNote = boundary.output.has("image")
                  ? "The agent produced no image for this turn."
                  : "The agent produced no playable audio for this turn.";
              }
            } else if (!content) {
              content = resolved.text;
              patchMessage(assistantId, { content });
            }
          } catch {
            // The stream already ended; a failed lookup leaves the honest note below.
            if (boundary.mediaOut && !content) {
              mediaNote = boundary.output.has("image")
                ? "The agent produced no image for this turn."
                : "The agent produced no playable audio for this turn.";
            }
          }
        }
        // A media-output agent legitimately streams no text (its answer is the produced clip/image),
        // so the empty-content note applies only to text agents.
        patchMessage(assistantId, {
          streaming: false,
          error:
            failed ??
            mediaNote ??
            (stopped
              ? "stopped"
              : !content && !boundary.mediaOut
                ? "The run finished without visible content — a reasoning model may have spent the whole token budget thinking."
                : undefined),
        });
        // Media agent: the run was session-less, so record a READABLE turn ourselves (a label for
        // the sent media + a label for the answer). A text agent's turns were appended by the
        // server. A media-output agent has no text answer — record a placeholder so the session
        // still shows the exchange.
        // Only a real media result records a turn — a mediaNote means nothing playable/viewable
        // was produced, so the placeholder must not masquerade as a successful exchange.
        const answerLabel = content || (boundary.mediaOut ? MEDIA_LABEL[mediaKind] : "");
        if (isMedia && sess && !failed && !stopped && !mediaNote && answerLabel) {
          await recordTurn(sess, userTurnLabel(boundary.input, text), answerLabel);
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

  // What the composer offers comes from the boundary alone — the one mapping in lib/modality.ts,
  // so this surface can never drift from the agent Run modal or the canvas Test panel.
  const composer = useMemo<ComposerCaps>(
    () =>
      composerCapsFor(
        opts.boundary ?? TEXT_BOUNDARY,
        opts.placeholder ?? (target?.kind === "agent" ? "Message the agent…" : undefined),
      ),
    [opts.boundary, opts.placeholder, target?.kind],
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
