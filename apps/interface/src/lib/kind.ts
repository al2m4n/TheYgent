// The determinism `kind` a node MUST carry. For every node type this is the fixed value from the
// generated registry — EXCEPT the guardrail, whose kind follows its check: a model check makes a
// real classifier call (an activity), a rule check is evaluated inline (an orchestration). Deriving
// it here (instead of the static per-type lookup) is what lets the editor stamp the right kind when
// you pick a rule vs a model check, and lets the validator accept both — mirroring the server, which
// derives the same kind from the same config. Guardrail is the only type whose kind is per-instance.

import { type Kind, kindForType } from "@theygent/ir-types";

/** Derive the kind a node must carry from its type (and, for a guardrail, its check config). Returns
 * `undefined` for an unknown type, exactly like the underlying registry lookup. */
export function expectedKind(node: { type: string; config?: unknown }): Kind | undefined {
  if (node.type === "guardrail") {
    const check = (node.config as { check?: { type?: string } } | undefined)?.check;
    // Only a model check maps to `activity`; anything else — including an unset check — is
    // `orchestration`, matching the server's derivation.
    return check?.type === "model" ? "activity" : "orchestration";
  }
  return kindForType(node.type);
}
