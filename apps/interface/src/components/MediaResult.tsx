// A finished run's answer, rendered for what it actually is. Shared by the per-agent Run modal and
// the canvas Test panel — the two single-shot surfaces (a chat turn goes through MessageBubble
// instead). Without this, a voice or image agent shows its answer as the raw `{"ref":"art_…"}`
// JSON the walker returns.
//
// The shape is decided from the persisted output, never from the declared modality: a graph ends on
// whichever boundary fired, so a voice agent whose transcription failed hands back prose on its
// error branch and must render as prose.

import { useEffect, useRef, useState } from "react";
import { Markdown } from "../chat/Markdown";
import { api } from "../lib/api";
import { type Boundary, attachmentKindFor, classifyRunOutput } from "../lib/modality";

export function MediaResult({
  output,
  boundary,
  streaming,
}: {
  output: string;
  boundary: Boundary;
  /** A live stream still appends to `output`; the caret marks it. */
  streaming?: boolean;
}) {
  const resolved = classifyRunOutput(output, boundary);
  if (resolved.kind === "artifact") return <Artifact refId={resolved.ref} boundary={boundary} />;
  if (resolved.kind === "json") {
    return (
      <pre className="mono max-h-64 overflow-auto whitespace-pre-wrap text-xs text-slate-200">
        {pretty(resolved.text)}
      </pre>
    );
  }
  return (
    <>
      <Markdown text={resolved.kind === "empty" ? "" : resolved.text} />
      {streaming && <span className="animate-pulse text-muted-foreground">▍</span>}
    </>
  );
}

function pretty(text: string): string {
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

/** Fetch the artifact's bytes and play/show them. The GET carries the auth header, which an
 *  `<audio src>` cannot — hence the object URL. */
function Artifact({ refId, boundary }: { refId: string; boundary: Boundary }) {
  const [state, setState] = useState<{ url: string; kind: "image" | "audio" } | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Keyed on the REF alone. `boundary` is rebuilt by `boundaryOf` on every render of the hosting
  // panel (a fresh object with a fresh Set), so depending on it would re-run this effect on every
  // parent re-render — a resize drag, a waterfall hover, a durable poll — and each cleanup would
  // revoke the object URL still bound to the live player while re-issuing the GET. It is only a
  // fallback for a blob with no usable media type, so a ref carries it in.
  const boundaryRef = useRef(boundary);
  boundaryRef.current = boundary;

  useEffect(() => {
    let url: string | null = null;
    let cancelled = false;
    api
      .downloadArtifact(refId)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setState({ url, kind: attachmentKindFor(blob.type, boundaryRef.current) });
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
      // The object URL pins the blob for the page lifetime otherwise.
      if (url) URL.revokeObjectURL(url);
    };
  }, [refId]);

  if (error) return <p className="text-xs text-amber-400">Could not load the answer: {error}</p>;
  if (!state) return <p className="text-sm text-slate-400">Loading the answer…</p>;
  if (state.kind === "image") {
    return (
      <img
        src={state.url}
        alt="the run's answer"
        className="max-h-64 max-w-full rounded-md border border-slate-700 object-contain"
      />
    );
  }
  // biome-ignore lint/a11y/useMediaCaption: synthesized speech, no caption source
  return <audio controls src={state.url} className="h-9 w-full max-w-sm" />;
}
