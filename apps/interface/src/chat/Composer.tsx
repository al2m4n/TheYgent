// The chat composer: text plus whatever the target's modality allows — image attach (upload or
// camera) for vision, audio attach (upload or microphone) for transcription, a generic file attach
// for a file/video boundary, and a validated JSON editor for a structured one. Enter sends,
// Shift+Enter breaks the line (in JSON mode Enter breaks the line and the send button submits —
// an object is written across lines). Attachments stage as removable chips above the input so what
// is about to be sent is always visible. The input is an input-group: a growing textarea with the
// media buttons and the send control on a rail beneath it.
//
// What it offers is never decided here: `caps` comes from `lib/modality.ts` (graph boundaries) or
// `useInferenceChat` (direct model targets), so every surface shows the same controls for the same
// target.

import {
  AudioLines,
  Camera,
  CircleStop,
  FileUp,
  ImagePlus,
  Mic,
  Send,
  Square,
  X,
} from "lucide-react";
import { useRef, useState } from "react";
import {
  AttachmentAction,
  AttachmentActions,
  Attachment as AttachmentChip,
  AttachmentContent,
  AttachmentDescription,
  AttachmentGroup,
  AttachmentMedia,
  AttachmentTitle,
} from "../components/ui/attachment";
import {
  InputGroup,
  InputGroupAddon,
  InputGroupButton,
  InputGroupTextarea,
} from "../components/ui/input-group";
import { parseJsonInput } from "../lib/modality";
import { notify } from "../lib/notify";
import { CameraModal } from "./CameraModal";
import { audioDurationSec, fileToDataUrl, useRecorder } from "./media";
import type { Attachment, ComposerCaps } from "./types";

/** The inline parse note for JSON mode — the same loud parse the run body will use. */
function parseJsonError(raw: string): string | null {
  const parsed = parseJsonInput(raw);
  return parsed.ok ? null : parsed.error;
}

export function Composer({
  caps,
  busy,
  disabled,
  disabledNote,
  onSend,
  onStop,
}: {
  caps: ComposerCaps;
  busy: boolean;
  disabled?: boolean;
  disabledNote?: string;
  onSend: (text: string, attachments: Attachment[]) => void | Promise<void>;
  onStop?: () => void;
}) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<Attachment[]>([]);
  const [cameraOpen, setCameraOpen] = useState(false);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const audioInputRef = useRef<HTMLInputElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const recorder = useRecorder();

  const hasAudio = attachments.some((a) => a.kind === "audio");
  const hasImage = attachments.some((a) => a.kind === "image");
  const hasFile = attachments.some((a) => a.kind === "file");
  // In JSON mode the box holds the payload, so it must parse before anything is sent — the same
  // loud parse the single-shot run surfaces use, shown inline instead of failing mid-run.
  const jsonError = caps.json && text.trim() !== "" ? parseJsonError(text) : null;
  // The boundary takes one payload; refuse a second attachment here rather than silently dropping
  // it when the run body is built.
  const atCap = caps.maxAttachments != null && attachments.length >= caps.maxAttachments;
  const sendable =
    !busy &&
    !disabled &&
    (text.trim().length > 0 || attachments.length > 0) &&
    (!caps.audioRequired || hasAudio) &&
    (!caps.imagesRequired || hasImage) &&
    (!caps.filesRequired || hasFile) &&
    !jsonError;

  function addAttachment(a: Attachment) {
    if (atCap) {
      // The caller already minted an object URL for the blob; refusing it without releasing that
      // URL would pin the bytes for the page lifetime.
      if (a.kind !== "image" && a.url.startsWith("blob:")) URL.revokeObjectURL(a.url);
      notify.error("This target takes one attachment — remove the staged one first.");
      return;
    }
    setAttachments((cur) => [...cur, a]);
  }

  async function submit() {
    if (!sendable) return;
    const t = text;
    const atts = attachments;
    setText("");
    setAttachments([]);
    await onSend(t.trim(), atts);
  }

  async function onPickImage(file: File) {
    try {
      addAttachment({ kind: "image", url: await fileToDataUrl(file), name: file.name });
    } catch (e) {
      notify.error(e instanceof Error ? e.message : String(e));
    }
  }

  function onPickAudio(file: File) {
    // Stage the clip IMMEDIATELY and fill the duration in when it is known: the length is a
    // best-effort bench metric, and a codec the browser cannot decode never resolves it — which
    // would otherwise leave the composer looking like it dropped the file.
    const url = URL.createObjectURL(file);
    addAttachment({ kind: "audio", url, blob: file, name: file.name });
    void audioDurationSec(file).then((durationSec) => {
      if (!durationSec) return;
      setAttachments((cur) =>
        cur.map((a) => (a.kind === "audio" && a.url === url ? { ...a, durationSec } : a)),
      );
    });
  }

  function onPickFile(file: File) {
    addAttachment({
      kind: "file",
      url: URL.createObjectURL(file),
      blob: file,
      name: file.name,
      mediaType: file.type,
    });
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

  if (disabled) {
    return (
      <p className="rounded-lg border border-dashed px-3 py-2 text-xs text-muted-foreground">
        {disabledNote ?? "This conversation can't be continued."}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {attachments.length > 0 && (
        <AttachmentGroup>
          {attachments.map((a, i) => (
            <AttachmentChip key={`${a.url}-${i}`} size="sm">
              <AttachmentMedia variant={a.kind === "image" ? "image" : "icon"}>
                {a.kind === "image" ? (
                  <img src={a.url} alt={a.name ?? "attachment"} />
                ) : a.kind === "file" ? (
                  <FileUp aria-hidden />
                ) : (
                  <AudioLines aria-hidden />
                )}
              </AttachmentMedia>
              <AttachmentContent>
                <AttachmentTitle>{a.name ?? a.kind}</AttachmentTitle>
                {a.kind === "audio" ? (
                  <>
                    {/* biome-ignore lint/a11y/useMediaCaption: staged voice clip, no caption source */}
                    <audio controls src={a.url} className="mt-1 h-8 w-44" />
                    {a.durationSec ? (
                      <AttachmentDescription>{a.durationSec.toFixed(1)}s</AttachmentDescription>
                    ) : null}
                  </>
                ) : a.kind === "file" ? (
                  <AttachmentDescription>
                    {a.mediaType || "file"} · {Math.max(1, Math.round(a.blob.size / 1024))} KB
                  </AttachmentDescription>
                ) : null}
              </AttachmentContent>
              <AttachmentActions>
                <AttachmentAction
                  aria-label="Remove attachment"
                  onClick={() => {
                    // A discarded blob's object URL would pin it for the page lifetime.
                    if (a.kind !== "image" && a.url.startsWith("blob:")) URL.revokeObjectURL(a.url);
                    setAttachments((cur) => cur.filter((_, j) => j !== i));
                  }}
                >
                  <X />
                </AttachmentAction>
              </AttachmentActions>
            </AttachmentChip>
          ))}
        </AttachmentGroup>
      )}
      {recorder.error && <p className="text-xs text-destructive">{recorder.error}</p>}
      {jsonError && <p className="text-xs text-amber-400">{jsonError}</p>}
      <InputGroup>
        {!caps.textDisabled ? (
          <InputGroupTextarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={caps.placeholder ?? "Send a message…"}
            aria-label={caps.json ? "JSON payload" : undefined}
            className={caps.json ? "mono max-h-56 min-h-20" : "max-h-40"}
            onKeyDown={(e) => {
              // isComposing: an IME Enter commits the composition candidate, it doesn't send.
              // A JSON payload is written across lines, so Enter breaks the line there and the
              // send control submits — Enter-to-send would truncate an object mid-write.
              if (e.key === "Enter" && !e.shiftKey && !caps.json && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void submit();
              }
            }}
          />
        ) : (
          <p className="w-full px-3 pt-2 text-xs text-muted-foreground">
            {recorder.recording
              ? "Recording… stop to stage the clip."
              : (caps.placeholder ?? "Record or attach audio, then send.")}
          </p>
        )}
        <InputGroupAddon align="block-end" className="gap-1">
          {caps.images && (
            <>
              <InputGroupButton
                size="icon-xs"
                aria-label="Attach image"
                title="Attach image"
                onClick={() => imageInputRef.current?.click()}
              >
                <ImagePlus />
              </InputGroupButton>
              <InputGroupButton
                size="icon-xs"
                aria-label="Capture from camera"
                title="Capture from camera"
                onClick={() => setCameraOpen(true)}
              >
                <Camera />
              </InputGroupButton>
              <input
                ref={imageInputRef}
                type="file"
                accept="image/*"
                aria-label="Image file"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void onPickImage(f);
                  e.target.value = "";
                }}
              />
            </>
          )}
          {caps.audio && (
            <>
              <InputGroupButton
                size="icon-xs"
                variant={recorder.recording ? "destructive" : "ghost"}
                aria-label={recorder.recording ? "Stop recording" : "Record from microphone"}
                title={recorder.recording ? "Stop recording" : "Record from microphone"}
                onClick={() => void toggleMic()}
              >
                {recorder.recording ? <Square /> : <Mic />}
              </InputGroupButton>
              <InputGroupButton
                size="icon-xs"
                aria-label="Attach audio file"
                title="Attach audio file"
                onClick={() => audioInputRef.current?.click()}
              >
                <AudioLines />
              </InputGroupButton>
              <input
                ref={audioInputRef}
                type="file"
                accept="audio/*"
                aria-label="Audio file"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) onPickAudio(f);
                  e.target.value = "";
                }}
              />
            </>
          )}
          {caps.files && (
            <>
              <InputGroupButton
                size="icon-xs"
                aria-label="Attach file"
                title="Attach file"
                onClick={() => fileInputRef.current?.click()}
              >
                <FileUp />
              </InputGroupButton>
              <input
                ref={fileInputRef}
                type="file"
                accept={caps.fileAccept}
                aria-label="Attachment file"
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) onPickFile(f);
                  e.target.value = "";
                }}
              />
            </>
          )}
          {busy && onStop ? (
            <InputGroupButton
              className="ml-auto"
              size="icon-sm"
              variant="secondary"
              aria-label="Stop generating"
              title="Stop generating"
              onClick={onStop}
            >
              <CircleStop />
            </InputGroupButton>
          ) : (
            <InputGroupButton
              className="ml-auto"
              size="icon-sm"
              variant="default"
              aria-label="Send"
              title="Send"
              disabled={!sendable}
              onClick={() => void submit()}
            >
              <Send />
            </InputGroupButton>
          )}
        </InputGroupAddon>
      </InputGroup>
      {cameraOpen && (
        <CameraModal
          onClose={() => setCameraOpen(false)}
          onCapture={(dataUrl) => addAttachment({ kind: "image", url: dataUrl, name: "camera" })}
        />
      )}
    </div>
  );
}
