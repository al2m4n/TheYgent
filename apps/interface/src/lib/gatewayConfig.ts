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

/** The policy's defaults: what a control plane fills in for a field it is not sent. */
const DEFAULT_POLICY: GatewayConfigDraft["policy"] = {
  subagent_target: null,
  main_target: null,
  local_target: null,
  warm_switch_margin: 1.5,
  shadow_local: true,
  shadow_cloud: false,
  shadow_cloud_daily_usd: 1,
  local_models: [],
  local_routing: {
    side_requests: { share: 1, trial: 0, model: null, agent_types: [] },
    subagents: { share: 0, trial: 0, model: null, agent_types: [] },
    main: { share: 0, trial: 0, model: null, agent_types: [] },
  },
};

/** The draft an edit starts from: the latest config's settings, or — before any is saved — an
 *  observer config with no routing targets. A field a control plane predating it did not send
 *  takes its default, so an older version still opens. */
export function draftOf(config: GatewayConfig | null): GatewayConfigDraft {
  if (config === null) {
    return {
      mode: "observer",
      policy: structuredClone(DEFAULT_POLICY),
      fingerprints: [],
      pricing_overrides: {},
    };
  }
  const routing = config.policy.local_routing;
  return {
    mode: config.mode,
    policy: {
      ...structuredClone(DEFAULT_POLICY),
      ...config.policy,
      local_models: (config.policy.local_models ?? []).map((m) => ({
        ...LOCAL_MODEL_DEFAULTS,
        ...m,
        rates: m.rates ? { ...m.rates } : null,
      })),
      local_routing: {
        side_requests: { ...DEFAULT_POLICY.local_routing.side_requests, ...routing?.side_requests },
        subagents: { ...DEFAULT_POLICY.local_routing.subagents, ...routing?.subagents },
        main: { ...DEFAULT_POLICY.local_routing.main, ...routing?.main },
      },
    },
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

/** What a listed local model's fields default to, as a control plane fills them in. */
const LOCAL_MODEL_DEFAULTS = { max_output_tokens: 16_384, rates: null };

/** One listed local model as the form holds it. */
export interface LocalModelRow {
  model: string;
  /** Context size in tokens, as text. */
  context: string;
  /** The most it writes in an agent turn, in tokens, as text. */
  maxOutput: string;
  tools: boolean;
  images: boolean;
  /** What running it costs, USD per million input and output tokens, as text ("" for none). */
  inputRate: string;
  outputRate: string;
}

/** A new row: nothing typed yet, the defaults a control plane would fill in. */
export function blankLocalModel(): LocalModelRow {
  return {
    model: "",
    context: "32768",
    maxOutput: "16384",
    tools: false,
    images: false,
    inputRate: "",
    outputRate: "",
  };
}

export type DialKind = "side_requests" | "subagents" | "main";
export const DIAL_KINDS: DialKind[] = ["side_requests", "subagents", "main"];

/** One local dial as the form holds it. */
export interface DialRow {
  /** Percent of eligible conversations, as text. */
  share: string;
  /** Percent tried locally after Anthropic answers, as text. */
  trial: string;
  /** A listed model, or "" for the smallest that fits. */
  model: string;
  /** Subagent types, comma-separated ("" for every type). */
  agentTypes: string;
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
  localModels: LocalModelRow[];
  dials: Record<DialKind, DialRow>;
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
    localModels: draft.policy.local_models.map((m) => ({
      model: m.model,
      context: String(m.context_tokens),
      maxOutput: String(m.max_output_tokens),
      tools: m.tools,
      images: m.images,
      inputRate: m.rates ? String(m.rates.input) : "",
      outputRate: m.rates ? String(m.rates.output) : "",
    })),
    dials: Object.fromEntries(
      DIAL_KINDS.map((kind) => {
        const dial = draft.policy.local_routing[kind];
        return [
          kind,
          {
            share: String(Math.round(dial.share * 1000) / 10),
            trial: String(Math.round(dial.trial * 1000) / 10),
            model: dial.model ?? "",
            agentTypes: dial.agent_types.join(", "),
          },
        ];
      }),
    ) as Record<DialKind, DialRow>,
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
  const localModels: GatewayConfigDraft["policy"]["local_models"] = [];
  form.localModels.forEach((row, i) => {
    const model = row.model.trim();
    if (!isToken(model)) errors[`local.${i}.model`] = "A logical model id, no spaces.";
    else if (ENGINES.has(model))
      errors[`local.${i}.model`] = "An engine name; use a logical model id.";
    else if (localModels.some((m) => m.model === model))
      errors[`local.${i}.model`] = "Listed twice.";
    const context = Number(row.context.trim());
    if (!Number.isInteger(context) || context < 1024) {
      errors[`local.${i}.context`] = "Tokens, 1024 or more.";
    }
    const maxOutput = Number(row.maxOutput.trim());
    if (!Number.isInteger(maxOutput) || maxOutput < 1024) {
      errors[`local.${i}.maxOutput`] = "Tokens, 1024 or more.";
    }
    // Both rates, or neither: a model without them is not priced.
    let rates: { input: number; output: number } | null = null;
    if (row.inputRate.trim() !== "" || row.outputRate.trim() !== "") {
      const input = rate(row.inputRate);
      const output = rate(row.outputRate);
      if (input === null) errors[`local.${i}.inputRate`] = "USD per million tokens, 0 or more.";
      if (output === null) errors[`local.${i}.outputRate`] = "USD per million tokens, 0 or more.";
      if (input !== null && output !== null) rates = { input, output };
    }
    localModels.push({
      model,
      context_tokens: context,
      max_output_tokens: maxOutput,
      tools: row.tools,
      images: row.images,
      rates,
    });
  });
  const listed = new Set(localModels.map((m) => m.model));
  const dials = {} as GatewayConfigDraft["policy"]["local_routing"];
  for (const kind of DIAL_KINDS) {
    const row = form.dials[kind];
    const percent = Number(row.share.trim());
    if (row.share.trim() === "" || !Number.isFinite(percent) || percent < 0 || percent > 100) {
      errors[`dial.${kind}.share`] = "A percentage from 0 to 100.";
    }
    const trial = Number(row.trial.trim());
    if (row.trial.trim() === "" || !Number.isFinite(trial) || trial < 0 || trial > 100) {
      errors[`dial.${kind}.trial`] = "A percentage from 0 to 100.";
    } else if (percent + trial > 100 + 1e-7) {
      errors[`dial.${kind}.trial`] = "The share and the trial together exceed 100%.";
    }
    const model = row.model.trim() || null;
    const known =
      model === null || listed.has(model) || (kind === "side_requests" && model === local);
    if (!known) errors[`dial.${kind}.model`] = "Not a listed local model.";
    const agentTypes = row.agentTypes
      .split(",")
      .map((t) => t.trim())
      .filter((t) => t !== "");
    if (agentTypes.some((t) => !isToken(t))) {
      errors[`dial.${kind}.agentTypes`] = "Agent type names, separated by commas.";
    }
    // Agent turns carry Claude Code's tools: a share, or a trial, needs a model that follows them.
    const candidates = localModels.filter((m) => model === null || m.model === model);
    if (
      kind !== "side_requests" &&
      (percent > 0 || trial > 0) &&
      !candidates.some((m) => m.tools)
    ) {
      errors[`dial.${kind}.${percent > 0 ? "share" : "trial"}`] ??=
        "Agent turns need a listed local model that follows tool calls.";
    }
    dials[kind] = {
      share: percent / 100,
      trial: trial / 100,
      model,
      agent_types: kind === "subagents" ? agentTypes : [],
    };
  }
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
        local_models: localModels,
        local_routing: dials,
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
