// The model bench's DATA-PLANE calls — the plane-boundary guard made concrete.
//
// Raw inference payloads (prompts, images, audio in/out) go DIRECTLY to the inference base URL in
// the user's trust domain (the localhost sidecar on desktop, the user's own infra self-hosted) —
// they are NEVER routed through the control plane (theygent must never be an involuntary MITM).
// So everything here targets the inference base URL and nothing else; the control plane only ever sees the
// metrics + digests the bench records afterwards (lib/api.ts → /bench/*).

import { ApiError, inferenceUrl } from "../lib/api";
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
 * OpenAI image content blocks (the VLM/vision path — no separate endpoint).
 */
export async function* streamChat(
  model: string,
  messages: unknown[],
  params: Record<string, unknown>,
  signal?: AbortSignal,
): AsyncGenerator<ChatChunk> {
  const res = await fetch(`${inferenceUrl()}/v1/chat/completions`, {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/json", ...dataPlaneHeaders() },
    // include_usage so the final chunk carries token counts + cost for the metrics.
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
  const res = await fetch(`${inferenceUrl()}/v1/embeddings`, {
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
  const res = await fetch(`${inferenceUrl()}/v1/audio/transcriptions`, {
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

/** TTS → audio bytes, streamed so we can stamp TTFB (first audio byte) honestly. */
export async function speak(
  model: string,
  input: string,
  params: Record<string, unknown>,
): Promise<SpeakResult> {
  const start = performance.now();
  const res = await fetch(`${inferenceUrl()}/v1/audio/speech`, {
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

export interface GeneratedImage {
  blob: Blob;
  totalMs: number;
}

/**
 * Text-to-image → an image blob. Requests base64 (the bytes come back inline, no URL to a server
 * in a different trust domain), decodes the first image, and returns it as a PNG blob for a chat
 * bubble. Engine knobs (e.g. `steps`) ride in `params` — the inference route forwards them through.
 */
export async function generateImage(
  model: string,
  prompt: string,
  params: Record<string, unknown>,
): Promise<GeneratedImage> {
  const start = performance.now();
  const res = await fetch(`${inferenceUrl()}/v1/images/generations`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...dataPlaneHeaders() },
    body: JSON.stringify({ model, prompt, response_format: "b64_json", ...params }),
  });
  if (!res.ok) throw await failFrom(res);
  const body = (await res.json()) as { data?: { b64_json?: string; url?: string }[] };
  const first = body.data?.[0];
  if (!first?.b64_json) throw new ApiError("the image endpoint returned no image", 502, "no_image");
  const bytes = Uint8Array.from(atob(first.b64_json), (c) => c.charCodeAt(0));
  return {
    blob: new Blob([bytes as unknown as BlobPart], { type: "image/png" }),
    totalMs: performance.now() - start,
  };
}
