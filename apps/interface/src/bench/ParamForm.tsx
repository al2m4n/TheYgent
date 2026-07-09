// The schema-driven, capability-narrowed param form. Renders a ParamSpec[] generically — there is
// NO per-param JSX and NO `if modality === …` tree (data-driven by design). The specs handed in are
// already narrowed by `paramsForModality`, so a param the model can't support is simply absent. A
// bounded numeric param (min+max) renders as a slider + number input bound together.

import { Field, Input, Select } from "../components/ui";
import { Slider } from "../components/ui/slider";
import type { ParamSpec } from "./params";

interface Props {
  specs: ParamSpec[];
  values: Record<string, string>;
  onChange: (key: string, raw: string) => void;
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

export function ParamForm({ specs, values, onChange }: Props) {
  if (specs.length === 0) {
    return (
      <p className="text-xs text-muted-foreground">This model advertises no tunable params.</p>
    );
  }
  return (
    <div className="grid grid-cols-2 gap-3">
      {specs.map((spec) => {
        const value = values[spec.key] ?? "";
        const bounded = spec.type === "number" && spec.min !== undefined && spec.max !== undefined;
        return (
          <Field key={spec.key} label={spec.label}>
            {spec.type === "select" ? (
              <Select value={value} onChange={(e) => onChange(spec.key, e.target.value)}>
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
                onChange={(e) => onChange(spec.key, e.target.value)}
                data-param={spec.key}
              />
            )}
          </Field>
        );
      })}
    </div>
  );
}
