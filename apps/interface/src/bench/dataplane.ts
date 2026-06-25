// The model bench's DATA-PLANE calls — the §10 / §1.4 guard made concrete.
//
// Raw inference payloads (prompts, images, audio in/out) go DIRECTLY to the inference base URL in
// the user's trust domain (the localhost sidecar on desktop, the user's own infra self-hosted) —
// they are NEVER routed through the control plane (theygent must never be an involuntary MITM).
// So everything here targets INFERENCE_URL and nothing else; the control plane only ever sees the
// metrics + digests the bench records afterwards (lib/api.ts → /bench/*).

import { ApiError, INFERENCE_URL } from "../lib/api";
import type { Usage } from "./metrics";

function dataPlaneHeaders(): Record<string, string> {
  // The inference plane is user-controlled and unauthenticated locally; a bearer is harmless and
  // keeps the call symmetric with the management-plane reads. NO control-plane token semantics.
  return { Authorization: "Bearer dev-local" };
}

async function failFrom(res: Response): Promise<ApiError> {
  let message = `${res.status} ${res.statusText}`;
  let code = "http_error";
  try {
    const body = await res.json();
    const err = body?.error ?? body;
    if (err?.message) message = err.message;
    if (err?.code) code = err.code;
  } catch {
    /* non-JSON */
  }
  return new ApiError(message, res.status, code);
}

export interface ChatChunk {
  choices?: { delta?: { content?: string; reasoning_content?: string } }[];
  usage?: Usage;
}

/**
 * Stream a chat/vision completion from the inference data plane (SSE). Yields parsed chunks; the
 * caller stamps `performance.now()` per chunk to feed the pure metrics math. `messages` may carry
 * OpenAI image content blocks (the VLM/vision path — no separate endpoint, §1.3).
 */
export async function* streamChat(
  model: string,
  messages: unknown[],
  params: Record<string, unknown>,
): AsyncGenerator<ChatChunk> {
  const res = await fetch(`${INFERENCE_URL}/v1/chat/completions`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...dataPlaneHeaders() },
    // include_usage so the final chunk carries token counts + cost for the metrics (§2.4).
    body: JSON.stringify({
      model,
      messages,
      stream: true,
      stream_options: { include_usage: true },
      ...params,
    }),
  });
  if (!res.ok || !res.body) throw await failFrom(res);
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // SSE frames are separated by a blank line.
    while (true) {
      const sep = buf.indexOf("\n\n");
      if (sep < 0) break;
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (!line.startsWith("data:")) continue;
        const data = line.slice(5).trim();
        if (data === "[DONE]") return;
        try {
          yield JSON.parse(data) as ChatChunk;
        } catch {
          /* skip a malformed frame */
        }
      }
    }
  }
}

export interface EmbeddingsResult {
  data: { embedding: number[]; index: number }[];
  usage?: Usage;
}

export async function embed(
  model: string,
  input: string | string[],
  params: Record<string, unknown>,
): Promise<EmbeddingsResult> {
  const res = await fetch(`${INFERENCE_URL}/v1/embeddings`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...dataPlaneHeaders() },
    body: JSON.stringify({ model, input, ...params }),
  });
  if (!res.ok) throw await failFrom(res);
  return (await res.json()) as EmbeddingsResult;
}

export async function transcribe(
  model: string,
  file: File | Blob,
  params: Record<string, string>,
): Promise<{ text: string }> {
  const form = new FormData();
  form.set("model", model);
  form.set("file", file, "audio" in file ? "audio" : (file as File).name || "audio.wav");
  for (const [k, v] of Object.entries(params)) if (v) form.set(k, v);
  const res = await fetch(`${INFERENCE_URL}/v1/audio/transcriptions`, {
    method: "POST",
    headers: dataPlaneHeaders(), // NO Content-Type — the browser sets the multipart boundary
    body: form,
  });
  if (!res.ok) throw await failFrom(res);
  return (await res.json()) as { text: string };
}

export interface SpeakResult {
  blob: Blob;
  ttfbMs: number;
  totalMs: number;
}

/** TTS → audio bytes, streamed so we can stamp TTFB (first audio byte) honestly (§2.2). */
export async function speak(
  model: string,
  input: string,
  params: Record<string, unknown>,
): Promise<SpeakResult> {
  const start = performance.now();
  const res = await fetch(`${INFERENCE_URL}/v1/audio/speech`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...dataPlaneHeaders() },
    body: JSON.stringify({ model, input, ...params }),
  });
  if (!res.ok || !res.body) throw await failFrom(res);
  const reader = res.body.getReader();
  const parts: Uint8Array[] = [];
  let ttfbMs = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    if (value) {
      if (ttfbMs === 0) ttfbMs = performance.now() - start;
      parts.push(value);
    }
  }
  const totalMs = performance.now() - start;
  const type = res.headers.get("content-type") || "audio/mpeg";
  const blobParts = parts.map((p) => p as unknown as BlobPart);
  return { blob: new Blob(blobParts, { type }), ttfbMs, totalMs };
}
