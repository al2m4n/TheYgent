// Schema-driven, capability-NARROWED param forms (data-driven discipline).
//
// Each modality declares its param surface as DATA; the panel renders it generically (no hardcoded
// per-param JSX). A param that needs a capability the model doesn't advertise is HIDDEN, not greyed
// wrongly (`requiresCap`) — the same "modality is capability data, not a hardcoded if/else tree"
// rule that keys the panel registry. New params are a data addition here, never a new component.
//
// The same specs back the agent editor's model-params panel, so the bench and the editor stay one
// vocabulary: what you tune on the bench is exactly what you can set on a node/binding.

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
  requiresCap?: "toolCalling" | "structuredOutput" | "vision" | "reasoning";
  /** Plain-language explanation surfaced behind the "?" help icon next to the field. */
  help?: string;
}

// Panels are keyed by the panel-bearing modalities. `vision` rides the chat panel (it is a chat
// sub-capability), so it has no entry of its own; the chat panel shows image attach when
// `caps.vision` is set.
export type PanelModality = "chat" | "embeddings" | "audio.transcription" | "audio.speech";

const CHAT_PARAMS: ParamSpec[] = [
  {
    key: "temperature",
    label: "Temperature",
    type: "number",
    min: 0,
    max: 2,
    step: 0.1,
    help: "Randomness of sampling. Lower (0–0.3) gives focused, repeatable answers; higher (0.8+) gives more varied, creative ones. 0 always picks the most likely token.",
  },
  {
    key: "top_p",
    label: "Top-p",
    type: "number",
    min: 0,
    max: 1,
    step: 0.05,
    help: "Nucleus sampling: the model only samples from the smallest set of tokens whose combined probability reaches this value. Lower = safer and more predictable; 1 = no cut. Tune this or temperature, not both.",
  },
  {
    key: "max_tokens",
    label: "Max tokens",
    type: "number",
    min: 1,
    step: 1,
    placeholder: "2048",
    help: "Hard cap on generated tokens — the reply is cut off at the limit. Reasoning models spend tokens thinking before answering, so set this generously or the visible answer may come back empty.",
  },
  {
    key: "stop",
    label: "Stop",
    type: "text",
    placeholder: "comma-separated",
    help: "Sequences that immediately end generation when the model emits them (the sequence itself is not included in the output). Comma-separate several.",
  },
  {
    key: "presence_penalty",
    label: "Presence penalty",
    type: "number",
    min: -2,
    max: 2,
    step: 0.1,
    help: "Penalizes any token that has already appeared, nudging the model toward new topics. Negative values encourage it to stay on the same subject.",
  },
  {
    key: "frequency_penalty",
    label: "Frequency penalty",
    type: "number",
    min: -2,
    max: 2,
    step: 0.1,
    help: "Penalizes tokens in proportion to how often they've appeared, reducing word-for-word repetition. Negative values permit more repetition.",
  },
  {
    key: "seed",
    label: "Seed",
    type: "number",
    step: 1,
    help: "Best-effort determinism: the same seed with the same prompt and params should reproduce the same answer on engines that support it — useful for comparable benchmark runs.",
  },
  // NOT capability-gated: reasoning detection from a probe is approximate and conservative, so
  // gating would hide the switch exactly on the models that need it. Engines without the switch
  // simply ignore it (the help text says so).
  {
    key: "chat_template_kwargs",
    label: "Reasoning",
    type: "select",
    options: ["on", "off"],
    help: "Turn the model's hidden thinking phase on or off (sent as the chat template's enable_thinking switch). Off = faster, direct answers; on = the model reasons before answering. Leave unset for the model's default; models without the switch ignore it.",
  },
  {
    key: "reasoning_effort",
    label: "Reasoning effort",
    type: "select",
    options: ["low", "medium", "high"],
    help: "How much thinking a reasoning model does before answering: low = brief, fast; high = long, thorough. Useful when a model overthinks — rambling, self-correcting reasoning burns tokens and time. Only models trained with effort levels honor it; others ignore it.",
  },
  // Capability-gated — hidden unless the model advertises the flag:
  {
    key: "tool_choice",
    label: "Tool choice",
    type: "select",
    options: ["auto", "none", "required"],
    requiresCap: "toolCalling",
    help: "Whether the model may call the offered tools: auto lets it decide, required forces a tool call, none disables calling for this request.",
  },
  {
    key: "response_format",
    label: "JSON / structured output",
    type: "select",
    options: ["text", "json_object"],
    requiresCap: "structuredOutput",
    help: "Ask for a structured reply. json_object constrains the model to emit valid JSON — also say in the prompt what JSON you expect, or the model may emit an empty object.",
  },
];

const EMBEDDINGS_PARAMS: ParamSpec[] = [
  {
    key: "dimensions",
    label: "Dimensions",
    type: "number",
    min: 1,
    step: 1,
    help: "Truncate the output vector to this many dimensions (only meaningful on models trained for shortening). Leave empty for the model's native size.",
  },
  {
    key: "encoding_format",
    label: "Encoding format",
    type: "select",
    options: ["float", "base64"],
    help: "How the vectors travel over the wire: float = plain JSON numbers (readable), base64 = packed floats (smaller and faster for large batches).",
  },
];

const STT_PARAMS: ParamSpec[] = [
  {
    key: "language",
    label: "Language",
    type: "text",
    placeholder: "en",
    help: "Two-letter code of the spoken language (en, de, fa, …). Setting it improves accuracy and speed; leave empty to auto-detect.",
  },
  {
    key: "prompt",
    label: "Prompt",
    type: "text",
    placeholder: "domain terms to bias toward",
    help: "Optional text that biases recognition — names, acronyms, and domain terms the audio is likely to contain, or the previous segment's transcript for continuity.",
  },
  {
    key: "temperature",
    label: "Temperature",
    type: "number",
    min: 0,
    max: 1,
    step: 0.1,
    help: "Decoding randomness. 0 returns the most likely transcript (recommended); raise it only if the model gets stuck repeating itself.",
  },
  {
    key: "response_format",
    label: "Response format",
    type: "select",
    options: ["json", "text", "verbose_json"],
    help: "Shape of the reply: json (a text field), plain text, or verbose_json with segment-level detail such as timestamps.",
  },
];

const TTS_PARAMS: ParamSpec[] = [
  // Voice names are engine-specific; empty falls through to the binding's default, then the
  // engine's own model default.
  {
    key: "voice",
    label: "Voice",
    type: "text",
    placeholder: "engine default",
    help: "The voice to speak with — voice names are engine-specific. Leave empty to use the model binding's default, then the engine's own default.",
  },
  {
    key: "speed",
    label: "Speed",
    type: "number",
    min: 0.25,
    max: 4,
    step: 0.05,
    help: "Playback speed multiplier: 1 = normal, 0.5 = half speed, 2 = double speed.",
  },
  {
    key: "response_format",
    label: "Format",
    type: "select",
    options: ["mp3", "wav", "opus", "flac", "aac", "pcm"],
    help: "Audio container of the produced speech. mp3 is the compact default; wav/pcm are uncompressed (larger, no decode step).",
  },
];

export const PARAM_SPECS: Record<PanelModality, ParamSpec[]> = {
  chat: CHAT_PARAMS,
  embeddings: EMBEDDINGS_PARAMS,
  "audio.transcription": STT_PARAMS,
  "audio.speech": TTS_PARAMS,
};

/** Narrow a spec list by a model's advertised capabilities (a gated param is hidden, not greyed). */
export function narrowSpecs(specs: ParamSpec[], caps: Capabilities | undefined): ParamSpec[] {
  return specs.filter((spec) => !spec.requiresCap || Boolean(caps?.[spec.requiresCap]));
}

/** The param specs that apply to a modality for a given model — narrowed by its capabilities. */
export function paramsForModality(
  modality: PanelModality,
  caps: Capabilities | undefined,
): ParamSpec[] {
  return narrowSpecs(PARAM_SPECS[modality] ?? [], caps);
}

/**
 * The specs for a graph NODE's `params` dict. Identical to the bench specs except for the speak
 * node, whose config vocabulary names the audio container `format` (the runtime maps it onto the
 * wire's `response_format`) — so the editor writes the key the walker actually reads.
 */
export function specsForNodeParams(modality: PanelModality): ParamSpec[] {
  const specs = PARAM_SPECS[modality] ?? [];
  if (modality !== "audio.speech") return specs;
  return specs.map((s) => (s.key === "response_format" ? { ...s, key: "format" } : s));
}

/**
 * Remap a saved bench preset's LITERAL params onto a node's `params` vocabulary (the speak node's
 * `response_format` → `format` rename); every other modality copies through unchanged.
 */
export function presetParamsForNode(
  modality: PanelModality,
  params: Record<string, unknown>,
): Record<string, unknown> {
  if (modality !== "audio.speech" || !("response_format" in params)) return { ...params };
  const { response_format, ...rest } = params;
  return { ...rest, format: response_format };
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
  // The reasoning toggle: on/off → the chat-template switch object the engines read.
  if (spec.key === "chat_template_kwargs") return { enable_thinking: raw === "on" };
  return raw;
}

/**
 * The inverse of `coerceParam` — turn a stored wire value back into the raw form string, so the
 * editor can seed its fields from params already saved in an agent. An unrecognized shape renders
 * as "" (the field shows unset; the stored value is preserved until the user edits that field).
 */
export function rawFromParam(spec: ParamSpec, value: unknown): string {
  if (value === undefined || value === null) return "";
  if (spec.key === "stop")
    return Array.isArray(value) ? value.map(String).join(", ") : String(value);
  if (spec.key === "response_format" && typeof value === "object")
    return (value as { type?: string }).type === "json_object" ? "json_object" : "";
  if (spec.key === "chat_template_kwargs") {
    if (typeof value !== "object") return "";
    const flag = (value as { enable_thinking?: unknown }).enable_thinking;
    return flag === true ? "on" : flag === false ? "off" : "";
  }
  if (typeof value === "string" || typeof value === "number") return String(value);
  return "";
}
