// Browser media capture for the chat composer: microphone recording (MediaRecorder), camera
// snapshots, and file → data-URI conversion. All of it stays in the user's trust domain — a
// recorded clip or captured frame goes straight to the inference data plane, never anywhere else.

import { useCallback, useEffect, useRef, useState } from "react";

export function fileToDataUrl(file: File | Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error ?? new Error("could not read file"));
    reader.readAsDataURL(file);
  });
}

/**
 * Best-effort duration of an audio blob, for the real-time-factor metric. Chrome reports
 * Infinity for MediaRecorder webm until forced to build a seek index (the currentTime jump);
 * anything still unreadable resolves 0 — a missing metric, never a broken transcription.
 */
export function audioDurationSec(blob: Blob): Promise<number> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(blob);
    const audio = new Audio();
    const done = (sec: number) => {
      URL.revokeObjectURL(url);
      resolve(Number.isFinite(sec) && sec > 0 ? sec : 0);
    };
    audio.onloadedmetadata = () => {
      if (Number.isFinite(audio.duration)) return done(audio.duration);
      audio.currentTime = Number.MAX_SAFE_INTEGER;
      audio.ontimeupdate = () => done(audio.duration);
    };
    audio.onerror = () => done(0);
    audio.src = url;
  });
}

export interface RecordingResult {
  blob: Blob;
  durationSec: number;
  mimeType: string;
}

function pickAudioMime(): string {
  if (typeof MediaRecorder === "undefined") return "";
  for (const t of ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"]) {
    if (MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

/** Microphone recording as a hook: start → talk → stop yields the clip + its wall-clock length. */
export function useRecorder() {
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const chunksRef = useRef<BlobPart[]>([]);
  const startedAtRef = useRef(0);

  const releaseStream = useCallback(() => {
    for (const track of streamRef.current?.getTracks() ?? []) track.stop();
    streamRef.current = null;
  }, []);

  // Leaving the page mid-recording must release the microphone (the tab indicator stays on
  // otherwise).
  useEffect(() => releaseStream, [releaseStream]);

  const start = useCallback(async () => {
    setError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      const mimeType = pickAudioMime();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.start();
      recorderRef.current = recorder;
      startedAtRef.current = performance.now();
      setRecording(true);
    } catch (e) {
      releaseStream();
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [releaseStream]);

  const stop = useCallback((): Promise<RecordingResult | null> => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === "inactive") return Promise.resolve(null);
    return new Promise((resolve) => {
      recorder.onstop = () => {
        const mimeType = recorder.mimeType || "audio/webm";
        const blob = new Blob(chunksRef.current, { type: mimeType });
        const durationSec = (performance.now() - startedAtRef.current) / 1000;
        releaseStream();
        recorderRef.current = null;
        setRecording(false);
        resolve(blob.size > 0 ? { blob, durationSec, mimeType } : null);
      };
      recorder.stop();
    });
  }, [releaseStream]);

  const cancel = useCallback(() => {
    const recorder = recorderRef.current;
    if (recorder && recorder.state !== "inactive") {
      recorder.onstop = null;
      recorder.stop();
    }
    recorderRef.current = null;
    releaseStream();
    setRecording(false);
  }, [releaseStream]);

  return { recording, error, start, stop, cancel };
}
