// A thin vertical drag bar that resizes the panel beside it. Pure UI chrome — no IR, no React Flow.
// Uses pointer capture so the drag keeps tracking even when the cursor leaves the 4px strip (no
// global listeners to leak). Double-click resets to the panel's default width.

import { useRef } from "react";

interface Props {
  /** Current width of the panel this handle resizes. */
  width: number;
  /** Commit a new (clamped) width. */
  onResize: (width: number) => void;
  /** Which side the panel sits on relative to the handle — decides the drag direction. */
  side: "left" | "right";
  min: number;
  max: number;
  /** Width restored on double-click. */
  defaultWidth: number;
  label: string;
}

export function ResizeHandle({ width, onResize, side, min, max, defaultWidth, label }: Props) {
  const drag = useRef<{ startX: number; startW: number } | null>(null);

  const clamp = (w: number) => Math.min(max, Math.max(min, w));

  const onPointerDown = (e: React.PointerEvent) => {
    e.preventDefault();
    drag.current = { startX: e.clientX, startW: width };
    e.currentTarget.setPointerCapture(e.pointerId);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drag.current) return;
    const dx = e.clientX - drag.current.startX;
    onResize(clamp(drag.current.startW + (side === "left" ? dx : -dx)));
  };
  const end = (e: React.PointerEvent) => {
    if (!drag.current) return;
    drag.current = null;
    e.currentTarget.releasePointerCapture(e.pointerId);
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  };

  // Keyboard resize for accessibility — arrows nudge the boundary 16px at a time; Home/End jump to
  // the min/max width (the full window-splitter keyboard pattern).
  const onKeyDown = (e: React.KeyboardEvent) => {
    const step = side === "left" ? 16 : -16;
    if (e.key === "ArrowLeft") onResize(clamp(width - step));
    else if (e.key === "ArrowRight") onResize(clamp(width + step));
    else if (e.key === "Home") onResize(min);
    else if (e.key === "End") onResize(max);
  };

  return (
    // The WAI-ARIA "window splitter" pattern: a focusable, draggable `separator` (an <hr> can't be
    // the interactive handle biome's useSemanticElements would suggest).
    // biome-ignore lint/a11y/useSemanticElements: a draggable splitter must be a div, not <hr>
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${label}`}
      aria-valuenow={width}
      aria-valuemin={min}
      aria-valuemax={max}
      tabIndex={0}
      title="Drag to resize · double-click to reset"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={end}
      onPointerCancel={end}
      onDoubleClick={() => onResize(defaultWidth)}
      onKeyDown={onKeyDown}
      className="group relative w-1 shrink-0 cursor-col-resize bg-slate-800 transition-colors hover:bg-blue-500/60 focus:bg-blue-500/60 focus:outline-none"
    >
      {/* widen the hit area beyond the visible 4px line without affecting layout */}
      <span className="absolute inset-y-0 -left-1 -right-1" />
    </div>
  );
}
