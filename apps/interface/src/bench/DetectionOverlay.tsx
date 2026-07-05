// Shared box/mask overlay — the ONE component that draws detections on an image,
// used by BOTH a grounding VLM's structured output (boxes-as-text parsed from a chat response) AND a
// classic-CV tool's result (YOLO/SAM via an external MCP server). "See boxes on the feed" works for
// both because they render through the same component.

export interface Detection {
  /** [x, y, w, h] in image pixel coordinates (or 0..1 normalized — set `normalized`). */
  box: [number, number, number, number];
  label?: string;
  score?: number;
}

interface Props {
  src: string;
  detections: Detection[];
  /** boxes are 0..1 fractions of width/height rather than pixels. */
  normalized?: boolean;
  width?: number;
}

export function DetectionOverlay({ src, detections, normalized, width = 480 }: Props) {
  return (
    <div className="relative inline-block" style={{ width }} data-testid="detection-overlay">
      <img src={src} alt="" className="block w-full rounded-md" />
      <svg
        className="pointer-events-none absolute inset-0 h-full w-full"
        viewBox={normalized ? "0 0 1 1" : undefined}
        preserveAspectRatio="none"
        role="img"
        aria-label={`${detections.length} detections`}
      >
        {detections.map((d) => {
          const [x, y, w, h] = d.box;
          const sw = normalized ? 0.004 : 2;
          return (
            <g key={`${d.label ?? ""}:${d.box.join(",")}`}>
              <rect
                x={x}
                y={y}
                width={w}
                height={h}
                fill="none"
                stroke="#3b82f6"
                strokeWidth={sw}
              />
              {(d.label || d.score !== undefined) && (
                <text
                  x={x}
                  y={Math.max(y - (normalized ? 0.01 : 4), 0)}
                  fill="#93c5fd"
                  fontSize={normalized ? 0.04 : 12}
                >
                  {d.label}
                  {d.score !== undefined ? ` ${(d.score * 100).toFixed(0)}%` : ""}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/**
 * Best-effort parse of bounding boxes from a model/tool JSON payload (grounding VLM output or a CV
 * tool result). Accepts a few common shapes: `{detections:[{box|bbox,label,score}]}` or a bare array.
 * Unknown shapes → []. Pure, so the "detection overlay from a fixture" guard tests it directly.
 */
export function parseDetections(payload: unknown): Detection[] {
  const list = Array.isArray(payload)
    ? payload
    : Array.isArray((payload as { detections?: unknown })?.detections)
      ? (payload as { detections: unknown[] }).detections
      : [];
  const out: Detection[] = [];
  for (const item of list as Record<string, unknown>[]) {
    const raw = (item.box ?? item.bbox) as unknown;
    if (Array.isArray(raw) && raw.length === 4 && raw.every((n) => typeof n === "number")) {
      out.push({
        box: raw as [number, number, number, number],
        label: typeof item.label === "string" ? item.label : undefined,
        score: typeof item.score === "number" ? item.score : undefined,
      });
    }
  }
  return out;
}

/**
 * Parse detections from a run/tool OUTPUT string. The run output is JSON-serialized on the wire
 * (control-plane `_coerce_output`), so both the tool tester and the chat panel's VLM grounding
 * overlay JSON-parse it first, then reuse `parseDetections`. Best-effort: a non-JSON string or
 * an unknown shape → [] (so a plain-text answer simply renders no boxes — it never throws).
 */
export function detectionsFromOutput(output: string | null | undefined): Detection[] {
  if (!output) return [];
  try {
    return parseDetections(JSON.parse(output));
  } catch {
    return [];
  }
}
