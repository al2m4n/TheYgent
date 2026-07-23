// The payload-schema field list. Every multi-input sample declares an object schema so a caller
// knows which fields to send and the graph can drill them with `$in.in.<name>`; before this the
// only control was a raw JSON textarea. These pin the two directions — read a shipped schema as
// rows, and write rows back — against the EXACT values the 12 samples carry, so the form can never
// quietly reshape an author's schema.

import { describe, expect, it } from "vitest";
import { rowsToSchema, schemaToRows } from "../src/components/Inspector";

// Verbatim from apps/control-plane/src/theygent_control_plane/samples/data/.
const EXPENSE = {
  type: "object",
  required: ["amount", "currency", "to"],
  properties: {
    amount: { type: "number" },
    currency: { type: "string" },
    to: { type: "string" },
    memo: { type: "string" },
  },
};
const TOOL_BELT = {
  type: "object",
  required: ["code", "question"],
  properties: {
    code: { type: "string", description: "ISO-3166 two-letter country code, e.g. DE" },
    question: { type: "string" },
  },
};
const BATCH_BRIEFING = { type: "array", items: { type: "string" } };
const HUMAN_GATE = { required: ["decision"] };

describe("schemaToRows", () => {
  it("reads a sample's object schema as a field list, required flags and all", () => {
    expect(schemaToRows(EXPENSE)).toEqual([
      { name: "amount", type: "number", required: true, description: "" },
      { name: "currency", type: "string", required: true, description: "" },
      { name: "to", type: "string", required: true, description: "" },
      { name: "memo", type: "string", required: false, description: "" },
    ]);
  });

  it("carries a per-field description through", () => {
    const rows = schemaToRows(TOOL_BELT);
    expect(rows?.[0]).toEqual({
      name: "code",
      type: "string",
      required: true,
      description: "ISO-3166 two-letter country code, e.g. DE",
    });
  });

  it("shows a required-but-undeclared name as an untyped field", () => {
    // The human-gate shape: `{"required": ["decision"]}` names a field without describing it.
    expect(schemaToRows(HUMAN_GATE)).toEqual([
      { name: "decision", type: "", required: true, description: "" },
    ]);
  });

  it("treats an absent schema as no fields", () => {
    expect(schemaToRows(undefined)).toEqual([]);
    expect(schemaToRows(null)).toEqual([]);
  });

  it("refuses what a field list cannot say, so those keep the JSON editor", () => {
    // An array payload (batch-briefing), a nested object, and richer JSON Schema all opt out
    // rather than being silently flattened.
    expect(schemaToRows(BATCH_BRIEFING)).toBeNull();
    expect(schemaToRows({ type: "object", properties: { a: { enum: ["x"] } } })).toBeNull();
    expect(
      schemaToRows({ type: "object", properties: { a: { type: "object", properties: {} } } }),
    ).toBeNull();
    expect(schemaToRows({ oneOf: [{ type: "string" }] })).toBeNull();
    expect(schemaToRows([1, 2])).toBeNull();
  });
});

describe("rowsToSchema", () => {
  const round = (schema: unknown) => {
    const rows = schemaToRows(schema);
    if (!rows) throw new Error("not representable");
    const hadType =
      schema !== null && typeof schema === "object" && !Array.isArray(schema)
        ? (schema as Record<string, unknown>).type !== undefined
        : false;
    return rowsToSchema(rows, hadType);
  };

  it("round-trips every object schema the samples ship, unchanged", () => {
    expect(round(EXPENSE)).toEqual(EXPENSE);
    expect(round(TOOL_BELT)).toEqual(TOOL_BELT);
  });

  it("does not promote a bare required-list into a full object schema", () => {
    // Editing `{"required": ["decision"]}` must not invent `type` and `properties` the author
    // never wrote — that would change the agent's content hash for no semantic gain.
    expect(round(HUMAN_GATE)).toEqual(HUMAN_GATE);
  });

  it("omits an untyped, undescribed field from properties but keeps it required", () => {
    const out = rowsToSchema(
      [
        { name: "a", type: "", required: true, description: "" },
        { name: "b", type: "string", required: false, description: "" },
      ],
      true,
    ) as Record<string, unknown>;
    expect(out.properties).toEqual({ b: { type: "string" } });
    expect(out.required).toEqual(["a"]);
  });

  it("drops unnamed rows — a half-typed field is not a declaration", () => {
    const out = rowsToSchema(
      [
        { name: "", type: "string", required: true, description: "" },
        { name: "kept", type: "string", required: true, description: "" },
      ],
      true,
    ) as Record<string, unknown>;
    expect(Object.keys(out.properties as object)).toEqual(["kept"]);
    expect(out.required).toEqual(["kept"]);
  });

  it("clears back to null when the last field is removed", () => {
    expect(rowsToSchema([], false)).toBeNull();
  });
});
