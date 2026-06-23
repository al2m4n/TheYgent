import {
  addNode,
  connect,
  deleteEdges,
  deleteNodes,
  duplicateNode,
  irToReactFlow,
  reactFlowToIr,
  relayout,
  setNodePositions,
  updateEdge,
  updateNodeConfig,
} from "../src/adapter";
import type { ViewBlock } from "../src/adapter/types";
import { sameHashedContent, viewStrippedContent } from "../src/lib/canonical";
import { sampleGraph, sampleGraphNoView } from "./fixtures";

describe("adapter round-trip (the headline — §4)", () => {
  it("IR → React Flow → IR is identity on view-stripped content", () => {
    const ir = sampleGraph();
    const back = reactFlowToIr(irToReactFlow(ir), ir);
    // Canonical-equal on the content the server would hash (view + contentHash excluded). This is
    // the frontend analogue of M13's walker/compiler parity — the seam is lossless.
    expect(viewStrippedContent(back)).toEqual(viewStrippedContent(ir));
    expect(sameHashedContent(back, ir)).toBe(true);
  });

  it("preserves node and edge ids, types, ports, channels through the round-trip", () => {
    const ir = sampleGraph();
    const back = reactFlowToIr(irToReactFlow(ir), ir);
    expect(back.nodes?.map((n) => n.id)).toEqual(["n_in", "n_llm", "n_out"]);
    expect(back.nodes?.map((n) => n.kind)).toEqual(["boundary", "activity", "boundary"]);
    expect(back.edges?.map((e) => e.id)).toEqual(["e1", "e2"]);
    const llm = back.nodes?.find((n) => n.id === "n_llm");
    expect(llm?.ports?.in?.[0]).toEqual({ id: "in", type: "any", required: true });
    expect(llm?.config).toEqual({ model: "default", messages: [{ role: "user", content: "$in" }] });
  });
});

describe("missing view → auto-layout (§4)", () => {
  it("a view-less IR loads, lays out, and round-trips to valid IR", () => {
    const ir = sampleGraphNoView();
    const rf = irToReactFlow(ir);
    // every node got a position (no NaN/undefined) — distinct, deterministic columns.
    for (const n of rf.nodes) {
      expect(Number.isFinite(n.position.x)).toBe(true);
      expect(Number.isFinite(n.position.y)).toBe(true);
    }
    // the linear chain lays out left-to-right by longest path.
    const x = Object.fromEntries(rf.nodes.map((n) => [n.id, n.position.x]));
    expect(x.n_in).toBeLessThan(x.n_llm);
    expect(x.n_llm).toBeLessThan(x.n_out);
    // content (view-stripped) is unchanged by rendering a view-less graph.
    const back = reactFlowToIr(rf, ir);
    expect(sameHashedContent(back, ir)).toBe(true);
  });

  it("is deterministic — two loads of the same view-less IR land identically", () => {
    const a = irToReactFlow(sampleGraphNoView());
    const b = irToReactFlow(sampleGraphNoView());
    expect(a.nodes.map((n) => n.position)).toEqual(b.nodes.map((n) => n.position));
  });
});

describe("view isolation (§4, decision §1.4)", () => {
  it("dragging a node changes view only — would-be contentHash is unchanged", () => {
    const ir = sampleGraph();
    const dragged = setNodePositions(ir, { n_llm: { x: 999, y: 777 } });
    expect(sameHashedContent(dragged, ir)).toBe(true);
    const view = dragged.view as ViewBlock;
    expect(view.nodes?.n_llm.position).toEqual({ x: 999, y: 777 });
    // the original is untouched (pure function).
    expect((ir.view as ViewBlock).nodes?.n_llm.position).toEqual({ x: 300, y: 80 });
  });
});

describe("no React Flow shape leaks into node data (§0 / Do-NOT)", () => {
  it("RF node data carries only label/type/ports — never kind/models/contentHash", () => {
    const rf = irToReactFlow(sampleGraph());
    for (const n of rf.nodes) {
      expect(Object.keys(n.data).sort()).toEqual(["label", "nodeType", "ports"]);
      expect(n.data).not.toHaveProperty("kind");
      expect(n.data).not.toHaveProperty("models");
      expect(n.data).not.toHaveProperty("contentHash");
    }
  });
});

describe("canvas edits produce valid IR (§4)", () => {
  it("addNode creates a node with the registry kind, default config, declared ports", () => {
    const ir = sampleGraph();
    const next = addNode(ir, "tool", { x: 200, y: 300 });
    const added = next.nodes?.find((n) => n.type === "tool" && n.id !== "n_in");
    expect(added).toBeDefined();
    expect(added?.kind).toBe("activity");
    expect(added?.ports?.out?.map((p) => p.id)).toEqual(["out", "err"]); // tool ok/err contract
    expect(added?.config).toHaveProperty("tool"); // default config from the per-type schema
    // its position landed in the view, not in hashed content.
    const view = next.view as ViewBlock;
    expect(view.nodes?.[added?.id ?? ""]?.position).toEqual({ x: 200, y: 300 });
  });

  it("connect creates a valid data edge referencing real handles", () => {
    let ir = sampleGraph();
    ir = deleteEdges(ir, ["e2"]); // free up n_out's in-port
    const r = connect(ir, {
      source: "n_llm",
      sourceHandle: "out",
      target: "n_out",
      targetHandle: "in",
    });
    expect(r.error).toBeUndefined();
    const e = r.ir?.edges?.find((x) => x.source === "n_llm" && x.target === "n_out");
    expect(e?.channel).toBe("data");
    expect(e?.sourceHandle).toBe("out");
    expect(e?.targetHandle).toBe("in");
  });

  it("rejects a connection to a non-existent handle", () => {
    const ir = sampleGraph();
    const r = connect(ir, {
      source: "n_in",
      sourceHandle: "out",
      target: "n_out",
      targetHandle: "nope",
    });
    expect(r.ir).toBeUndefined();
    expect(r.error).toMatch(/no in-port/);
  });

  it("rejects a second data edge into an already-fed in-port (ambiguous multi-input)", () => {
    const ir = sampleGraph(); // e2 already feeds n_out.in
    const r = connect(ir, {
      source: "n_in",
      sourceHandle: "out",
      target: "n_out",
      targetHandle: "in",
    });
    expect(r.error).toMatch(/already fed|ambiguous/);
  });

  it("deleteNodes removes the node, its incident edges, and its view entry", () => {
    const ir = sampleGraph();
    const next = deleteNodes(ir, ["n_llm"]);
    expect(next.nodes?.some((n) => n.id === "n_llm")).toBe(false);
    expect(next.edges?.some((e) => e.source === "n_llm" || e.target === "n_llm")).toBe(false);
    expect((next.view as ViewBlock).nodes?.n_llm).toBeUndefined();
  });

  it("updateNodeConfig replaces only the targeted node's config", () => {
    const ir = sampleGraph();
    const next = updateNodeConfig(ir, "n_llm", { model: "default", messages: [] });
    expect(next.nodes?.find((n) => n.id === "n_llm")?.config).toEqual({
      model: "default",
      messages: [],
    });
    // a config edit IS hashed content — so this MUST differ from the original.
    expect(sameHashedContent(next, ir)).toBe(false);
  });

  it("duplicateNode clones config/ports with a fresh id, offset, and no edges", () => {
    const ir = sampleGraph();
    const { ir: next, newId } = duplicateNode(ir, "n_llm");
    const orig = ir.nodes?.find((n) => n.id === "n_llm");
    const copy = next.nodes?.find((n) => n.id === newId);
    expect(newId).not.toBe("n_llm");
    expect(copy?.type).toBe("llm");
    expect(copy?.config).toEqual(orig?.config);
    expect(copy?.label).toBe("answer copy");
    // a copy starts unconnected — no edges reference it.
    expect(next.edges?.some((e) => e.source === newId || e.target === newId)).toBe(false);
    // it landed in the view, offset from the original — layout only.
    const view = next.view as ViewBlock;
    expect(view.nodes?.[newId]?.position).toEqual({ x: 340, y: 120 });
  });

  it("updateEdge patches channel/condition without touching other edges", () => {
    const ir = sampleGraph();
    const next = updateEdge(ir, "e1", { channel: "control", condition: "$in.in.go" });
    const e1 = next.edges?.find((e) => e.id === "e1");
    expect(e1?.channel).toBe("control");
    expect(e1?.condition).toBe("$in.in.go");
    expect(next.edges?.find((e) => e.id === "e2")?.channel).toBe("data");
  });

  it("relayout rewrites view positions (layout only) without touching hashed content", () => {
    const ir = sampleGraph();
    const next = relayout(ir);
    expect(sameHashedContent(next, ir)).toBe(true); // layout never changes hashed content
    const view = next.view as ViewBlock;
    // the linear chain lays out left-to-right by longest path.
    const x = (id: string) => view.nodes?.[id]?.position.x ?? 0;
    expect(x("n_in")).toBeLessThan(x("n_llm"));
    expect(x("n_llm")).toBeLessThan(x("n_out"));
  });
});
