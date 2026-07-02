import { connect, deleteEdges } from "../src/adapter";
import { validateGraph } from "../src/lib/validate";
import { sampleGraph } from "./fixtures";

describe("validateGraph (mirror of the backend's validate_graph)", () => {
  it("a well-formed graph has no issues", () => {
    expect(validateGraph(sampleGraph())).toEqual([]);
  });

  it("flags an unfed required in-port", () => {
    const ir = deleteEdges(sampleGraph(), ["e2"]); // n_out.in now unfed
    const issues = validateGraph(ir);
    expect(issues.some((i) => i.nodeId === "n_out" && /not connected/.test(i.message))).toBe(true);
  });

  it("flags an llm node referencing an undeclared model", () => {
    const ir = sampleGraph();
    const llm = ir.nodes?.find((n) => n.id === "n_llm");
    if (llm) llm.config = { model: "ghost", messages: [{ role: "user", content: "$in" }] };
    const issues = validateGraph(ir);
    expect(issues.some((i) => i.nodeId === "n_llm" && /undeclared model/.test(i.message))).toBe(
      true,
    );
  });

  it("flags an empty required config field", () => {
    const ir = sampleGraph();
    const llm = ir.nodes?.find((n) => n.id === "n_llm");
    if (llm) llm.config = { model: "", messages: [] };
    const issues = validateGraph(ir);
    expect(issues.some((i) => i.nodeId === "n_llm" && /'model' is required/.test(i.message))).toBe(
      true,
    );
  });

  it("flags a cycle", () => {
    // wire n_out.* back into n_llm to make a cycle (handles are advisory for this check).
    const ir = sampleGraph();
    ir.nodes?.find((n) => n.id === "n_out")?.ports?.out?.push({ id: "out", type: "any" });
    ir.nodes
      ?.find((n) => n.id === "n_llm")
      ?.ports?.in?.push({ id: "back", type: "any", required: false });
    const r = connect(ir, {
      source: "n_out",
      sourceHandle: "out",
      target: "n_llm",
      targetHandle: "back",
    });
    const issues = validateGraph(r.ir ?? ir);
    expect(issues.some((i) => /cycle/.test(i.message))).toBe(true);
  });

  it("flags schema-invalid-but-parseable config (gates Save) — not just JSON parse-ability", () => {
    const ir = sampleGraph();
    const llm = ir.nodes?.find((n) => n.id === "n_llm");
    // Valid JSON, but `model` should be a string and `messages` an array of objects — the per-type
    // JSON Schema rejects this. The editor's gate = (errorCount > 0), so this blocks save.
    if (llm) llm.config = { model: 123, messages: "not-an-array" };
    const issues = validateGraph(ir);
    const errors = issues.filter((i) => i.severity === "error" && i.nodeId === "n_llm");
    expect(errors.length).toBeGreaterThan(0);
    // it specifically caught the type mismatches, not just "required missing".
    expect(errors.some((e) => /config\/model/.test(e.message))).toBe(true);
    expect(errors.some((e) => /config\/messages/.test(e.message))).toBe(true);
  });

  it("a well-typed but schema-valid config passes the schema check", () => {
    const ir = sampleGraph();
    // sampleGraph's llm config is { model: "default", messages: [{role,content}] } — schema-valid.
    expect(validateGraph(ir).filter((i) => i.severity === "error")).toEqual([]);
  });

  it("flags a dangling edge handle", () => {
    const ir = sampleGraph();
    // point e1 at a non-existent in-port.
    const e1 = ir.edges?.find((e) => e.id === "e1");
    if (e1) e1.targetHandle = "nope";
    const issues = validateGraph(ir);
    expect(issues.some((i) => i.edgeId === "e1" && /no in-port/.test(i.message))).toBe(true);
  });

  // A capability tool node + an llm tools port wired by a `tool` edge.
  function capabilityGraph() {
    const ir = sampleGraph();
    ir.nodes
      ?.find((n) => n.id === "n_llm")
      ?.ports?.in?.push({ id: "tools", type: "any", required: false, role: "tool" });
    ir.nodes?.push({
      id: "n_echo",
      type: "tool",
      kind: "activity",
      label: null,
      config: { tool: "echo", description: "echo" },
      ports: {
        in: [{ id: "in", type: "any", required: true }],
        out: [{ id: "out", type: "any", required: true }],
      },
    });
    ir.edges?.push({
      id: "e_tool",
      source: "n_echo",
      sourceHandle: "out",
      target: "n_llm",
      targetHandle: "tools",
      channel: "tool",
      condition: null,
    });
    return ir;
  }

  it("a capability tool node is NOT flagged for its unfed required in-port", () => {
    // Regression: the validator must exempt a capability node (its args come from the model).
    const issues = validateGraph(capabilityGraph());
    expect(issues.some((i) => i.nodeId === "n_echo" && /not connected/.test(i.message))).toBe(
      false,
    );
    expect(issues.filter((i) => i.severity === "error")).toEqual([]);
  });

  it("flags a tool node wired as BOTH a capability and a step", () => {
    const ir = capabilityGraph();
    // also wire n_echo's output into n_out as a data edge → mixed mode.
    ir.edges?.push({
      id: "e_mixed",
      source: "n_echo",
      sourceHandle: "out",
      target: "n_out",
      targetHandle: "in",
      channel: "data",
      condition: null,
    });
    const issues = validateGraph(ir);
    expect(
      issues.some((i) => i.nodeId === "n_echo" && /capability .* or a step/.test(i.message)),
    ).toBe(true);
  });
});

describe("validateGraph — durable-only + boundary-node hints", () => {
  function withHuman() {
    const ir = sampleGraph();
    ir.nodes?.push({
      id: "n_gate",
      type: "human",
      kind: "boundary",
      label: null,
      config: { prompt: "ok?" },
      ports: {
        in: [{ id: "in", type: "any", required: false }],
        out: [{ id: "out", type: "any", required: true }],
      },
    });
    return ir;
  }

  it("warns at graph level when a durable-only node type is present", () => {
    const issues = validateGraph(withHuman());
    expect(issues.some((i) => i.severity === "warning" && /durable-only/.test(i.message))).toBe(
      true,
    );
  });

  it("warns when a human gate falls back to an unset default on timeout", () => {
    const ir = withHuman();
    const gate = ir.nodes?.find((n) => n.id === "n_gate");
    if (gate) gate.config = { prompt: "ok?", onTimeout: "default", default: null };
    const issues = validateGraph(ir);
    expect(issues.some((i) => i.nodeId === "n_gate" && /no default value/.test(i.message))).toBe(
      true,
    );
  });

  it("rejects a non-positive maxDepth on a pinned-body node (mirrors the backend)", () => {
    const ir = sampleGraph();
    ir.nodes?.push({
      id: "n_sg",
      type: "subgraph",
      kind: "boundary",
      label: null,
      config: { agent: "agent.child", version: "0.1.0", contentHash: null, maxDepth: 0 },
      ports: {
        in: [{ id: "in", type: "any", required: false }],
        out: [{ id: "out", type: "any", required: true }],
      },
    });
    const issues = validateGraph(ir);
    expect(issues.some((i) => i.nodeId === "n_sg" && /maxDepth/.test(i.message))).toBe(true);
  });

  it("warns when more than one output node exists (two live outputs fail at run time)", () => {
    const ir = sampleGraph();
    ir.nodes?.push({
      id: "n_out2",
      type: "output",
      kind: "boundary",
      label: null,
      config: {},
      ports: { in: [{ id: "in", type: "any", required: false }], out: [] },
    });
    const issues = validateGraph(ir);
    expect(issues.some((i) => /output nodes/.test(i.message))).toBe(true);
  });
});
