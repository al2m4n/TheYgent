// A tiny set of inline primitives (M15 mirrors the cockpit: Tailwind, no component library). Five
// small building blocks for the chrome around the canvas — buttons, inputs, badges.

import {
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
  useEffect,
} from "react";
import { toneOf } from "../lib/categories";
import { statusClass } from "../lib/format";

type Variant = "primary" | "default" | "ghost" | "danger";

const VARIANT: Record<Variant, string> = {
  primary: "bg-blue-600 hover:bg-blue-500 text-white border-blue-500",
  default: "bg-[var(--c-elev)] hover:bg-[var(--c-hover)] text-slate-200 border-slate-700",
  ghost: "bg-transparent hover:bg-[var(--c-hover)] text-slate-300 border-transparent",
  danger:
    "bg-transparent text-red-600 border-red-200 hover:bg-red-50 dark:text-red-300 dark:border-red-900 dark:hover:bg-red-950",
};

export function Button({
  variant = "default",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      className={`inline-flex items-center gap-1.5 rounded-md border px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT[variant]} ${className}`}
      {...props}
    />
  );
}

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={`w-full rounded-md border border-slate-700 bg-[var(--c-surface)] px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-blue-500 ${className}`}
      {...props}
    />
  );
}

export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={`w-full rounded-md border border-slate-700 bg-[var(--c-surface)] px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-blue-500 ${className}`}
      {...props}
    />
  );
}

export function Textarea({
  className = "",
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      className={`w-full rounded-md border border-slate-700 bg-[var(--c-surface)] px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-blue-500 ${className}`}
      {...props}
    />
  );
}

// The run status pill (Runs list + run detail) — completed green, failed red, streaming amber.
export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${statusClass(status)}`}
    >
      {status}
    </span>
  );
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: string }) {
  // The tone palette is centralized in lib/categories so a Badge, a filter chip, and a table cell
  // all paint the same category the same colour. Unknown tones fall back to slate.
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ${toneOf(tone).badge}`}
    >
      {children}
    </span>
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-slate-500">
        {label}
      </span>
      {children}
    </label>
  );
}

// The standard content-page container. Every routed page wraps in this so the page width and the
// responsive side/top padding stay IDENTICAL across the app — change the gutter once, here, and it
// moves everywhere. Pages are full-width (no centered max-width cap); pass spacing via `className`
// (e.g. "space-y-4"). The canvas Editor is the one exception — it's a full-bleed, full-height tool
// view, not a scrolling content page, so it does not use Page.
export function Page({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <div className={`w-full px-4 py-6 sm:px-6 lg:px-8 ${className}`}>{children}</div>;
}

// ── M16 Registries primitives (mirrors the cockpit's ui.tsx, in this app's palette) ──

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-lg border border-slate-800 bg-[var(--c-surface-2)] ${className}`}>
      {children}
    </div>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full border-collapse text-left text-sm">{children}</table>
    </div>
  );
}

export function Th({ children }: { children: ReactNode }) {
  return (
    <th className="border-b border-slate-800 bg-[var(--c-surface)] px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
      {children}
    </th>
  );
}

export function Td({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <td className={`border-b border-slate-800/60 px-3 py-2 ${className}`}>{children}</td>;
}

export function ErrorBanner({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return (
    <div className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700 dark:border-red-900 dark:bg-red-950 dark:text-red-200">
      {msg}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-800 px-6 py-10 text-center text-sm text-slate-500">
      {children}
    </div>
  );
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="px-3 py-8 text-center text-sm text-slate-500">{label}</div>;
}

// A centered modal dialog (the shared overlay the bench + browse flows open into). Backdrop click
// and Escape both close it. `width` is a Tailwind max-w-* class so callers size to content.
export function Modal({
  title,
  onClose,
  children,
  width = "max-w-3xl",
}: {
  title: ReactNode;
  onClose: () => void;
  children: ReactNode;
  width?: string;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      {/* The backdrop is a real button → keyboard-accessible close, no a11y lint. The panel is a
          sibling above it (relative > absolute), so panel clicks never reach the backdrop. */}
      <button
        type="button"
        aria-label="Close dialog"
        className="absolute inset-0 h-full w-full cursor-default bg-black/60"
        onClick={onClose}
      />
      {/* biome-ignore lint/a11y/useSemanticElements: an app-level modal panel, not a native <dialog> (no showModal) */}
      <div
        role="dialog"
        aria-modal="true"
        className={`relative flex max-h-[90vh] w-full ${width} flex-col overflow-hidden rounded-lg border border-slate-700 bg-[var(--c-surface)] shadow-xl`}
      >
        <header className="flex shrink-0 items-center justify-between border-b border-slate-800 px-4 py-3">
          <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded px-2 text-lg leading-none text-slate-500 hover:text-slate-200"
            aria-label="Close"
          >
            ✕
          </button>
        </header>
        <div className="min-h-0 flex-1 overflow-auto p-4">{children}</div>
      </div>
    </div>
  );
}

// A thin download/progress bar. `value`/`max` in bytes; `indeterminate` animates when total unknown.
export function ProgressBar({
  value,
  max,
  indeterminate = false,
}: {
  value: number;
  max?: number | null;
  indeterminate?: boolean;
}) {
  const pct = max && max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  return (
    <div className="h-2 w-full overflow-hidden rounded bg-slate-800">
      <div
        className={`h-full bg-blue-500 transition-[width] duration-300 ${
          indeterminate ? "animate-pulse" : ""
        }`}
        style={{ width: indeterminate ? "40%" : `${pct}%` }}
      />
    </div>
  );
}
