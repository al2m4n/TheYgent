// The editor-side model-parameters panel: edit the LITERAL generation params an agent carries —
// a model binding's `params` (llm) or an audio node's `config.params` (transcribe / speak) —
// through the same schema-driven, capability-narrowed specs the bench uses, so the two surfaces
// stay one vocabulary. Saved bench presets load here as a literal COPY of their values: the
// preset name never lands in the IR (a live reference would let a deployed agent drift under an
// edited preset, breaking contentHash immutability — the same rule as apply-preset on the bench).

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { ParamForm } from "../bench/ParamForm";
import {
  type PanelModality,
  type ParamSpec,
  coerceParam,
  narrowSpecs,
  presetParamsForNode,
  rawFromParam,
  specsForNodeParams,
} from "../bench/params";
import { api } from "../lib/api";
import { Button, Select } from "./ui";

// Older graphs (and hand-authored IR) may carry these params in their camelCase spelling — the
// runtime lowers them onto the wire either way. The form reads whichever spelling is present and
// writes the wire (snake_case) key, deleting the twin so one param never exists in both spellings.
const CAMEL_TWIN: Record<string, string> = { max_tokens: "maxTokens", top_p: "topP" };

function seedValues(specs: ParamSpec[], params: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const spec of specs) {
    const twin = CAMEL_TWIN[spec.key];
    const value = params[spec.key] ?? (twin ? params[twin] : undefined);
    out[spec.key] = rawFromParam(spec, value);
  }
  return out;
}

/**
 * Edit one params dict in place. `logicalId` (the model the binding forwards to the inference
 * seam) drives capability narrowing and the preset filter; when the inference plane can't answer,
 * every spec is shown so an offline author can still set the knobs.
 */
export function ModelParamsSection({
  modality,
  logicalId,
  params,
  onChange,
  note,
}: {
  modality: PanelModality;
  logicalId?: string;
  params: Record<string, unknown>;
  onChange: (next: Record<string, unknown>) => void;
  note?: string;
}) {
  const allSpecs = useMemo(() => specsForNodeParams(modality), [modality]);
  const caps = useQuery({
    queryKey: ["capabilities", logicalId],
    queryFn: () => api.getModelCapabilities(logicalId as string),
    enabled: Boolean(logicalId),
    retry: false,
    staleTime: 30_000,
  });
  const specs = caps.data ? narrowSpecs(allSpecs, caps.data) : allSpecs;

  // Raw form strings, seeded from the stored params; re-seed only when the dict changes from
  // OUTSIDE (node switch, Code-view edit, preset load) so typing is never clobbered by our own
  // round-trip — the same last-emitted discipline as the messages editor.
  const [values, setValues] = useState<Record<string, string>>(() =>
    seedValues(allSpecs, params ?? {}),
  );
  const lastEmitted = useRef(JSON.stringify(params ?? {}));
  useEffect(() => {
    const incoming = JSON.stringify(params ?? {});
    if (incoming !== lastEmitted.current) {
      lastEmitted.current = incoming;
      setValues(seedValues(allSpecs, params ?? {}));
    }
  }, [params, allSpecs]);

  const emit = (next: Record<string, unknown>) => {
    lastEmitted.current = JSON.stringify(next);
    onChange(next);
  };

  const setField = (key: string, raw: string) => {
    setValues((v) => ({ ...v, [key]: raw }));
    const spec = allSpecs.find((s) => s.key === key);
    if (!spec) return;
    const next = { ...(params ?? {}) };
    delete next[key];
    const twin = CAMEL_TWIN[key];
    if (twin) delete next[twin];
    const coerced = coerceParam(spec, raw);
    if (coerced !== undefined) next[key] = coerced;
    emit(next);
  };

  return (
    <div className="space-y-2">
      <span className="block text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        Model parameters
      </span>
      <PresetLoader
        modality={modality}
        onLoad={(presetParams) => {
          const next = { ...(params ?? {}), ...presetParamsForNode(modality, presetParams) };
          setValues(seedValues(allSpecs, next));
          emit(next);
        }}
      />
      <ParamForm specs={specs} values={values} onChange={setField} columns={1} />
      <p className="text-[10px] leading-relaxed text-muted-foreground/80">
        {note ??
          "Saved into the agent as literal values. Empty fields send nothing — the engine's defaults apply."}
      </p>
    </div>
  );
}

/** Load a saved bench preset (modality-matched) — copies its literal values over the current
 * params; later edits to the preset do NOT follow the agent. Hidden when none exist. */
function PresetLoader({
  modality,
  onLoad,
}: {
  modality: PanelModality;
  onLoad: (params: Record<string, unknown>) => void;
}) {
  const [presetId, setPresetId] = useState("");
  const presets = useQuery({
    queryKey: ["presets", modality],
    queryFn: () => api.listPresets({ modality }),
    retry: false,
    staleTime: 15_000,
  });
  if (!presets.data || presets.data.length === 0) return null;
  return (
    <div className="flex items-center gap-1.5">
      <Select
        value={presetId}
        aria-label="Preset"
        className="!text-xs flex-1"
        onChange={(e) => setPresetId(e.target.value)}
      >
        <option value="">Load a preset…</option>
        {presets.data.map((p) => (
          <option key={p.id} value={p.id}>
            {p.name}
            {p.logical_id ? ` · ${p.logical_id}` : ""}
          </option>
        ))}
      </Select>
      <Button
        variant="ghost"
        className="h-7 text-xs"
        disabled={!presetId}
        onClick={() => {
          const preset = presets.data?.find((p) => p.id === presetId);
          if (preset) onLoad(preset.params);
        }}
      >
        Load
      </Button>
    </div>
  );
}
