// The unified chat surface's data shapes. One message model serves every chat in the app —
// benching a model from a registry row, talking to a published agent, or continuing a session —
// so the rendering (bubbles, thinking blocks, attachments, per-turn metrics) is written once.
// Transports differ (direct data-plane vs. control-plane runs); they all produce this shape.

export type ChatRole = "user" | "assistant";

export interface ImageAttachment {
  kind: "image";
  /** http(s) URL, a data: URI (base64-inline), or an object URL — all pass to the engine or
   *  <img> unchanged. */
  url: string;
  /** The raw bytes, kept for a generated image the user may save (an object-URL blob). */
  blob?: Blob;
  name?: string;
}

export interface AudioAttachment {
  kind: "audio";
  /** Object URL for in-app playback. */
  url: string;
  /** The raw bytes, kept for upload (transcription) — playback alone would lose them. */
  blob?: Blob;
  name?: string;
  durationSec?: number;
}

export type Attachment = ImageAttachment | AudioAttachment;

export interface ChatMessage {
  id: string;
  role: ChatRole;
  /** The answer text (assistant) or the typed prompt (user). Assistant text renders as markdown. */
  content: string;
  /** A reasoning model's thinking — kept apart from the answer, rendered collapsible. */
  reasoning?: string;
  attachments?: Attachment[];
  /** Per-turn bench metrics (TTFT, tok/s, …) — present only on bench transports. */
  metrics?: Record<string, number>;
  /** A failed or degraded turn's note (the turn stays visible; the note explains it). */
  error?: string;
  streaming?: boolean;
  /** The control-plane run behind this turn, when one exists (links to the run detail). */
  runId?: string;
}

/** What the composer offers for the current target (modality-driven, not hardcoded per page). */
export interface ComposerCaps {
  /** Image attach (upload / camera) — on when the model advertises vision. */
  images?: boolean;
  /** Audio attach (upload / microphone) — on for transcription targets. */
  audio?: boolean;
  /** Sending requires an audio attachment (transcription: the audio IS the message). */
  audioRequired?: boolean;
  /** Hide the text input entirely (transcription: there is nothing to type). */
  textDisabled?: boolean;
  placeholder?: string;
}

/** The contract between a chat transport hook and the ChatView shell. */
export interface ChatController {
  messages: ChatMessage[];
  busy: boolean;
  /** A transport-level failure (pre-stream HTTP error etc.) — per-turn issues ride the message. */
  error: string | null;
  /** The session recording this conversation (created lazily on the first send). */
  sessionId: string | null;
  composer: ComposerCaps;
  send: (text: string, attachments: Attachment[]) => void | Promise<void>;
  /** Abort the in-flight turn, keeping the partial answer. */
  stop?: () => void;
  /** The composer is unavailable (e.g. the session's model is gone) — note says why. */
  disabled?: boolean;
  disabledNote?: string;
}

export function messageId(): string {
  return crypto.randomUUID();
}
