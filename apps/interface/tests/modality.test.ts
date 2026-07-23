// The I/O-boundary seam: one derivation of what a graph takes and returns, one payload builder,
// one output classifier — shared by New Chat, session continuation, the agent Run modal and the
// canvas Test panel. These are the pure functions all four surfaces sit on, so their behaviour is
// pinned here rather than re-asserted per surface.

import { NODE_TYPES } from "@theygent/ir-types";
import type { IRDocument } from "@theygent/ir-types";
import { describe, expect, it, vi } from "vitest";
import type { Attachment } from "../src/chat/types";
import { effectiveDraft, effectiveModality, runInputError } from "../src/components/RunInputField";
import {
  INPUT_MODALITIES,
  OUTPUT_MODALITIES,
  TEXT_BOUNDARY,
  boundaryOf,
  buildRunInput,
  checkRunInput,
  classifyRunOutput,
  composerCapsFor,
  modalityLabel,
  schemaModalities,
} from "../src/lib/modality";

type NodeSpec = { id: string; type: string; config?: Record<string, unknown> };

function ir(nodes: NodeSpec[]): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "agent.t",
    name: "t",
    version: "0.1.0",
    models: {},
    tools: {},
    nodes: nodes.map((n) => ({ kind: "boundary", ports: {}, ...n })),
    edges: [],
  } as unknown as IRDocument;
}

const upload = vi.fn(async () => ({ ref: "art_1", contentType: "audio/webm" }));

describe("boundaryOf", () => {
  it("defaults to text when the input node declares no modality", () => {
    // The common case: `blankGraph` writes `config: {}`, so the key is ABSENT — and the backend's
    // own Pydantic default is text.
    expect(boundaryOf(ir([{ id: "n_in", type: "input", config: {} }])).input).toBe("text");
    expect(boundaryOf(ir([{ id: "n_in", type: "input" }])).input).toBe("text");
  });

  it("treats a null modality as unset, not as an error", () => {
    // An older editor could write null through the enum field's blank option. The IR is invalid
    // server-side, but the composer must still present something usable rather than nothing.
    expect(boundaryOf(ir([{ id: "n_in", type: "input", config: { modality: null } }])).input).toBe(
      "text",
    );
  });

  it("falls back to text for a value outside the generated enum", () => {
    const b = boundaryOf(ir([{ id: "n_in", type: "input", config: { modality: "hologram" } }]));
    expect(b.input).toBe("text");
  });

  it("reads a declared modality", () => {
    expect(
      boundaryOf(ir([{ id: "n_in", type: "input", config: { modality: "audio" } }])).input,
    ).toBe("audio");
    expect(
      boundaryOf(ir([{ id: "n_in", type: "input", config: { modality: "json" } }])).input,
    ).toBe("json");
  });

  it("returns the text boundary for a missing document or a graph with no input node", () => {
    expect(boundaryOf(undefined)).toEqual(TEXT_BOUNDARY);
    expect(boundaryOf(ir([{ id: "n_llm", type: "llm" }]))).toEqual(TEXT_BOUNDARY);
  });

  it("collects EVERY output boundary — a graph ends on whichever one fires", () => {
    // The voice-desk shape: an audio answer plus two text error branches. Collapsing to the first
    // node's modality would mis-render one of the two.
    const b = boundaryOf(
      ir([
        { id: "n_in", type: "input", config: { modality: "audio" } },
        { id: "n_out", type: "output", config: { modality: "audio" } },
        { id: "n_stt_failed", type: "output", config: {} },
        { id: "n_tts_failed", type: "output", config: {} },
      ]),
    );
    expect([...b.output].sort()).toEqual(["audio", "text"]);
    expect(b.mediaOut).toBe(true);
  });

  it("has no media output when every boundary is textual", () => {
    const b = boundaryOf(
      ir([
        { id: "n_in", type: "input", config: {} },
        { id: "n_out", type: "output", config: {} },
      ]),
    );
    expect(b.mediaOut).toBe(false);
  });
});

describe("the modality vocabulary", () => {
  // The literal unions are hand-written (the generated `ir.d.ts` types `config` as an opaque dict),
  // so they must be pinned to the registry the palette and the validator already use. A modality
  // added in packages/ir fails here until it is handled.
  it("matches the generated node-type registry", () => {
    expect([...INPUT_MODALITIES]).toEqual(schemaModalities("input"));
    expect([...OUTPUT_MODALITIES]).toEqual(schemaModalities("output"));
    expect(schemaModalities("input")).toEqual(
      (NODE_TYPES.input.configSchema as { properties: { modality: { enum: string[] } } }).properties
        .modality.enum,
    );
  });
});

describe("composerCapsFor", () => {
  it("makes the clip the message for an audio boundary", () => {
    const caps = composerCapsFor(
      boundaryOf(ir([{ id: "i", type: "input", config: { modality: "audio" } }])),
    );
    expect(caps).toMatchObject({ audio: true, audioRequired: true, textDisabled: true });
  });

  it("requires the image on a vision boundary while keeping the question optional", () => {
    const caps = composerCapsFor(
      boundaryOf(ir([{ id: "i", type: "input", config: { modality: "image" } }])),
    );
    expect(caps.images).toBe(true);
    expect(caps.imagesRequired).toBe(true);
    expect(caps.textDisabled).toBeUndefined();
  });

  it("turns the prose box into a JSON editor for a structured boundary", () => {
    const caps = composerCapsFor(
      boundaryOf(ir([{ id: "i", type: "input", config: { modality: "json" } }])),
    );
    expect(caps.json).toBe(true);
  });

  it("offers a filtered file picker for video and file boundaries", () => {
    const caps = composerCapsFor(
      boundaryOf(ir([{ id: "i", type: "input", config: { modality: "video" } }])),
    );
    expect(caps).toMatchObject({ files: true, filesRequired: true, fileAccept: "video/*" });
  });

  it("caps attachments at one — the boundary takes a single payload", () => {
    for (const m of ["audio", "image", "video", "file"]) {
      const caps = composerCapsFor(
        boundaryOf(ir([{ id: "i", type: "input", config: { modality: m } }])),
      );
      expect(caps.maxAttachments).toBe(1);
    }
  });
});

describe("checkRunInput", () => {
  it("refuses a vision turn with no image — the graph drills $in.in.image", () => {
    const result = checkRunInput("image", { text: "what is this?", attachments: [] });
    expect(result.ok).toBe(false);
  });

  it("refuses an audio turn with no clip", () => {
    expect(checkRunInput("audio", { text: "", attachments: [] }).ok).toBe(false);
  });

  it("accepts an empty text box — a text boundary asks for nothing more", () => {
    expect(checkRunInput("text", { text: "", attachments: [] }).ok).toBe(true);
  });

  it("refuses malformed JSON before a run starts", () => {
    expect(checkRunInput("json", { text: "", attachments: [], json: "{oops" }).ok).toBe(false);
    expect(checkRunInput("json", { text: "", attachments: [], json: '{"a":1}' }).ok).toBe(true);
  });
});

describe("buildRunInput", () => {
  const clip: Attachment = {
    kind: "audio",
    url: "blob:x",
    blob: new Blob(["a"], { type: "audio/webm" }),
    name: "clip",
  };
  const image: Attachment = { kind: "image", url: "data:image/png;base64,AAA", name: "shot" };

  it("sends a bare string for a text boundary", async () => {
    upload.mockClear();
    const built = await buildRunInput("text", { text: "hello", attachments: [] }, upload);
    expect(built).toEqual({ ok: true, value: "hello" });
    expect(upload).not.toHaveBeenCalled();
  });

  it("parses a json boundary loudly and never uploads", async () => {
    upload.mockClear();
    const good = await buildRunInput(
      "json",
      { text: "", attachments: [], json: '{"code":"DE"}' },
      upload,
    );
    expect(good).toEqual({ ok: true, value: { code: "DE" } });
    const bad = await buildRunInput("json", { text: "", attachments: [], json: "{oops" }, upload);
    expect(bad.ok).toBe(false);
    expect(upload).not.toHaveBeenCalled();
  });

  it("uploads an audio clip and sends only the reference", async () => {
    upload.mockClear();
    const built = await buildRunInput("audio", { text: "", attachments: [clip] }, upload);
    expect(built).toEqual({ ok: true, value: { ref: "art_1", contentType: "audio/webm" } });
    expect(upload).toHaveBeenCalledOnce();
    expect(upload).toHaveBeenCalledWith(clip.blob);
  });

  it("sends an image INLINE, not as a reference", async () => {
    // The llm node string-templates the url into an image_url content part with no artifact fetch,
    // so an `art_…` ref would reach the model as a literal string.
    upload.mockClear();
    const built = await buildRunInput("image", { text: "what?", attachments: [image] }, upload);
    expect(built).toEqual({
      ok: true,
      value: { image: "data:image/png;base64,AAA", text: "what?" },
    });
    expect(upload).not.toHaveBeenCalled();
  });

  it("uploads video and file boundaries through the same artifact contract as audio", async () => {
    const file: Attachment = {
      kind: "file",
      url: "blob:y",
      blob: new Blob(["v"], { type: "video/mp4" }),
      name: "clip.mp4",
      mediaType: "video/mp4",
    };
    for (const m of ["video", "file"] as const) {
      // A distinct upload result per modality, so this asserts the STORE's answer propagates —
      // echoing the shared mock's default would prove nothing.
      const store = vi.fn(async () => ({ ref: `art_${m}`, contentType: "video/mp4" }));
      const built = await buildRunInput(m, { text: "", attachments: [file] }, store);
      expect(built).toEqual({ ok: true, value: { ref: `art_${m}`, contentType: "video/mp4" } });
      expect(store).toHaveBeenCalledWith(file.blob);
    }
  });

  it("fails without starting a run when the required attachment is missing", async () => {
    upload.mockClear();
    const built = await buildRunInput("audio", { text: "hi", attachments: [] }, upload);
    expect(built.ok).toBe(false);
    expect(upload).not.toHaveBeenCalled();
  });
});

// The single-shot surfaces (bench Run modal, canvas Test panel) route a draft through these two
// helpers before `buildRunInput`. A json boundary has two routes to the same payload — its native
// control and the explicit JSON mode — and BOTH must land the text where the builder reads it.
describe("effectiveDraft / effectiveModality", () => {
  const jsonBoundary = boundaryOf(ir([{ id: "i", type: "input", config: { modality: "json" } }]));
  const textBoundary = boundaryOf(ir([{ id: "i", type: "input", config: {} }]));

  it("routes a json boundary's NATIVE box into the payload, not into oblivion", async () => {
    // Regression: the draft seeds `json: ""`, which is not nullish, so `draft.json ?? draft.text`
    // never fell through — the typed object validated in the UI and then `null` reached the graph.
    const state = {
      mode: "native" as const,
      draft: { text: '{"code":"DE"}', attachments: [], json: "" },
    };
    expect(runInputError(jsonBoundary, state)).toBeNull();
    const built = await buildRunInput(
      effectiveModality(jsonBoundary, state),
      effectiveDraft(jsonBoundary, state),
      upload,
    );
    expect(built).toEqual({ ok: true, value: { code: "DE" } });
  });

  it("routes the explicit JSON mode's box into the payload on ANY boundary", async () => {
    const state = {
      mode: "json" as const,
      draft: { text: "ignored", attachments: [], json: '{"a":1}' },
    };
    expect(effectiveModality(textBoundary, state)).toBe("json");
    const built = await buildRunInput(
      effectiveModality(textBoundary, state),
      effectiveDraft(textBoundary, state),
      upload,
    );
    expect(built).toEqual({ ok: true, value: { a: 1 } });
  });

  it("leaves a plain text boundary's draft alone", () => {
    const state = { mode: "native" as const, draft: { text: "hi", attachments: [], json: "" } };
    expect(effectiveDraft(textBoundary, state)).toBe(state.draft);
  });
});

describe("classifyRunOutput", () => {
  const voice = boundaryOf(
    ir([
      { id: "n_in", type: "input", config: { modality: "audio" } },
      { id: "n_out", type: "output", config: { modality: "audio" } },
      { id: "n_failed", type: "output", config: {} },
    ]),
  );

  it("resolves an artifact reference on a media boundary", () => {
    expect(
      classifyRunOutput(JSON.stringify({ ref: "art_9", contentType: "audio/wav" }), voice),
    ).toEqual({ kind: "artifact", ref: "art_9" });
  });

  it("renders prose as prose even on a media boundary — the error branch fired", () => {
    expect(classifyRunOutput("transcribe failed: no launcher", voice)).toEqual({
      kind: "text",
      text: "transcribe failed: no launcher",
    });
  });

  it("reports an empty output rather than a blank bubble", () => {
    expect(classifyRunOutput(null, voice)).toEqual({ kind: "empty" });
    expect(classifyRunOutput("", voice)).toEqual({ kind: "empty" });
  });

  it("marks a json boundary's structured answer for code rendering", () => {
    const jsonOut = boundaryOf(
      ir([
        { id: "n_in", type: "input", config: {} },
        { id: "n_out", type: "output", config: { modality: "json" } },
      ]),
    );
    expect(classifyRunOutput('{"verdict":"pass"}', jsonOut).kind).toBe("json");
    // Prose on the same boundary is still prose.
    expect(classifyRunOutput("all good", jsonOut).kind).toBe("text");
  });

  it("never mistakes a text boundary's JSON-looking answer for an artifact", () => {
    const text = boundaryOf(ir([{ id: "n_out", type: "output", config: {} }]));
    expect(classifyRunOutput('{"ref":"art_9"}', text).kind).toBe("text");
  });
});

describe("modalityLabel", () => {
  const at = (input?: string, output?: string) =>
    modalityLabel(
      boundaryOf(
        ir([
          { id: "n_in", type: "input", config: input ? { modality: input } : {} },
          { id: "n_out", type: "output", config: output ? { modality: output } : {} },
        ]),
      ),
    );

  it("names the recognisable shapes and stays silent on a plain text agent", () => {
    expect(at("audio", "audio")).toBe("voice");
    expect(at("image")).toBe("vision");
    expect(at(undefined, "image")).toBe("image");
    expect(at("json")).toBe("json in");
    expect(at()).toBeNull();
  });
});
