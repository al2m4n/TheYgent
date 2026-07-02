// The guardrail authoring panel + the per-instance kind seam. A guardrail is the one node whose
// `kind` follows its config: a rule check is inline (orchestration), a model check is an LLM-judge
// call (activity). These tests pin that seam end to end — the shared derivation, the mutation that
// stamps kind, the save round-trip that must not clobber it, the validator that must accept it, and
// the panel that authors it.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import { type IRDocument, kindForType } from "@theygent/ir-types";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  type GuardrailCheck,
  irToReactFlow,
  reactFlowToIr,
  setGuardrailCheck,
} from "../src/adapter";
import { sameHashedContent } from "../src/lib/canonical";
import { expectedKind } from "../src/lib/kind";
import { validateGraph } from "../src/lib/validate";

vi.mock("../src/lib/api", () => ({
  api: {
    listModels: vi.fn().mockResolvedValue([]),
    listConnections: vi.fn().mockResolvedValue([]),
    listMcpServers: vi.fn().mockResolvedValue([]),
    listTriggers: vi.fn().mockResolvedValue([]),
    listAgents: vi.fn().mockResolvedValue([]),
  },
}));

import { Inspector } from "../src/components/Inspector";
import { api } from "../src/lib/api";

// input → guardrail → (pass → output, block → output): a structurally valid graph so validation
// errors are attributable to the guardrail, not to an unfed port.
function graph(kind: string, check: unknown, models: Record<string, unknown> = {}): IRDocument {
  return {
    schemaVersion: "1.0",
    id: "a",
    name: "n",
    version: "0.1.0",
    models,
    tools: {},
    nodes: [
      {
        id: "n_in",
        type: "input",
        kind: "boundary",
        label: null,
        config: {},
        ports: { in: [], out: [{ id: "out", type: "any", required: true }] },
      },
      {
        id: "n_g",
        type: "guardrail",
        kind,
        label: null,
        config: { check, onBlock: { message: "blocked" } },
        ports: {
          in: [{ id: "in", type: "any", required: true }],
          out: [
            { id: "pass", type: "any", required: true },
            { id: "block", type: "any", required: true },
          ],
        },
      },
      {
        id: "n_ok",
        type: "output",
        kind: "boundary",
        label: null,
        config: {},
        ports: { in: [{ id: "in", type: "any", required: true }], out: [] },
      },
      {
        id: "n_no",
        type: "output",
        kind: "boundary",
        label: null,
        config: {},
        ports: { in: [{ id: "in", type: "any", required: true }], out: [] },
      },
    ],
    edges: [
      {
        id: "e1",
        source: "n_in",
        sourceHandle: "out",
        target: "n_g",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
      {
        id: "e2",
        source: "n_g",
        sourceHandle: "pass",
        target: "n_ok",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
      {
        id: "e3",
        source: "n_g",
        sourceHandle: "block",
        target: "n_no",
        targetHandle: "in",
        channel: "data",
        condition: null,
      },
    ],
  } as IRDocument;
}

const RULE_PII = { type: "rule", rule: { kind: "pii", spec: {} } };
const MODEL_OK = { type: "model", model: { model: "judge", prompt: "in scope?", passOn: "yes" } };
const JUDGE_BINDING = { judge: { binding: "mlx", model: "judge" } };

// ── expectedKind: the shared per-instance derivation (mirrors the backend) ───────────────────────

describe("expectedKind", () => {
  it("derives a guardrail's kind from its check: model ⇒ activity, rule ⇒ orchestration", () => {
    expect(expectedKind({ type: "guardrail", config: { check: { type: "model" } } })).toBe(
      "activity",
    );
    expect(expectedKind({ type: "guardrail", config: { check: { type: "rule" } } })).toBe(
      "orchestration",
    );
  });

  it("defaults an unset/partial guardrail check to orchestration (the palette default)", () => {
    expect(expectedKind({ type: "guardrail", config: {} })).toBe("orchestration");
    expect(expectedKind({ type: "guardrail" })).toBe("orchestration");
  });

  it("delegates to the static registry kind for every non-guardrail type", () => {
    expect(expectedKind({ type: "llm" })).toBe(kindForType("llm"));
    expect(expectedKind({ type: "router" })).toBe(kindForType("router"));
    expect(expectedKind({ type: "definitely-not-a-type" })).toBeUndefined();
  });
});

// ── setGuardrailCheck: writes the check AND stamps the kind, in one edit ──────────────────────────

describe("setGuardrailCheck", () => {
  const base = graph("orchestration", null);

  it("stamps activity for a model check and orchestration for a rule check", () => {
    const model = setGuardrailCheck(base, "n_g", MODEL_OK as GuardrailCheck, { message: "x" });
    const gm = model.nodes?.find((n) => n.id === "n_g");
    expect(gm?.kind).toBe("activity");
    expect((gm?.config as { check?: { type?: string } }).check?.type).toBe("model");
    expect((gm?.config as { onBlock?: unknown }).onBlock).toEqual({ message: "x" });

    const rule = setGuardrailCheck(base, "n_g", RULE_PII as GuardrailCheck);
    expect(rule.nodes?.find((n) => n.id === "n_g")?.kind).toBe("orchestration");
  });

  it("changes the hashed content (a rule vs a model guardrail are different agents)", () => {
    const next = setGuardrailCheck(base, "n_g", MODEL_OK as GuardrailCheck);
    expect(sameHashedContent(next, base)).toBe(false);
  });

  it("is a pure function — the input IR is untouched", () => {
    setGuardrailCheck(base, "n_g", MODEL_OK as GuardrailCheck);
    expect(base.nodes?.find((n) => n.id === "n_g")?.kind).toBe("orchestration");
  });
});

// ── round-trip: a model guardrail survives IR → RF → IR as activity (the clobber regression) ─────

describe("guardrail round-trip (the reactFlowToIr clobber regression)", () => {
  it("keeps a model guardrail's activity kind through IR → RF → IR", () => {
    const ir = graph("activity", MODEL_OK, JUDGE_BINDING);
    const back = reactFlowToIr(irToReactFlow(ir), ir);
    expect(back.nodes?.find((n) => n.id === "n_g")?.kind).toBe("activity");
    expect(sameHashedContent(back, ir)).toBe(true);
  });

  it("keeps a rule guardrail's orchestration kind through IR → RF → IR", () => {
    const ir = graph("orchestration", RULE_PII);
    const back = reactFlowToIr(irToReactFlow(ir), ir);
    expect(back.nodes?.find((n) => n.id === "n_g")?.kind).toBe("orchestration");
    expect(sameHashedContent(back, ir)).toBe(true);
  });
});

// ── validateGraph: the false-reject is gone; incompleteness is caught ─────────────────────────────

const errorsOf = (ir: IRDocument) => validateGraph(ir).filter((i) => i.severity === "error");

describe("validateGraph — guardrail per-instance kind", () => {
  it("accepts a model guardrail stamped activity (no false kind error)", () => {
    const errs = errorsOf(graph("activity", MODEL_OK, JUDGE_BINDING));
    expect(errs).toHaveLength(0);
  });

  it("accepts a complete rule guardrail (orchestration)", () => {
    expect(errorsOf(graph("orchestration", RULE_PII))).toHaveLength(0);
  });

  it("flags a kind that disagrees with the check", () => {
    const errs = errorsOf(graph("orchestration", MODEL_OK, JUDGE_BINDING));
    expect(errs.some((e) => /must have kind 'activity'/.test(e.message))).toBe(true);
  });

  it("flags an incomplete model guardrail with a clear error, not a confusing 400 later", () => {
    const noModel = errorsOf(
      graph("activity", { type: "model", model: { model: "", prompt: "hi", passOn: "yes" } }),
    );
    expect(noModel.some((e) => /pick a judge model/.test(e.message))).toBe(true);

    const noPrompt = errorsOf(
      graph(
        "activity",
        { type: "model", model: { model: "judge", prompt: "", passOn: "yes" } },
        JUDGE_BINDING,
      ),
    );
    expect(noPrompt.some((e) => /judge prompt/.test(e.message))).toBe(true);

    const undeclared = errorsOf(graph("activity", MODEL_OK, {}));
    expect(undeclared.some((e) => /undeclared model 'judge'/.test(e.message))).toBe(true);
  });

  it("flags a rule check missing its rule", () => {
    const errs = errorsOf(graph("orchestration", { type: "rule" }));
    expect(errs.some((e) => /needs a rule/.test(e.message))).toBe(true);
  });
});

// ── RTL: the panel authors the check and stamps the kind ─────────────────────────────────────────

function renderInspector(ir: IRDocument, onChange: (ir: IRDocument) => void) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Inspector
        ir={ir}
        selection={{ kind: "node", id: "n_g" }}
        onChange={onChange}
        onSelect={() => {}}
      />
    </QueryClientProvider>,
  );
}

describe("GuardrailPanel", () => {
  beforeEach(() => vi.clearAllMocks());

  // The two check-type toggle buttons share text with other copy, so target them exactly.
  const checkBtn = (name: string) =>
    screen.getAllByRole("button").find((b) => b.textContent?.trim() === name) as HTMLElement;

  it("switching to a model check stamps kind=activity via onChange", () => {
    const onChange = vi.fn();
    renderInspector(graph("orchestration", RULE_PII), onChange);
    fireEvent.click(checkBtn("model"));
    expect(onChange).toHaveBeenCalled();
    const next = onChange.mock.calls[0][0] as IRDocument;
    expect(next.nodes?.find((n) => n.id === "n_g")?.kind).toBe("activity");
  });

  it("switching to a rule check stamps kind=orchestration via onChange", () => {
    const onChange = vi.fn();
    renderInspector(graph("activity", MODEL_OK, JUDGE_BINDING), onChange);
    fireEvent.click(checkBtn("rule"));
    const next = onChange.mock.calls[0][0] as IRDocument;
    expect(next.nodes?.find((n) => n.id === "n_g")?.kind).toBe("orchestration");
  });

  it("renders the rule-kind options derived from the schema (not hardcoded)", () => {
    renderInspector(graph("orchestration", RULE_PII), vi.fn());
    for (const k of ["regex", "length", "json_schema", "allow", "deny", "pii"]) {
      expect(screen.getByRole("option", { name: k })).toBeTruthy();
    }
  });

  it("shows the model form (prompt + pass on) for a model check", () => {
    renderInspector(graph("activity", MODEL_OK, JUDGE_BINDING), vi.fn());
    expect(screen.getByPlaceholderText(/Answer yes or no/)).toBeTruthy();
    // the passOn input carries "yes"; the prompt textarea carries "in scope?" — target passOn by value.
    expect(screen.getByDisplayValue("yes")).toBeTruthy();
  });

  it("picking a judge model writes check.model.model and auto-declares the binding", async () => {
    (api.listModels as ReturnType<typeof vi.fn>).mockResolvedValue([
      { logicalId: "judge-fast", binding: { binding: "mlx" } },
    ]);
    const onChange = vi.fn();
    renderInspector(
      graph("activity", { type: "model", model: { model: "", prompt: "p", passOn: "yes" } }, {}),
      onChange,
    );
    // open the judge-model picker and choose the inference model
    fireEvent.click(screen.getByText("pick a binding or inference model…"));
    fireEvent.click(await screen.findByText("judge-fast"));
    const next = onChange.mock.calls.at(-1)?.[0] as IRDocument;
    const g = next.nodes?.find((n) => n.id === "n_g");
    expect((g?.config as { check?: { model?: { model?: string } } }).check?.model?.model).toBe(
      "judge-fast",
    );
    expect((next.models as Record<string, unknown>)["judge-fast"]).toBeTruthy();
    expect(g?.kind).toBe("activity");
  });
});
