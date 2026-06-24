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
});
