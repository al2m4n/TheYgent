// The node-type registry is the generated bridge to packages/ir (§2.2). These assertions document
// what the palette derives and catch an accidental hand-edit of the generated JSON. The CI
// type-drift guard (regenerate + git diff) is the real lockstep check; this is the in-suite sanity.

import { NODE_TYPES, NODE_TYPE_LIST, kindForType } from "@theygent/ir-types";

describe("node-type registry (derived from packages/ir)", () => {
  it("includes the built types and the M14 additive-lowering types", () => {
    for (const t of ["input", "output", "llm", "tool", "mcp_tool", "router"]) {
      expect(NODE_TYPES[t], `missing built type ${t}`).toBeDefined();
    }
    for (const t of ["human", "subgraph", "loop", "map"]) {
      expect(NODE_TYPES[t], `missing M14 type ${t}`).toBeDefined();
    }
  });

  it("pins each type to its determinism kind (the lookup that keeps kind off RF nodes)", () => {
    expect(kindForType("llm")).toBe("activity");
    expect(kindForType("router")).toBe("orchestration");
    expect(kindForType("input")).toBe("boundary");
    expect(kindForType("loop")).toBe("orchestration");
    expect(kindForType("unknown-type")).toBeUndefined();
  });

  it("declares default ports per type (tool carries the ok/err contract)", () => {
    expect(NODE_TYPES.input.ports.out?.map((p) => p.id)).toEqual(["out"]);
    expect(NODE_TYPES.output.ports.in?.map((p) => p.id)).toEqual(["in"]);
    expect(NODE_TYPES.tool.ports.out?.map((p) => p.id)).toEqual(["out", "err"]);
  });

  it("exposes a shaped default config drawn from the per-type schema", () => {
    expect(NODE_TYPES.llm.defaultConfig).toHaveProperty("model");
    expect(NODE_TYPES.llm.defaultConfig).toHaveProperty("messages");
    expect(NODE_TYPES.router.defaultConfig).toHaveProperty("select");
  });

  it("NODE_TYPE_LIST is the sorted, deduped palette source", () => {
    const ids = NODE_TYPE_LIST.map((s) => s.type);
    expect(ids).toEqual([...ids].sort());
    expect(new Set(ids).size).toBe(ids.length);
  });
});
