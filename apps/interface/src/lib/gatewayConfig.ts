// The AI Gateway config as an admin edits it on the Settings tab: the draft a config version
// starts from, and the checks the control plane will apply, run early so a form can say what is
// wrong before saving. The control plane validates again and is the authority.

import type { GatewayConfig, GatewayConfigDraft, GatewayMode, GatewayModelRates } from "./api";

/** What each mode does, in a few words, and its badge tone — exhaustive over the modes. */
export const MODE_SUMMARY: Record<GatewayMode, { hint: string; tone: string }> = {
  passthrough: { hint: "forwards without recording", tone: "slate" },
  observer: { hint: "records every exchange", tone: "blue" },
  router: { hint: "routes to cheaper models", tone: "green" },
};

/** The control plane's identifier rule for model ids and labels: no spaces, at most 128. */
const TOKEN = /^[A-Za-z0-9][A-Za-z0-9._:@+/[\]-]*$/;
/** Engine names: a local target is a logical model id, never one of these. */
const ENGINES = new Set(["mlx", "vllm", "llamacpp", "openai-compatible"]);

export const RATE_FIELDS: { key: keyof GatewayModelRates; label: string }[] = [
  { key: "input", label: "Input" },
  { key: "output", label: "Output" },
  { key: "cache_write_5m", label: "Cache write 5m" },
  { key: "cache_write_1h", label: "Cache write 1h" },
  { key: "cache_read", label: "Cache read" },
];

export function isToken(value: string): boolean {
  return value.length <= 128 && TOKEN.test(value);
}

/** The draft an edit starts from: the latest config's settings, or — before any is saved — an
 *  observer config with no routing targets. */
export function draftOf(config: GatewayConfig | null): GatewayConfigDraft {
  if (config === null) {
    return {
      mode: "observer",
      policy: {
        subagent_target: null,
        main_target: null,
        local_target: null,
        warm_switch_margin: 1.5,
        shadow_local: true,
        shadow_cloud: false,
        shadow_cloud_daily_usd: 1,
        local_models: [],
        local_routing: {
          side_requests: { share: 1, model: null, agent_types: [] },
          subagents: { share: 0, model: null, agent_types: [] },
          main: { share: 0, model: null, agent_types: [] },
        },
      },
      fingerprints: [],
      pricing_overrides: {},
    };
  }
  return {
    mode: config.mode,
    policy: { ...config.policy },
    fingerprints: config.fingerprints.map((rule) => ({ ...rule })),
    pricing_overrides: Object.fromEntries(
      Object.entries(config.pricing_overrides).map(([model, rates]) => [model, { ...rates }]),
    ),
  };
}

/** One pricing-override row as the form holds it: text, so a half-typed number survives. */
export interface RateRow {
  model: string;
  rates: Record<keyof GatewayModelRates, string>;
}

export function rowsOf(overrides: Record<string, GatewayModelRates>): RateRow[] {
  return Object.entries(overrides).map(([model, rates]) => ({
    model,
    rates: Object.fromEntries(RATE_FIELDS.map(({ key }) => [key, String(rates[key])])) as Record<
      keyof GatewayModelRates,
      string
    >,
  }));
}

export function blankRow(): RateRow {
  return {
    model: "",
    rates: { input: "", output: "", cache_write_5m: "", cache_write_1h: "", cache_read: "" },
  };
}

/** The form's fields, as text. */
export interface SettingsForm {
  mode: GatewayConfigDraft["mode"];
  subagentTarget: string;
  mainTarget: string;
  localTarget: string;
  margin: string;
  shadowLocal: boolean;
  shadowCloud: boolean;
  shadowCap: string;
  localModels: GatewayConfigDraft["policy"]["local_models"];
  localRouting: GatewayConfigDraft["policy"]["local_routing"];
  rates: RateRow[];
}

export function formOf(draft: GatewayConfigDraft): SettingsForm {
  return {
    mode: draft.mode,
    subagentTarget: draft.policy.subagent_target ?? "",
    mainTarget: draft.policy.main_target ?? "",
    localTarget: draft.policy.local_target ?? "",
    margin: String(draft.policy.warm_switch_margin),
    shadowLocal: draft.policy.shadow_local,
    shadowCloud: draft.policy.shadow_cloud,
    shadowCap: String(draft.policy.shadow_cloud_daily_usd),
    localModels: draft.policy.local_models.map((m) => ({ ...m })),
    localRouting: structuredClone(draft.policy.local_routing),
    rates: rowsOf(draft.pricing_overrides),
  };
}

/** What is wrong with a form, by field (`rates.<row>.<field>` for a pricing cell). */
export type FormErrors = Record<string, string>;

function rate(text: string): number | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;
  const value = Number(trimmed);
  return Number.isFinite(value) && value >= 0 ? value : null;
}

/** The draft a form describes, or the reasons it can't be saved. Fingerprint rules are the
 *  Catalog's and pass through untouched. */
export function draftFromForm(
  form: SettingsForm,
  fingerprints: GatewayConfigDraft["fingerprints"],
): { draft: GatewayConfigDraft; errors: null } | { draft: null; errors: FormErrors } {
  const errors: FormErrors = {};
  const target = (field: string, text: string): string | null => {
    const value = text.trim();
    if (value === "") return null;
    if (!isToken(value)) errors[field] = "A model id: letters, digits and . _ : - only, no spaces.";
    return value;
  };
  const subagent = target("subagentTarget", form.subagentTarget);
  const main = target("mainTarget", form.mainTarget);
  const local = target("localTarget", form.localTarget);
  if (local !== null && ENGINES.has(local) && !errors.localTarget) {
    errors.localTarget = `"${local}" is an engine; use the logical id of a model registered on the inference plane.`;
  }
  const margin = Number(form.margin.trim());
  if (form.margin.trim() === "" || !Number.isFinite(margin) || margin < 1) {
    errors.margin = "A number of at least 1.";
  }
  const cap = rate(form.shadowCap);
  if (cap === null) errors.shadowCap = "USD per day, 0 or more.";
  const overrides: Record<string, GatewayModelRates> = {};
  form.rates.forEach((row, i) => {
    const model = row.model.trim();
    if (!isToken(model)) {
      errors[`rates.${i}.model`] = "A model id, no spaces.";
    } else if (model in overrides) {
      errors[`rates.${i}.model`] = "This model already has a row.";
    }
    const rates = {} as GatewayModelRates;
    for (const { key } of RATE_FIELDS) {
      const value = rate(row.rates[key]);
      if (value === null) errors[`rates.${i}.${key}`] = "USD per million tokens, 0 or more.";
      else rates[key] = value;
    }
    overrides[model] = rates;
  });
  if (Object.keys(errors).length > 0) return { draft: null, errors };
  return {
    draft: {
      mode: form.mode,
      policy: {
        subagent_target: subagent,
        main_target: main,
        local_target: local,
        warm_switch_margin: margin,
        shadow_local: form.shadowLocal,
        shadow_cloud: form.shadowCloud,
        shadow_cloud_daily_usd: cap ?? 0,
        local_models: form.localModels,
        local_routing: form.localRouting,
      },
      fingerprints,
      pricing_overrides: overrides,
    },
    errors: null,
  };
}

/** Whether two drafts would save the same config (fingerprint rules aside). */
export function sameSettings(a: GatewayConfigDraft, b: GatewayConfigDraft): boolean {
  const settings = (d: GatewayConfigDraft) =>
    JSON.stringify([
      d.mode,
      d.policy.subagent_target,
      d.policy.main_target,
      d.policy.local_target,
      d.policy.warm_switch_margin,
      d.policy.shadow_local,
      d.policy.shadow_cloud,
      d.policy.shadow_cloud_daily_usd,
      d.policy.local_models,
      d.policy.local_routing,
      Object.keys(d.pricing_overrides)
        .sort()
        .map((model) => [model, RATE_FIELDS.map(({ key }) => d.pricing_overrides[model][key])]),
    ]);
  return settings(a) === settings(b);
}
