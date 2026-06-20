// A small set of inline components (M8 §3.5: Tailwind, no component library). Just enough
// for five views of tables and forms — the design-system question is deferred to the real
// builder milestone.

import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from "react";
import { statusClass } from "../lib/format";

type Variant = "primary" | "secondary" | "danger";

const VARIANT: Record<Variant, string> = {
  primary: "bg-indigo-600 hover:bg-indigo-500 text-white border-indigo-500",
  secondary: "bg-slate-800 hover:bg-slate-700 text-slate-100 border-slate-600",
  danger: "bg-rose-700 hover:bg-rose-600 text-white border-rose-600",
};

export function Button({
  variant = "secondary",
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type="button"
      className={`rounded-md border px-3 py-1.5 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${VARIANT[variant]} ${className}`}
      {...props}
    />
  );
}

export function Input({ className = "", ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input
      className={`w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-indigo-500 ${className}`}
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
      className={`w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-indigo-500 ${className}`}
      {...props}
    />
  );
}

export function Select({ className = "", ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select
      className={`w-full rounded-md border border-slate-700 bg-slate-900 px-3 py-1.5 text-sm text-slate-100 outline-none focus:border-indigo-500 ${className}`}
      {...props}
    />
  );
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium uppercase tracking-wide text-slate-400">{label}</span>
      {children}
    </label>
  );
}

export function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-block rounded-full border px-2 py-0.5 text-xs font-medium ${statusClass(status)}`}
    >
      {status}
    </span>
  );
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: string }) {
  const tones: Record<string, string> = {
    neutral: "bg-slate-700/40 text-slate-300 border-slate-600",
    green: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
    red: "bg-rose-500/15 text-rose-300 border-rose-500/30",
  };
  return (
    <span
      className={`inline-block rounded border px-1.5 py-0.5 text-xs ${tones[tone] ?? tones.neutral}`}
    >
      {children}
    </span>
  );
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-lg border border-slate-800 bg-slate-900/40 ${className}`}>
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
    <th className="border-b border-slate-800 bg-slate-900/60 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-slate-400">
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
    <div className="rounded-md border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
      {msg}
    </div>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-3 py-8 text-center text-sm text-slate-500">{children}</div>;
}

export function Spinner({ label = "Loading…" }: { label?: string }) {
  return <div className="px-3 py-8 text-center text-sm text-slate-500">{label}</div>;
}
