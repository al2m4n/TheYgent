// The chat composer: text plus whatever the target's modality allows — image attach (upload or
// camera) for vision, audio attach (upload or microphone) for transcription. Enter sends,
// Shift+Enter breaks the line. Attachments stage as removable chips above the input so what is
// about to be sent is always visible.

import { Camera, CircleStop, ImagePlus, Mic, Paperclip, Send, Square, X } from "lucide-react";
import { useRef, useState } from "react";
import { Button, Textarea } from "../components/ui";
import { notify } from "../lib/notify";
import { CameraModal } from "./CameraModal";
import { audioDurationSec, fileToDataUrl, useRecorder } from "./media";
import type { Attachment, ComposerCaps } from "./types";

function IconButton({
  label,
  onClick,
  disabled,
  active,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  active?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      disabled={disabled}
      onClick={onClick}
      className={`rounded-md border p-2 disabled:opacity-50 ${
        active
          ? "border-red-500/50 bg-red-500/10 text-red-600 dark:text-red-400"
          : "border-slate-700 bg-[var(--c-elev)] text-slate-400 hover:bg-[var(--c-hover)] hover:text-slate-200"
      }`}
    >
      {children}
    </button>
  );
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
      <p className="rounded-md border border-dashed border-slate-700 px-3 py-2 text-xs text-slate-500">
        {disabledNote ?? "This conversation can't be continued."}
      </p>
    );
  }

  return (
    <div className="space-y-2">
      {attachments.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {attachments.map((a, i) => (
            <span
              key={`${a.url}-${i}`}
              className="flex items-center gap-2 rounded-md border border-slate-700 bg-[var(--c-elev)] px-2 py-1 text-xs text-slate-300"
            >
              {a.kind === "image" ? (
                <img
                  src={a.url}
                  alt={a.name ?? "attachment"}
                  className="h-8 w-8 rounded object-cover"
                />
              ) : (
                // biome-ignore lint/a11y/useMediaCaption: staged voice clip, no caption source
                <audio controls src={a.url} className="h-8 w-44" />
              )}
              <span className="max-w-32 truncate">
                {a.name}
                {a.kind === "audio" && a.durationSec ? ` · ${a.durationSec.toFixed(1)}s` : ""}
              </span>
              <button
                type="button"
                aria-label="Remove attachment"
                className="text-slate-500 hover:text-slate-200"
                onClick={() => {
                  // A discarded clip's object URL would pin the blob for the page lifetime.
                  if (a.kind === "audio" && a.url.startsWith("blob:")) URL.revokeObjectURL(a.url);
                  setAttachments((cur) => cur.filter((_, j) => j !== i));
                }}
              >
                <X size={13} />
              </button>
            </span>
          ))}
        </div>
      )}
      {recorder.error && <p className="text-xs text-red-500">{recorder.error}</p>}
      <div className="flex items-end gap-2">
        {caps.images && (
          <>
            <IconButton label="Attach image" onClick={() => imageInputRef.current?.click()}>
              <ImagePlus size={16} />
            </IconButton>
            <IconButton label="Capture from camera" onClick={() => setCameraOpen(true)}>
              <Camera size={16} />
            </IconButton>
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
            <IconButton
              label={recorder.recording ? "Stop recording" : "Record from microphone"}
              onClick={() => void toggleMic()}
              active={recorder.recording}
            >
              {recorder.recording ? <Square size={16} /> : <Mic size={16} />}
            </IconButton>
            <IconButton label="Attach audio file" onClick={() => audioInputRef.current?.click()}>
              <Paperclip size={16} />
            </IconButton>
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
        {!caps.textDisabled ? (
          <Textarea
            rows={2}
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder={caps.placeholder ?? "Send a message…"}
            className="flex-1"
            onKeyDown={(e) => {
              // isComposing: an IME Enter commits the composition candidate, it doesn't send.
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void submit();
              }
            }}
          />
        ) : (
          <p className="flex-1 self-center text-xs text-slate-500">
            {recorder.recording
              ? "Recording… stop to stage the clip."
              : (caps.placeholder ?? "Record or attach audio, then send.")}
          </p>
        )}
        {busy && onStop ? (
          <Button onClick={onStop} title="Stop generating">
            <CircleStop size={16} />
          </Button>
        ) : (
          <Button variant="primary" disabled={!sendable} onClick={() => void submit()} title="Send">
            <Send size={16} />
          </Button>
        )}
      </div>
      {cameraOpen && (
        <CameraModal
          onClose={() => setCameraOpen(false)}
          onCapture={(dataUrl) => addAttachment({ kind: "image", url: dataUrl, name: "camera" })}
        />
      )}
    </div>
  );
}
