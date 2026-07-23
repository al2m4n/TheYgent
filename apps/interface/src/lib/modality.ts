// The graph I/O boundary as data. ONE derivation of "what payload does this graph take, and what
// does it hand back", shared by New Chat, a reopened session, the per-agent Run modal, the canvas
// Test panel and the API dialog — so a voice agent behaves identically wherever it is exercised.
//
// Modality is DECLARATIVE server-side: the walker binds the run input to the input node's out-port
// without reading `config.modality` (packages/ir documents it as driving "the editor + trigger
// mapping, not execution"). This module is therefore the only thing standing between what an
// author declared and a payload the graph can actually drill.
//
// Three rules the file rests on:
//   1. **Absent means text.** `InputConfig.modality` defaults to `"text"` in packages/ir, and a
//      node born from `blankGraph` simply has no key. Absent — and the `null` an older editor
//      could write — both read as `"text"`.
//   2. **The vocabulary is derived.** Valid values come from the generated node-type registry's
//      config schema, so a modality added in packages/ir is recognised after a regenerate. The
//      literal unions below mirror it and a test pins them to it.
//   3. **Output is a SET, and the bytes win.** A graph routinely ends on several output boundaries
//      with different shapes (voice-desk: an audio answer plus text error branches), so the
//      declared modality says what a run *may* return, never what this run *did*. What to render
//      is decided from the persisted output itself.
//
// This is the I/O-boundary vocabulary (`text | audio | image | video | json | file`), deliberately
// DISTINCT from the inference plane's model modality (`chat | vision | embeddings | audio.* |
// images.generation`). Never conflate them.

import { type IRDocument, NODE_TYPES } from "@theygent/ir-types";
import type { Attachment, ComposerCaps } from "../chat/types";

/** The payload shape an `input` boundary declares. Mirrors `InputModality` in packages/ir. */
export type InputModality = "text" | "audio" | "image" | "video" | "json" | "file";
/** The result shape an `output` boundary declares. Mirrors `OutputModality` in packages/ir. */
export type OutputModality = "text" | "audio" | "image" | "json";

export const INPUT_MODALITIES: readonly InputModality[] = [
  "text",
  "audio",
  "image",
  "video",
  "json",
  "file",
];
export const OUTPUT_MODALITIES: readonly OutputModality[] = ["text", "audio", "image", "json"];

/** The enum a boundary type's `modality` config field accepts, read off the generated registry.
 *  A test asserts the unions above equal this — the drift guard on the hand-written literals. */
export function schemaModalities(nodeType: "input" | "output"): string[] {
  const properties = (
    NODE_TYPES[nodeType]?.configSchema as { properties?: Record<string, unknown> }
  )?.properties;
  const field = properties?.modality as { enum?: unknown[] } | undefined;
  return Array.isArray(field?.enum) ? field.enum.map(String) : [];
}

/** Modalities whose payload rides as a stored artifact REFERENCE rather than an inline value. */
const REF_INPUTS: ReadonlySet<InputModality> = new Set(["audio", "video", "file"]);

export interface Boundary {
  /** The single typed entry's declared modality; `"text"` when unset. */
  input: InputModality;
  /** EVERY output boundary's modality — a graph may end on several with different shapes. */
  output: ReadonlySet<OutputModality>;
  /** True when some output boundary can hand back an artifact reference (audio/image). */
  mediaOut: boolean;
}

/** What an input-less, unset, or text graph reads as. */
export const TEXT_BOUNDARY: Boundary = {
  input: "text",
  output: new Set<OutputModality>(["text"]),
  mediaOut: false,
};

function readModality(node: unknown): unknown {
  return (node as { config?: Record<string, unknown> } | undefined)?.config?.modality;
}

/**
 * Derive a graph's boundary from its IR. Callers that may not have an IR yet (a version still
 * being fetched) should pass `undefined` and gate on the result, rather than letting a voice agent
 * present a text composer for the first render.
 */
export function boundaryOf(ir?: IRDocument | null): Boundary {
  const nodes = (ir?.nodes ?? []) as unknown[];
  const inputNode = nodes.find((n) => (n as { type?: string }).type === "input");
  const input = coerce(readModality(inputNode), "input") as InputModality;
  const output = new Set<OutputModality>();
  for (const node of nodes) {
    if ((node as { type?: string }).type !== "output") continue;
    output.add(coerce(readModality(node), "output") as OutputModality);
  }
  if (output.size === 0) output.add("text");
  return { input, output, mediaOut: output.has("audio") || output.has("image") };
}

/** Absent, null, or a value the generated schema doesn't know → the schema's own default, text. */
function coerce(value: unknown, nodeType: "input" | "output"): string {
  if (typeof value !== "string" || value === "") return "text";
  const allowed = schemaModalities(nodeType);
  // An out-of-enum value can only come from an IR the server would already reject; showing the
  // text control is the honest degradation — the run itself still fails loudly server-side.
  return allowed.length === 0 || allowed.includes(value) ? value : "text";
}

/** The badge every surface shows for a non-plain boundary (null = an ordinary text agent). */
export function modalityLabel(b: Boundary): string | null {
  if (b.input === "audio" && b.output.has("audio")) return "voice";
  if (b.input === "audio") return "audio in";
  if (b.input === "image") return "vision";
  if (b.input !== "text") return `${b.input} in`;
  if (b.output.has("image")) return "image";
  if (b.output.has("audio")) return "audio";
  return null;
}

/** The file-picker filter for a modality that takes a binary attachment. */
export function acceptFor(modality: InputModality): string | undefined {
  if (modality === "audio") return "audio/*";
  if (modality === "image") return "image/*";
  if (modality === "video") return "video/*";
  return undefined;
}

/**
 * What the composer offers for a boundary. The single mapping from a declared modality to
 * affordances — no surface re-derives it, so the chat composer, the agent Run modal and the canvas
 * Test panel can never drift.
 *
 * `placeholder` is the CALLER's page-level hint for an ordinary conversation ("Message the agent…").
 * A non-textual boundary keeps its own copy instead: what the user must do here is more specific
 * than any page-level phrasing, and letting the generic one win is how a vision composer ends up
 * inviting prose.
 */
export function composerCapsFor(b: Boundary, placeholder?: string): ComposerCaps {
  switch (b.input) {
    case "audio":
      // The clip IS the message — there is nothing to type alongside it.
      return { audio: true, audioRequired: true, textDisabled: true, maxAttachments: 1 };
    case "image":
      // An image AND a question: the graph drills `$in.in.image` / `$in.in.text`, so the image is
      // required while the question is optional. One image — the walker takes a single url.
      return {
        images: true,
        imagesRequired: true,
        maxAttachments: 1,
        placeholder: "Attach an image and ask about it…",
      };
    case "video":
    case "file":
      return {
        files: true,
        filesRequired: true,
        fileAccept: acceptFor(b.input),
        textDisabled: true,
        maxAttachments: 1,
        placeholder: `Attach ${b.input === "video" ? "a video" : "a file"}, then send.`,
      };
    case "json":
      // A structured boundary: the payload is an object the graph drills with `$in.in.<field>`, so
      // the composer becomes a validated JSON editor instead of a prose box.
      return { json: true, placeholder: '{"field": "value"}' };
    default:
      // An image-generation agent takes a text prompt, but what it DOES with it is the useful
      // hint — so this wins over the caller's page-level phrasing, like every other non-plain
      // boundary above. (A caller's placeholder only applies to a plain text-in/text-out agent.)
      if (b.output.has("image")) return { placeholder: "Describe an image to generate…" };
      if (b.output.has("audio")) return { placeholder: "Type something to have it read back…" };
      return { placeholder: placeholder ?? "Send a message…" };
  }
}

/** The composed input a surface has collected, before it becomes a run body. */
export interface RunInputDraft {
  text: string;
  attachments: Attachment[];
  /** Raw JSON text — the `json` modality, or the escape hatch any surface can offer. */
  json?: string;
}

export type BuiltInput = { ok: true; value: unknown } | { ok: false; error: string };

/**
 * Is this draft runnable for the modality? Pure and synchronous, so it can gate a Run/Send button
 * BEFORE a run starts — a missing clip or malformed JSON is refused in the tab, never mid-run.
 */
export function checkRunInput(modality: InputModality, draft: RunInputDraft): BuiltInput {
  if (modality === "json") return parseJsonInput(draft.json ?? draft.text);
  if (modality === "image") {
    return draft.attachments.some((a) => a.kind === "image")
      ? { ok: true, value: undefined }
      : { ok: false, error: "Attach an image — this graph's input boundary is an image." };
  }
  if (REF_INPUTS.has(modality)) {
    return pickBlob(modality, draft.attachments)
      ? { ok: true, value: undefined }
      : { ok: false, error: `Attach ${modality} — this graph's input boundary is ${modality}.` };
  }
  return { ok: true, value: undefined };
}

function pickBlob(modality: InputModality, attachments: Attachment[]): Attachment | undefined {
  const wanted = modality === "audio" ? "audio" : "file";
  return (
    attachments.find((a) => a.kind === wanted && a.blob) ?? attachments.find((a) => Boolean(a.blob))
  );
}

/**
 * Shape the run body's `input`. The counterpart of `composerCapsFor`: what the composer collected
 * becomes exactly the payload the walker's boundary binds.
 *
 *   text (and unset) → the bare string
 *   json             → the parsed object, parsed LOUDLY so a malformed payload never leaves the
 *                      tab as a look-alike string that only errors mid-run
 *   image            → `{ image: <data URI>, text }`, drilled as `$in.in.image` / `$in.in.text`.
 *                      NOT an artifact ref: the llm node string-templates the url into an
 *                      `image_url` part with no artifact fetch, so it must be something the
 *                      inference plane can resolve.
 *   audio|video|file → `{ ref, contentType }` after uploading the bytes, so multi-MB blobs ride as
 *                      a reference and are never journaled through step args.
 *
 * `upload` is injected so tests never touch the network.
 */
export async function buildRunInput(
  modality: InputModality,
  draft: RunInputDraft,
  upload: (blob: Blob) => Promise<{ ref: string; contentType: string }>,
): Promise<BuiltInput> {
  const check = checkRunInput(modality, draft);
  if (!check.ok) return check;
  if (modality === "json") return parseJsonInput(draft.json ?? draft.text);
  if (modality === "image") {
    const image = draft.attachments.find((a) => a.kind === "image");
    if (!image) return { ok: false, error: "Attach an image first." };
    return { ok: true, value: { image: image.url, text: draft.text } };
  }
  if (REF_INPUTS.has(modality)) {
    const attachment = pickBlob(modality, draft.attachments);
    if (!attachment?.blob) return { ok: false, error: `Attach ${modality} first.` };
    const ref = await upload(attachment.blob);
    return { ok: true, value: { ref: ref.ref, contentType: ref.contentType } };
  }
  return { ok: true, value: draft.text };
}

/** The loud JSON parse every typed-input surface shares. An empty box means "no input" (null). */
export function parseJsonInput(raw: string): BuiltInput {
  if (raw.trim() === "") return { ok: true, value: null };
  try {
    return { ok: true, value: JSON.parse(raw) };
  } catch (e) {
    return {
      ok: false,
      error: `Invalid JSON input: ${e instanceof Error ? e.message : String(e)}`,
    };
  }
}

/** What a finished run's persisted output should be RENDERED as. */
export type RunOutput =
  | { kind: "text"; text: string }
  | { kind: "json"; text: string }
  | { kind: "artifact"; ref: string }
  | { kind: "empty" };

/**
 * Classify a run's persisted output. Decided from the BYTES, not from the declared modality — a
 * multi-output graph returns whichever boundary actually fired, so a voice agent whose
 * transcription failed hands back prose on its error branch and must render as prose.
 */
export function classifyRunOutput(output: string | null | undefined, b: Boundary): RunOutput {
  if (!output) return { kind: "empty" };
  if (b.mediaOut) {
    const ref = artifactRefOf(output);
    if (ref) return { kind: "artifact", ref };
  }
  if (b.output.has("json") && isJsonObject(output)) return { kind: "json", text: output };
  return { kind: "text", text: output };
}

function isJsonObject(text: string): boolean {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return false;
  try {
    JSON.parse(trimmed);
    return true;
  } catch {
    return false;
  }
}

/** The artifact id inside a media run's output, or null when it carries something else. */
export function artifactRefOf(output?: string | null): string | null {
  if (!output) return null;
  try {
    const parsed = JSON.parse(output) as { ref?: unknown };
    return typeof parsed.ref === "string" && parsed.ref ? parsed.ref : null;
  } catch {
    return null;
  }
}

/** The attachment kind a downloaded artifact renders as, from the blob's own media type. */
export function attachmentKindFor(contentType: string | undefined, b: Boundary): "image" | "audio" {
  if (contentType?.startsWith("image/")) return "image";
  if (contentType?.startsWith("audio/")) return "audio";
  return b.output.has("image") ? "image" : "audio";
}
