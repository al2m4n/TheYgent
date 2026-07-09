// The schema-driven, capability-narrowed param form. Renders a ParamSpec[] generically — there is
// NO per-param JSX and NO `if modality === …` tree (data-driven by design). The specs handed in are
// already narrowed by `paramsForModality`, so a param the model can't support is simply absent. A
// bounded numeric param (min+max) renders as a slider + number input bound together. Each field's
// label carries a "?" tooltip with the spec's plain-language help.

import { HelpCircle } from "lucide-react";
import { Input, Select } from "../components/ui";
import { Slider } from "../components/ui/slider";
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "../components/ui/tooltip";
import type { ParamSpec } from "./params";

interface Props {
  specs: ParamSpec[];
  values: Record<string, string>;
  onChange: (key: string, raw: string) => void;
  /** One column for narrow hosts (the editor inspector); the bench cards keep two. */
  columns?: 1 | 2;
}

/** The field label + its "?" help bubble. A plain div (not <label>) so the tooltip trigger inside
 * doesn't forward clicks to the control; the controls carry their own aria-labels instead. */
export function ParamLabel({ label, help }: { label: string; help?: string }) {
  return (
    <span className="flex items-center gap-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
      {label}
      {help && (
        // A local provider (the shadcn sidebar does the same) keeps this usable in hosts/tests
        // that render outside the app-level TooltipProvider; nesting is harmless.
        <TooltipProvider delayDuration={150}>
          <Tooltip>
            <TooltipTrigger asChild>
              <button
                type="button"
                aria-label={`What does ${label} do?`}
                className="text-muted-foreground/70 transition-colors hover:text-foreground focus-visible:text-foreground"
              >
                <HelpCircle size={12} aria-hidden />
              </button>
            </TooltipTrigger>
            <TooltipContent side="top" className="max-w-64 whitespace-normal normal-case">
              {help}
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      )}
    </span>
  );
}

function SliderNumber({
  spec,
  value,
  onChange,
}: {
  spec: ParamSpec;
  value: string;
  onChange: (raw: string) => void;
}) {
  // The slider drives the same value as the number box; clearing the number unsets the param.
  const sliderValue = value === "" ? (spec.min ?? 0) : Number(value);
  return (
    <div className="flex items-center gap-2">
      <Slider
        min={spec.min}
        max={spec.max}
        step={spec.step}
        value={[sliderValue]}
        onValueChange={(vals) => onChange(String(vals[0] ?? ""))}
        className="flex-1"
        aria-label={spec.label}
      />
      {/* The slider names its thumb via aria-label; the paired number box carries the same
          string — assistive tech disambiguates the two by role. */}
      <Input
        type="number"
        min={spec.min}
        max={spec.max}
        step={spec.step}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        data-param={spec.key}
        className="w-20"
        aria-label={spec.label}
      />
    </div>
  );
}

export function ParamForm({ specs, values, onChange, columns = 2 }: Props) {
  if (specs.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">This model advertises no tunable params.</p>
    );
  }
  return (
    <div className={`grid gap-3 ${columns === 1 ? "grid-cols-1" : "grid-cols-2"}`}>
      {specs.map((spec) => {
        const value = values[spec.key] ?? "";
        const bounded = spec.type === "number" && spec.min !== undefined && spec.max !== undefined;
        return (
          <div key={spec.key} className="space-y-1">
            <ParamLabel label={spec.label} help={spec.help} />
            {spec.type === "select" ? (
              <Select
                value={value}
                aria-label={spec.label}
                onChange={(e) => onChange(spec.key, e.target.value)}
              >
                <option value="">—</option>
                {(spec.options ?? []).map((opt) => (
                  <option key={opt} value={opt}>
                    {opt}
                  </option>
                ))}
              </Select>
            ) : bounded ? (
              <SliderNumber spec={spec} value={value} onChange={(raw) => onChange(spec.key, raw)} />
            ) : (
              <Input
                type={spec.type === "number" ? "number" : "text"}
                min={spec.min}
                max={spec.max}
                step={spec.step}
                placeholder={spec.placeholder}
                value={value}
                aria-label={spec.label}
                onChange={(e) => onChange(spec.key, e.target.value)}
                data-param={spec.key}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}
