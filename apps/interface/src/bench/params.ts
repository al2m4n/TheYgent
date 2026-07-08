// Schema-driven, capability-NARROWED param forms (data-driven discipline).
//
// Each modality declares its param surface as DATA; the panel renders it generically (no hardcoded
// per-param JSX). A param that needs a capability the model doesn't advertise is HIDDEN, not greyed
// wrongly (`requiresCap`) — the same "modality is capability data, not a hardcoded if/else tree"
// rule that keys the panel registry. New params are a data addition here, never a new component.

import type { Capabilities } from "../lib/api";

export type ParamType = "number" | "text" | "select" | "bool";

export interface ParamSpec {
  key: string;
  label: string;
  type: ParamType;
  min?: number;
  max?: number;
  step?: number;
  options?: string[];
  placeholder?: string;
  /** Shown ONLY if the model's capabilities advertise this flag (else hidden, not greyed). */
  requiresCap?: "toolCalling" | "structuredOutput" | "vision";
  help?: string;
}

// Panels are keyed by the panel-bearing modalities. `vision` rides the chat panel (it is a chat
// sub-capability), so it has no entry of its own; the chat panel shows image attach when
// `caps.vision` is set.
export type PanelModality = "chat" | "embeddings" | "audio.transcription" | "audio.speech";

const CHAT_PARAMS: ParamSpec[] = [
  { key: "temperature", label: "Temperature", type: "number", min: 0, max: 2, step: 0.1 },
  { key: "top_p", label: "Top-p", type: "number", min: 0, max: 1, step: 0.05 },
  { key: "max_tokens", label: "Max tokens", type: "number", min: 1, step: 1, placeholder: "2048" },
  { key: "stop", label: "Stop", type: "text", placeholder: "comma-separated" },
  {
    key: "presence_penalty",
    label: "Presence penalty",
    type: "number",
    min: -2,
    max: 2,
    step: 0.1,
  },
  {
    key: "frequency_penalty",
    label: "Frequency penalty",
    type: "number",
    min: -2,
    max: 2,
    step: 0.1,
  },
  { key: "seed", label: "Seed", type: "number", step: 1 },
  // Capability-gated — hidden unless the model advertises the flag:
  {
    key: "tool_choice",
    label: "Tool choice",
    type: "select",
    options: ["auto", "none", "required"],
    requiresCap: "toolCalling",
  },
  {
    key: "response_format",
    label: "JSON / structured output",
    type: "select",
    options: ["text", "json_object"],
    requiresCap: "structuredOutput",
  },
];

const EMBEDDINGS_PARAMS: ParamSpec[] = [
  { key: "dimensions", label: "Dimensions", type: "number", min: 1, step: 1 },
  {
    key: "encoding_format",
    label: "Encoding format",
    type: "select",
    options: ["float", "base64"],
  },
];

const STT_PARAMS: ParamSpec[] = [
  { key: "language", label: "Language", type: "text", placeholder: "en" },
  { key: "prompt", label: "Prompt", type: "text", placeholder: "domain terms to bias toward" },
  { key: "temperature", label: "Temperature", type: "number", min: 0, max: 1, step: 0.1 },
  {
    key: "response_format",
    label: "Response format",
    type: "select",
    options: ["json", "text", "verbose_json"],
  },
];

const TTS_PARAMS: ParamSpec[] = [
  // Voice names are engine-specific; empty falls through to the binding's default, then the
  // engine's own model default.
  { key: "voice", label: "Voice", type: "text", placeholder: "engine default" },
  { key: "speed", label: "Speed", type: "number", min: 0.25, max: 4, step: 0.05 },
  {
    key: "response_format",
    label: "Format",
    type: "select",
    options: ["mp3", "wav", "opus", "flac", "aac", "pcm"],
  },
];

export const PARAM_SPECS: Record<PanelModality, ParamSpec[]> = {
  chat: CHAT_PARAMS,
  embeddings: EMBEDDINGS_PARAMS,
  "audio.transcription": STT_PARAMS,
  "audio.speech": TTS_PARAMS,
};

/** The param specs that apply to a modality for a given model — narrowed by its capabilities. */
export function paramsForModality(
  modality: PanelModality,
  caps: Capabilities | undefined,
): ParamSpec[] {
  const specs = PARAM_SPECS[modality] ?? [];
  return specs.filter((spec) => !spec.requiresCap || Boolean(caps?.[spec.requiresCap]));
}

/** Coerce a raw form value to the param's wire type (numbers as numbers; "stop" → array). */
export function coerceParam(spec: ParamSpec, raw: string): unknown {
  if (raw === "") return undefined;
  if (spec.type === "number") {
    const n = Number(raw);
    return Number.isFinite(n) ? n : undefined;
  }
  if (spec.key === "stop")
    return raw
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
  if (spec.key === "response_format" && raw === "json_object") return { type: "json_object" };
  return raw;
}
