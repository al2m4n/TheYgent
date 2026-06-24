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
