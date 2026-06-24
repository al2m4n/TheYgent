// A tiny set of inline primitives (M15 mirrors the cockpit: Tailwind, no component library). Five
// small building blocks for the chrome around the canvas — buttons, inputs, badges.

import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from "react";

type Variant = "primary" | "default" | "ghost" | "danger";

const VARIANT: Record<Variant, string> = {
  primary: "bg-blue-600 hover:bg-blue-500 text-white border-blue-500",
  default: "bg-[#161b26] hover:bg-[#1d2433] text-slate-200 border-slate-700",
  ghost: "bg-transparent hover:bg-[#1d2433] text-slate-300 border-transparent",
  danger: "bg-transparent hover:bg-red-950 text-red-300 border-red-900",
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
      className={`w-full rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-blue-500 ${className}`}
      {...props}
    />
  );
}

export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={`w-full rounded-md border border-slate-700 bg-[#0e131c] px-2.5 py-1.5 text-sm text-slate-100 outline-none focus:border-blue-500 ${className}`}
      {...props}
    />
  );
}

export function Badge({ children, tone = "slate" }: { children: ReactNode; tone?: string }) {
  const tones: Record<string, string> = {
    slate: "bg-slate-800 text-slate-300",
    blue: "bg-blue-950 text-blue-300",
    green: "bg-emerald-950 text-emerald-300",
    amber: "bg-amber-950 text-amber-300",
    red: "bg-red-950 text-red-300",
  };
  return (
    <span
      className={`inline-flex items-center rounded px-1.5 py-0.5 text-[11px] font-medium ${tones[tone] ?? tones.slate}`}
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

// ── M16 Registries primitives (mirrors the cockpit's ui.tsx, in this app's palette) ──

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-lg border border-slate-800 bg-[#11161f] ${className}`}>{children}</div>
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
    <th className="border-b border-slate-800 bg-[#0e131c] px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-slate-500">
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
    <div className="rounded-md border border-red-900 bg-red-950 px-3 py-2 text-sm text-red-200">
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
