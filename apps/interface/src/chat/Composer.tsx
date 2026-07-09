// The chat composer: text plus whatever the target's modality allows — image attach (upload or
// camera) for vision, audio attach (upload or microphone) for transcription. Enter sends,
// Shift+Enter breaks the line. Attachments stage as removable chips above the input so what is
// about to be sent is always visible. The input is an input-group: a growing textarea with the
// media buttons and the send control on a rail beneath it.

import { AudioLines, Camera, CircleStop, ImagePlus, Mic, Send, Square, X } from "lucide-react";
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
import { notify } from "../lib/notify";
import { CameraModal } from "./CameraModal";
import { audioDurationSec, fileToDataUrl, useRecorder } from "./media";
import type { Attachment, ComposerCaps } from "./types";

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
  const recorder = useRecorder();

  const hasAudio = attachments.some((a) => a.kind === "audio");
  const sendable =
    !busy &&
    !disabled &&
    (text.trim().length > 0 || attachments.length > 0) &&
    (!caps.audioRequired || hasAudio);

  function addAttachment(a: Attachment) {
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

  async function onPickAudio(file: File) {
    addAttachment({
      kind: "audio",
      url: URL.createObjectURL(file),
      blob: file,
      name: file.name,
      durationSec: await audioDurationSec(file),
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
                ) : null}
              </AttachmentContent>
              <AttachmentActions>
                <AttachmentAction
                  aria-label="Remove attachment"
                  onClick={() => {
                    // A discarded clip's object URL would pin the blob for the page lifetime.
                    if (a.kind === "audio" && a.url.startsWith("blob:")) URL.revokeObjectURL(a.url);
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
      <InputGroup>
        {!caps.textDisabled ? (
          <InputGroupTextarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={caps.placeholder ?? "Send a message…"}
            className="max-h-40"
            onKeyDown={(e) => {
              // isComposing: an IME Enter commits the composition candidate, it doesn't send.
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
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
                className="hidden"
                onChange={(e) => {
                  const f = e.target.files?.[0];
                  if (f) void onPickAudio(f);
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
