import {
  addNode,
  connect,
  deleteEdges,
  deleteNodes,
  duplicateNode,
  irToReactFlow,
  reactFlowToIr,
  relayout,
  setNodeIcon,
  setNodePositions,
  setToolKind,
  toolKindOf,
  updateEdge,
  updateNodeConfig,
  withDerivedTools,
} from "../src/adapter";
import type { ViewBlock } from "../src/adapter/types";
import { sameHashedContent, viewStrippedContent } from "../src/lib/canonical";
import { nastyGraph, sampleGraph, sampleGraphNoView } from "./fixtures";

// M22 capability wiring: sampleGraph + an `echo` tool node + a `tool`-role `tools` port on the llm.
// `capabilityBase` has no tool edge yet (for connect() tests); `capabilityWired` adds the edge.
function capabilityBase() {
  const ir = sampleGraph();
  const llm = ir.nodes?.find((n) => n.id === "n_llm");
  llm?.ports?.in?.push({ id: "tools", type: "any", required: false, role: "tool" });
  ir.nodes?.push({
    id: "n_echo",
    type: "tool",
    kind: "activity",
    label: null,
    config: { tool: "echo", description: "echo the value back" },
    ports: {
      in: [{ id: "in", type: "any", required: true }],
      out: [
        { id: "out", type: "any", required: true },
        { id: "err", type: "error", required: true },
        { id: "use", type: "any", required: true, role: "tool" },
      ],
    },
  });
  return ir;
}

function capabilityWired() {
  const ir = capabilityBase();
  ir.edges?.push({
    id: "e_tool",
    source: "n_echo",
    sourceHandle: "use",
    target: "n_llm",
    targetHandle: "tools",
    channel: "tool",
    condition: null,
  });
  // M22: ir.tools is the DERIVED registry (keyed by node id) — a saved capability graph carries it,
  // so the load→save round-trip is identity (reactFlowToIr re-derives the same).
  ir.tools = { n_echo: { kind: "builtin", ref: "echo" } };
  return ir;
}

describe("adapter round-trip (the headline — §4)", () => {
  it("IR → React Flow → IR is identity on view-stripped content", () => {
    const ir = sampleGraph();
    const back = reactFlowToIr(irToReactFlow(ir), ir);
    // Canonical-equal on the content the server would hash (view + contentHash excluded). This is
    // the frontend analogue of M13's walker/compiler parity — the seam is lossless.
    expect(viewStrippedContent(back)).toEqual(viewStrippedContent(ir));
    expect(sameHashedContent(back, ir)).toBe(true);
  });

  it("is identity on the NASTY graph — control edges, router conditions, ok/err + multi-in ports, collapsed view", () => {
    const ir = nastyGraph();
    const back = reactFlowToIr(irToReactFlow(ir), ir);

    // The headline: identity on the content the server would hash, for the genuinely lossy graph.
    expect(viewStrippedContent(back)).toEqual(viewStrippedContent(ir));
    expect(sameHashedContent(back, ir)).toBe(true);

    // And spell out that each lossy surface specifically survived:
    const edge = (id: string) => back.edges?.find((e) => e.id === id);
    expect(edge("e6")?.channel).toBe("control"); // control channel preserved
    expect(edge("e2")?.condition).toBe("$in.in.ok"); // router-driven condition preserved
    expect(edge("e4")?.condition).toBe("fallback");
    const tool = back.nodes?.find((n) => n.id === "n_tool");
    expect(tool?.ports?.out?.map((p) => [p.id, p.type])).toEqual([
      ["out", "any"],
      ["err", "error"], // the ok/err shape, incl. the error-typed port
    ]);
    const llm = back.nodes?.find((n) => n.id === "n_llm");
    expect(llm?.ports?.in?.map((p) => p.id)).toEqual(["file", "question"]); // multi-in-port

    // The collapsed/viewport view data is view-only — it must NOT have leaked into hashed content.
    const content = viewStrippedContent(ir) as Record<string, unknown>;
    expect(JSON.stringify(content)).not.toContain("collapsed");
    expect(JSON.stringify(content)).not.toContain("viewport");
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

describe("node icon override (a `view`-only display field, §1.4)", () => {
  it("setNodeIcon is layout-only — would-be contentHash is unchanged", () => {
    const ir = sampleGraph();
    const next = setNodeIcon(ir, "n_llm", "bot");
    expect(sameHashedContent(next, ir)).toBe(true);
    expect((next.view as ViewBlock).nodes?.n_llm.icon).toBe("bot");
    // pure function — the original is untouched.
    expect((ir.view as ViewBlock).nodes?.n_llm.icon).toBeUndefined();
  });

  it("an override surfaces on the RF node and round-trips back into the view", () => {
    const ir = setNodeIcon(sampleGraph(), "n_llm", "bot");
    const rf = irToReactFlow(ir);
    expect(rf.nodes.find((n) => n.id === "n_llm")?.data.icon).toBe("bot");
    // nodes with NO override carry no icon key (the one rule holds for them).
    expect(rf.nodes.find((n) => n.id === "n_in")?.data).not.toHaveProperty("icon");
    // save → the override is back in the view, content still identical.
    const back = reactFlowToIr(rf, ir);
    expect((back.view as ViewBlock).nodes?.n_llm.icon).toBe("bot");
    expect(sameHashedContent(back, ir)).toBe(true);
  });

  it("clearing reverts to the default and survives a later drag", () => {
    let ir = setNodeIcon(sampleGraph(), "n_llm", "bot");
    ir = setNodeIcon(ir, "n_llm", null);
    expect((ir.view as ViewBlock).nodes?.n_llm.icon).toBeUndefined();
    // a drag must not wipe an existing override.
    let withIcon = setNodeIcon(sampleGraph(), "n_llm", "bot");
    withIcon = setNodePositions(withIcon, { n_llm: { x: 1, y: 2 } });
    expect((withIcon.view as ViewBlock).nodes?.n_llm.icon).toBe("bot");
    expect((withIcon.view as ViewBlock).nodes?.n_llm.position).toEqual({ x: 1, y: 2 });
  });

  it("relayout keeps the override while rewriting positions", () => {
    const ir = setNodeIcon(sampleGraph(), "n_llm", "bot");
    const next = relayout(ir);
    expect((next.view as ViewBlock).nodes?.n_llm.icon).toBe("bot");
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
    // ok/err (the M6 contract) + the M22 `use` capability handle.
    expect(added?.ports?.out?.map((p) => p.id)).toEqual(["out", "err", "use"]);
    expect(added?.ports?.out?.find((p) => p.id === "use")?.role).toBe("tool");
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

  it("M22: connect wires a tool node's `use` handle into the llm tools port as a channel:tool edge", () => {
    const r = connect(capabilityBase(), {
      source: "n_echo",
      sourceHandle: "use",
      target: "n_llm",
      targetHandle: "tools",
    });
    expect(r.error).toBeUndefined();
    const e = r.ir?.edges?.find((x) => x.source === "n_echo" && x.target === "n_llm");
    expect(e?.channel).toBe("tool");
    expect(e?.targetHandle).toBe("tools");
  });

  it("M22: rejects a data handle wired into a tools port (role mismatch)", () => {
    // n_in's `out` is a data handle; the llm `tools` port is role tool → like-to-like rejects it.
    const r = connect(capabilityBase(), {
      source: "n_in",
      sourceHandle: "out",
      target: "n_llm",
      targetHandle: "tools",
    });
    expect(r.ir).toBeUndefined();
    expect(r.error).toMatch(/cannot connect a data handle to a tool handle/);
  });

  it("M22: allows several tool capabilities into one llm tools port (no dedup)", () => {
    let ir = capabilityBase();
    ir.nodes?.push({
      id: "n_echo2",
      type: "tool",
      kind: "activity",
      label: null,
      config: { tool: "http_fetch", description: "fetch" },
      ports: {
        in: [{ id: "in", type: "any", required: true }],
        out: [
          { id: "out", type: "any", required: true },
          { id: "use", type: "any", required: true, role: "tool" },
        ],
      },
    });
    const r1 = connect(ir, {
      source: "n_echo",
      sourceHandle: "use",
      target: "n_llm",
      targetHandle: "tools",
    });
    expect(r1.error).toBeUndefined();
    ir = r1.ir as typeof ir;
    const r2 = connect(ir, {
      source: "n_echo2",
      sourceHandle: "use",
      target: "n_llm",
      targetHandle: "tools",
    });
    expect(r2.error).toBeUndefined(); // a tools port takes many capabilities (not deduped like data)
  });

  it("M22: round-trips a capability graph (tool edge + tools port) identically", () => {
    const ir = capabilityWired();
    const back = reactFlowToIr(irToReactFlow(ir), ir);
    expect(sameHashedContent(back, ir)).toBe(true);
    const e = back.edges?.find((x) => x.channel === "tool");
    expect(e?.source).toBe("n_echo");
    expect(e?.target).toBe("n_llm");
    const toolsPort = back.nodes
      ?.find((n) => n.id === "n_llm")
      ?.ports?.in?.find((p) => p.id === "tools");
    expect(toolsPort?.role).toBe("tool");
  });

  it("M22 D1: toolKindOf derives the kind from config (builtin / rest / mcp)", () => {
    const ir = capabilityBase();
    const echo = ir.nodes?.find((n) => n.id === "n_echo");
    if (!echo) throw new Error("fixture");
    expect(toolKindOf(echo)).toBe("builtin");
    expect(toolKindOf({ ...echo, config: { connection: "c_api" } })).toBe("rest");
    expect(toolKindOf({ ...echo, config: { urlTemplate: "https://x" } })).toBe("rest");
    expect(toolKindOf({ ...echo, type: "mcp_tool", config: { server: "fs", tool: "read" } })).toBe(
      "mcp",
    );
  });

  it("M22 D1: setToolKind builtin→rest seeds a url placeholder, keeps the tool type", () => {
    const next = setToolKind(capabilityBase(), "n_echo", "rest");
    const n = next.nodes?.find((x) => x.id === "n_echo");
    expect(n?.type).toBe("tool");
    expect(toolKindOf(n!)).toBe("rest");
    // an empty REST tool still reads as http (matches the backend's _capability_binding).
    expect((n?.config as Record<string, unknown>).urlTemplate).toBe("https://");
    // the description carries across the kind switch.
    expect((n?.config as Record<string, unknown>).description).toBe("echo the value back");
  });

  it("M22 D1: setToolKind builtin→mcp swaps to the mcp_tool type and re-derives ir.tools", () => {
    const wired = (() => {
      // wire the capability edge, then derive ir.tools the way the Editor's applyIr does.
      const r = connect(capabilityBase(), {
        source: "n_echo",
        sourceHandle: "use",
        target: "n_llm",
        targetHandle: "tools",
      });
      return withDerivedTools(r.ir!);
    })();
    expect(wired.tools?.n_echo).toEqual({ kind: "builtin", ref: "echo" });
    const next = setToolKind(wired, "n_echo", "mcp");
    const n = next.nodes?.find((x) => x.id === "n_echo");
    expect(n?.type).toBe("mcp_tool");
    expect(toolKindOf(n!)).toBe("mcp");
    // the `use` capability handle (and the edge) survive the swap — both types declare it.
    expect(n?.ports?.out?.some((p) => p.id === "use")).toBe(true);
    expect(next.edges?.some((e) => e.channel === "tool" && e.source === "n_echo")).toBe(true);
    // the derived registry now tags it mcp.
    expect((next.tools?.n_echo as { kind?: string }).kind).toBe("mcp");
  });

  it("M22 D1: setToolKind is a no-op when the kind is unchanged", () => {
    const ir = capabilityBase();
    expect(setToolKind(ir, "n_echo", "builtin")).toBe(ir);
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
