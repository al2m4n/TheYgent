// Apply a tuned param preset to a real agent's binding (M18 §1.7 / §2.7) — the tune→ship loop.
//
// The load-bearing rule (§1.7, the contentHash-drift trap): a preset is an authoring-time snippet of
// LITERAL values. "Apply preset" COPIES those values into the target binding's `models[binding].params`
// — the IR from then on carries the *values*, and the preset NAME appears NOWHERE in the hashed
// content. We must NOT write a preset *reference* (e.g. `paramsPreset: "fast"`) into the IR — a live
// reference would let a deployed agent silently change when the preset changes, destroying the
// contentHash immutability promise. And the frontend NEVER hashes (M15 §1.2): we write literal params,
// save through M11, and the server strips `view` + computes the new `contentHash`.

import type { IRDocument } from "@theygent/ir-types";

/**
 * Return a new IRDocument with `presetParams` copied (as literal values) into `models[bindingKey].params`.
 * Pure + immutable — the editor saves the result through M11 (the server hashes). Throws if the binding
 * does not exist (a typo'd binding must fail loudly, never silently create one).
 */
export function applyPresetToBinding(
  ir: IRDocument,
  bindingKey: string,
  presetParams: Record<string, unknown>,
): IRDocument {
  const models = (ir.models ?? {}) as Record<string, { params?: Record<string, unknown> } & object>;
  const target = models[bindingKey];
  if (!target) {
    throw new Error(
      `no model binding named '${bindingKey}'; bindings: [${Object.keys(models).join(", ")}]`,
    );
  }
  // Merge the preset's literal values over the binding's current params (preset wins). The preset
  // name is never written — only its values land in the hashed content.
  const nextParams = { ...(target.params ?? {}), ...presetParams };
  return {
    ...ir,
    models: {
      ...models,
      [bindingKey]: { ...target, params: nextParams },
    },
  } as IRDocument;
}
