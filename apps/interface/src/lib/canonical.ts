// A stable structural projection of an IR with the non-hashed fields stripped.
//
// THIS IS NOT A contentHash CANONICALIZER. The frontend NEVER computes the hash or canonicalizes
// for version identity — that lives in exactly one place, the Pydantic IR package (M15 §1.2; a
// second JS canonicalizer would drift from Python and silently corrupt version identity). This is
// only a test/UX helper: it lets us ASSERT "the content the server WOULD hash is unchanged"
// (round-trip identity, view isolation) without ever producing a hash. It mirrors the server's
// three exclusions (`view`, `contentHash`, `content_hash`) so the projection tracks what the hash
// covers, but it stops at structural equality — it does not hash, and it is never sent anywhere.

const EXCLUDED = new Set(["view", "contentHash", "content_hash"]);

function canonical(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      out[key] = canonical((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

/** The IR projected to its would-be-hashed content: top-level `view`/`contentHash` removed, keys
 * deep-sorted. Equal projections ⇒ the server would compute the same hash. */
export function viewStrippedContent(ir: unknown): unknown {
  if (!ir || typeof ir !== "object") return canonical(ir);
  const out: Record<string, unknown> = {};
  for (const key of Object.keys(ir as Record<string, unknown>).sort()) {
    if (EXCLUDED.has(key)) continue;
    out[key] = canonical((ir as Record<string, unknown>)[key]);
  }
  return out;
}

/** True when two IRs have identical would-be-hashed content (layout/hash fields ignored). */
export function sameHashedContent(a: unknown, b: unknown): boolean {
  return JSON.stringify(viewStrippedContent(a)) === JSON.stringify(viewStrippedContent(b));
}
