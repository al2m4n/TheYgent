import { describe, expect, it } from "vitest";
import { validateIR } from "../src/lib/ir-validate";

const GOOD = JSON.stringify({
  schemaVersion: "1.0",
  id: "agt_x",
  name: "t",
  version: "0.1.0",
  models: {},
  tools: {},
  nodes: [
    { id: "n_in", type: "input", kind: "boundary", ports: { in: [], out: [] } },
    { id: "n_out", type: "output", kind: "boundary", ports: { in: [], out: [] } },
  ],
  edges: [{ id: "e1", source: "n_in", target: "n_out", channel: "data" }],
});

describe("validateIR", () => {
  it("accepts a structurally valid trivial IR", () => {
    const { issues } = validateIR(GOOD);
    expect(issues).toEqual([]);
  });

  it("flags a JSON syntax error and returns no doc", () => {
    const { doc, issues } = validateIR("{ not json");
    expect(doc).toBeNull();
    expect(issues[0].severity).toBe("error");
    expect(issues[0].message).toMatch(/invalid JSON/);
  });

  it("reports missing required envelope fields", () => {
    const { issues } = validateIR(JSON.stringify({ name: "x" }));
    const msgs = issues.map((i) => i.message);
    expect(msgs).toContain('missing required field "id"');
    expect(msgs).toContain('missing required field "version"');
  });

  it("rejects a node whose kind does not match its type", () => {
    const bad = JSON.parse(GOOD);
    bad.nodes[0].kind = "activity"; // input must be boundary
    const { issues } = validateIR(JSON.stringify(bad));
    expect(issues.some((i) => /must have kind "boundary"/.test(i.message))).toBe(true);
  });

  it("rejects an edge that references a missing node", () => {
    const bad = JSON.parse(GOOD);
    bad.edges[0].target = "ghost";
    const { issues } = validateIR(JSON.stringify(bad));
    expect(issues.some((i) => /references no node/.test(i.message))).toBe(true);
  });

  it("warns (not errors) on an unknown node type", () => {
    const bad = JSON.parse(GOOD);
    bad.nodes[0].type = "frobnicate";
    bad.nodes[0].kind = "activity";
    const { issues } = validateIR(JSON.stringify(bad));
    const issue = issues.find((i) => /unknown type "frobnicate"/.test(i.message));
    expect(issue?.severity).toBe("warning");
  });
});
