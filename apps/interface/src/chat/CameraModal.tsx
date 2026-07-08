// Camera capture for vision chat: live preview → one frame snapshotted to a JPEG data URI.
// The frame goes straight to the inference data plane as an image content block; nothing is
// uploaded anywhere else. The stream is released the moment the dialog closes.

import { useEffect, useRef, useState } from "react";
import { Button, ErrorBanner, Modal } from "../components/ui";

export function CameraModal({
  onCapture,
  onClose,
}: {
  onCapture: (dataUrl: string) => void;
  onClose: () => void;
}) {
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          video: { facingMode: "environment" },
        });
        if (cancelled) {
          for (const t of stream.getTracks()) t.stop();
          return;
        }
        streamRef.current = stream;
        if (videoRef.current) {
          videoRef.current.srcObject = stream;
          await videoRef.current.play();
        }
        setReady(true);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
      for (const t of streamRef.current?.getTracks() ?? []) t.stop();
      streamRef.current = null;
    };
  }, []);

  function capture() {
    const video = videoRef.current;
    if (!video || video.videoWidth === 0) return;
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")?.drawImage(video, 0, 0);
    onCapture(canvas.toDataURL("image/jpeg", 0.9));
    onClose();
  }

  return (
    <Modal title="Capture from camera" width="max-w-xl" onClose={onClose}>
      <div className="space-y-3">
        <ErrorBanner error={error} />
        {/* muted live preview only — the a11y name lives on the modal title */}
        <video ref={videoRef} playsInline muted className="w-full rounded-md bg-black">
          <track kind="captions" />
        </video>
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={capture} disabled={!ready}>
            Capture
          </Button>
        </div>
      </div>
    </Modal>
  );
}
