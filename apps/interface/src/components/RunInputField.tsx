// The single-shot counterpart of the chat composer: one input control that respects a graph's
// declared input boundary, shared by the per-agent Run modal and the editor's canvas Test panel.
// The chat composer (`chat/Composer.tsx`) is conversation-shaped — chips, Enter-sends, a send
// icon; a Run surface needs a value plus its own Run button. Both read the SAME `ComposerCaps`
// from `lib/modality.ts` and produce the SAME run payload through `buildRunInput`, so what a
// graph accepts can never differ between where it is tested and where it is used.
//
// Every modality keeps a raw-JSON escape hatch: a multi-input graph (`$in.<port>.<field>`) may
// need a hand-written object regardless of what its boundary declares, and that is exactly the
// case an author is testing on the canvas.

import { AudioLines, Camera, FileUp, ImagePlus, Mic, Square, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { CameraModal } from "../chat/CameraModal";
import { fileToDataUrl, useRecorder } from "../chat/media";
import type { Attachment } from "../chat/types";
import { type Boundary, type RunInputDraft, acceptFor, parseJsonInput } from "../lib/modality";
import { notify } from "../lib/notify";
import { Button, Input, Select } from "./ui";

/** How the value is being entered: the boundary's own control, or hand-written JSON. */
export type RunInputMode = "native" | "json";

export interface RunInputState {
  mode: RunInputMode;
  draft: RunInputDraft;
}

/** The draft a surface starts from, and returns to after a modality switch. */
export function emptyRunInput(boundary: Boundary): RunInputState {
  return {
    // A json boundary opens in JSON — the author should not have to find the toggle first.
    mode: boundary.input === "json" ? "json" : "native",
    draft: { text: "", attachments: [], json: "" },
  };
}

/** The parse/attachment problem blocking a run, or null when the draft is runnable. */
export function runInputError(boundary: Boundary, state: RunInputState): string | null {
  if (state.mode === "json") {
    const parsed = parseJsonInput(state.draft.json ?? "");
    return parsed.ok ? null : parsed.error;
  }
  if (boundary.input === "json") {
    const parsed = parseJsonInput(state.draft.text);
    return parsed.ok ? null : parsed.error;
  }
  if (boundary.input === "image" && !state.draft.attachments.some((a) => a.kind === "image")) {
    return "Attach an image — this graph's input boundary is an image.";
  }
  if (
    (boundary.input === "audio" || boundary.input === "video" || boundary.input === "file") &&
    !state.draft.attachments.some((a) => a.blob)
  ) {
    return `Attach ${boundary.input} — this graph's input boundary is ${boundary.input}.`;
  }
  return null;
}

/** The effective modality for building the payload — JSON mode overrides the declared one. */
export function effectiveModality(boundary: Boundary, state: RunInputState) {
  return state.mode === "json" ? ("json" as const) : boundary.input;
}

/**
 * The draft `buildRunInput` should read. Both routes that end in a JSON payload — the explicit
 * JSON mode and a `json` BOUNDARY in its own native mode — must put the text in `draft.json`,
 * because that is the field the builder reads. Leaving a json boundary's value in `draft.text`
 * would validate as a parsed object in the UI and then send `null` (the builder's `draft.json ??
 * draft.text` never falls through, since the seeded `json` is `""`, not nullish).
 */
export function effectiveDraft(boundary: Boundary, state: RunInputState): RunInputDraft {
  if (state.mode === "json") return { text: "", attachments: [], json: state.draft.json ?? "" };
  if (boundary.input === "json") return { ...state.draft, json: state.draft.text };
  return state.draft;
}

export function RunInputField({
  boundary,
  state,
  onChange,
  onSubmit,
  disabled,
  compact,
}: {
  boundary: Boundary;
  state: RunInputState;
  onChange: (next: RunInputState) => void;
  /** Enter in a single-line control runs, exactly like the chat composer sends. */
  onSubmit: () => void;
  disabled?: boolean;
  /** The editor's header rail is horizontal and tight; the bench modal has room to breathe. */
  compact?: boolean;
}) {
  const recorder = useRecorder();
  const imageRef = useRef<HTMLInputElement | null>(null);
  const audioRef = useRef<HTMLInputElement | null>(null);
  const fileRef = useRef<HTMLInputElement | null>(null);
  const [cameraOpen, setCameraOpen] = useState(false);

  const modality = boundary.input;
  const isJson = state.mode === "json";
  const takesMedia =
    modality === "audio" || modality === "image" || modality === "video" || modality === "file";

  // On the canvas the boundary is LIVE: the author can retype the input node's modality mid-session.
  // A clip staged for an audio boundary must not survive a switch to text, or the payload would be
  // built from a modality that no longer applies. Skips the first render so a surface can seed its
  // own initial draft.
  const seenModality = useRef(modality);
  // Read-latest so the reset can revoke what is ACTUALLY staged without re-running on every keystroke.
  const draftRef = useRef(state.draft);
  draftRef.current = state.draft;
  // biome-ignore lint/correctness/useExhaustiveDependencies: reset only when the modality changes
  useEffect(() => {
    if (seenModality.current === modality) return;
    seenModality.current = modality;
    revokeStaged(draftRef.current.attachments);
    onChange(emptyRunInput(boundary));
  }, [modality]);

  const set = (patch: Partial<RunInputDraft>) =>
    onChange({ ...state, draft: { ...state.draft, ...patch } });

  function addAttachment(a: Attachment) {
    // The boundary takes one payload; replace rather than stack, so what will be sent is never
    // ambiguous.
    revokeStaged(state.draft.attachments);
    set({ attachments: [a] });
  }

  async function toggleMic() {
    if (recorder.recording) {
      const rec = await recorder.stop();
      if (rec) {
        addAttachment({
          kind: "audio",
          url: URL.createObjectURL(rec.blob),
          blob: rec.blob,
          name: "recording",
          durationSec: rec.durationSec,
        });
      }
    } else {
      await recorder.start();
    }
  }

  // A `json` boundary's native control IS the JSON editor, so it gets ONE option — two entries
  // both reading "JSON" would be a coin flip between two code paths.
  const modeSelect = (
    <Select
      value={modality === "json" ? "json" : state.mode}
      onChange={(e) => onChange({ ...state, mode: e.target.value as RunInputMode })}
      className={compact ? "w-24" : "w-28"}
      aria-label="Input mode"
      title="How to enter the run input — the boundary's own control, or a hand-written JSON payload"
    >
      {modality !== "json" && <option value="native">{NATIVE_LABEL[modality]}</option>}
      <option value="json">JSON</option>
    </Select>
  );

  if (isJson || modality === "json") {
    const value = isJson ? (state.draft.json ?? "") : state.draft.text;
    return (
      <div className="flex items-start gap-2">
        {modeSelect}
        <textarea
          value={value}
          onChange={(e) => (isJson ? set({ json: e.target.value }) : set({ text: e.target.value }))}
          placeholder='{"field": "value"}'
          aria-label="Run input"
          disabled={disabled}
          spellCheck={false}
          className={`mono min-h-0 flex-1 rounded-md border border-slate-700 bg-[var(--c-surface)] px-2 py-1.5 text-xs outline-none focus:border-blue-500 ${
            compact ? "h-9" : "h-20"
          }`}
        />
      </div>
    );
  }

  if (takesMedia) {
    const staged = state.draft.attachments[0];
    return (
      <div className="space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          {modeSelect}
          {modality === "audio" && (
            <>
              <Button
                variant={recorder.recording ? "primary" : "ghost"}
                onClick={() => void toggleMic()}
                aria-label={recorder.recording ? "Stop recording" : "Record from microphone"}
                title={recorder.recording ? "Stop recording" : "Record from microphone"}
                disabled={disabled}
              >
                {recorder.recording ? <Square size={14} /> : <Mic size={14} />}
              </Button>
              <Button
                variant="ghost"
                onClick={() => audioRef.current?.click()}
                aria-label="Attach audio file"
                title="Attach audio file"
                disabled={disabled}
              >
                <AudioLines size={14} />
              </Button>
              <input
                ref={audioRef}
                type="file"
                accept="audio/*"
                aria-label="Audio file"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (!f) return;
                  // No duration probe here: this is a run input, not a bench metric, and the
                  // preview player below reports length itself. Staging must never wait on it.
                  addAttachment({
                    kind: "audio",
                    url: URL.createObjectURL(f),
                    blob: f,
                    name: f.name,
                  });
                }}
              />
            </>
          )}
          {modality === "image" && (
            <>
              <Button
                variant="ghost"
                onClick={() => imageRef.current?.click()}
                aria-label="Attach image"
                title="Attach image"
                disabled={disabled}
              >
                <ImagePlus size={14} />
              </Button>
              <Button
                variant="ghost"
                onClick={() => setCameraOpen(true)}
                aria-label="Capture from camera"
                title="Capture from camera"
                disabled={disabled}
              >
                <Camera size={14} />
              </Button>
              <input
                ref={imageRef}
                type="file"
                accept="image/*"
                aria-label="Image file"
                className="hidden"
                onChange={async (e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (!f) return;
                  try {
                    addAttachment({ kind: "image", url: await fileToDataUrl(f), name: f.name });
                  } catch (err) {
                    notify.error(err instanceof Error ? err.message : String(err));
                  }
                }}
              />
              <Input
                value={state.draft.text}
                onChange={(e) => set({ text: e.target.value })}
                placeholder="Ask about the image…"
                aria-label="Run input"
                className="min-w-40 flex-1"
                disabled={disabled}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                    e.preventDefault();
                    onSubmit();
                  }
                }}
              />
            </>
          )}
          {(modality === "video" || modality === "file") && (
            <>
              <Button
                variant="ghost"
                onClick={() => fileRef.current?.click()}
                aria-label={`Attach ${modality}`}
                title={`Attach ${modality}`}
                disabled={disabled}
              >
                <FileUp size={14} />
              </Button>
              <input
                ref={fileRef}
                type="file"
                accept={acceptFor(modality)}
                aria-label={`${modality} file`}
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  e.target.value = "";
                  if (!f) return;
                  addAttachment({
                    kind: "file",
                    url: URL.createObjectURL(f),
                    blob: f,
                    name: f.name,
                    mediaType: f.type,
                  });
                }}
              />
            </>
          )}
          {staged ? (
            <span className="inline-flex items-center gap-1 rounded border border-slate-700 px-1.5 py-0.5 text-[11px] text-slate-300">
              {staged.name ?? staged.kind}
              <button
                type="button"
                aria-label="Remove attachment"
                onClick={() => {
                  if (staged.kind !== "image" && staged.url.startsWith("blob:")) {
                    URL.revokeObjectURL(staged.url);
                  }
                  set({ attachments: [] });
                }}
                className="text-slate-500 hover:text-slate-200"
              >
                <X size={11} />
              </button>
            </span>
          ) : (
            <span className="text-[11px] text-slate-500">
              {recorder.recording ? "Recording… stop to stage the clip." : `${modality} input`}
            </span>
          )}
        </div>
        {staged?.kind === "audio" && (
          // biome-ignore lint/a11y/useMediaCaption: staged clip, no caption source
          <audio controls src={staged.url} className="h-8 w-56" />
        )}
        {staged?.kind === "image" && (
          <img
            src={staged.url}
            alt={staged.name ?? "staged"}
            className="max-h-24 rounded border border-slate-700 object-contain"
          />
        )}
        {recorder.error && <p className="text-xs text-destructive">{recorder.error}</p>}
        {cameraOpen && (
          <CameraModal
            onClose={() => setCameraOpen(false)}
            onCapture={(dataUrl) => {
              setCameraOpen(false);
              addAttachment({ kind: "image", url: dataUrl, name: "camera" });
            }}
          />
        )}
      </div>
    );
  }

  return (
    <div className="flex items-start gap-2">
      {modeSelect}
      <Input
        value={state.draft.text}
        onChange={(e) => set({ text: e.target.value })}
        placeholder="Run input…"
        aria-label="Run input"
        className="flex-1"
        disabled={disabled}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.nativeEvent.isComposing) {
            e.preventDefault();
            onSubmit();
          }
        }}
      />
    </div>
  );
}

/** Release object URLs for staged blobs — each pins its bytes for the page lifetime otherwise.
 *  (An image is staged as a data URI, which owns nothing to release.) */
function revokeStaged(attachments: Attachment[]): void {
  for (const a of attachments) {
    if (a.kind !== "image" && a.url.startsWith("blob:")) URL.revokeObjectURL(a.url);
  }
}

const NATIVE_LABEL: Record<string, string> = {
  text: "Text",
  json: "JSON",
  audio: "Audio",
  image: "Image",
  video: "Video",
  file: "File",
};
