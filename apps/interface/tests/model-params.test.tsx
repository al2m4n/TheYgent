// The editor's model-parameters surface: the spec↔wire coercions (incl. the reasoning toggle and
// the speak node's `format` vocabulary), the "?" help affordance, and the params panel writing
// LITERAL values — into a model binding's `params` from the llm inspector, and via the bench-preset
// loader (a copy of values; the preset name never lands in the IR).

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { IRDocument } from "@theygent/ir-types";
import { type ReactNode, useState } from "react";
import { describe, expect, it, vi } from "vitest";

vi.mock("../src/lib/api", () => ({
  ApiError: class ApiError extends Error {
    code?: string;
  },
  CONTROL_PLANE_URL: "http://cp.test",
  INFERENCE_URL: "http://inf.test",
  api: {
    getModelCapabilities: vi.fn(async () => ({ reasoning: true, toolCalling: false })),
    listPresets: vi.fn(async () => [
      {
        id: "p1",
        name: "fast",
        modality: "chat",
        logical_id: "local-fast",
        params: { temperature: 0.1, max_tokens: 512 },
        created_at: "",
        updated_at: "",
      },
    ]),
    listModels: vi.fn(async () => []),
    listMcpServers: vi.fn(async () => []),
    listConnections: vi.fn(async () => []),
    listTriggers: vi.fn(async () => []),
    listAgents: vi.fn(async () => []),
  },
}));

import {
  coerceParam,
  narrowSpecs,
  presetParamsForNode,
  rawFromParam,
  specsForNodeParams,
} from "../src/bench/params";
import { Inspector } from "../src/components/Inspector";
import { ModelParamsSection } from "../src/components/ModelParamsPanel";
import { sampleGraph } from "./fixtures";

function renderWithClient(ui: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

// ── the spec↔wire coercions ───────────────────────────────────────────────────

describe("reasoning toggle + raw/wire round-trip", () => {
  const chatSpecs = specsForNodeParams("chat");
  const reasoning = chatSpecs.find((s) => s.key === "chat_template_kwargs");

  it("coerces on/off to the chat-template switch and back", () => {
    if (!reasoning) throw new Error("chat specs must include the reasoning toggle");
    expect(coerceParam(reasoning, "off")).toEqual({ enable_thinking: false });
    expect(coerceParam(reasoning, "on")).toEqual({ enable_thinking: true });
    expect(coerceParam(reasoning, "")).toBeUndefined();
    expect(rawFromParam(reasoning, { enable_thinking: false })).toBe("off");
    expect(rawFromParam(reasoning, { enable_thinking: true })).toBe("on");
    expect(rawFromParam(reasoning, undefined)).toBe("");
  });

  it("is ALWAYS offered (capability probes are approximate — gating would hide the switch on the models that need it)", () => {
    const keys = narrowSpecs(chatSpecs, { reasoning: false, toolCalling: false }).map((s) => s.key);
    expect(keys).toContain("chat_template_kwargs");
    expect(keys).toContain("reasoning_effort");
    expect(keys).not.toContain("tool_choice"); // real capability gates still narrow
  });

  it("reasoning effort passes through as the standard low/medium/high string", () => {
    const effort = chatSpecs.find((s) => s.key === "reasoning_effort");
    if (!effort) throw new Error("chat specs must include reasoning_effort");
    expect(coerceParam(effort, "low")).toBe("low");
    expect(rawFromParam(effort, "low")).toBe("low");
    expect(coerceParam(effort, "")).toBeUndefined();
  });

  it("round-trips the other special shapes (stop list, structured output)", () => {
    const stop = chatSpecs.find((s) => s.key === "stop");
    const rf = chatSpecs.find((s) => s.key === "response_format");
    if (!stop || !rf) throw new Error("chat specs must include stop + response_format");
    expect(rawFromParam(stop, coerceParam(stop, "END, STOP"))).toBe("END, STOP");
    expect(rawFromParam(rf, coerceParam(rf, "json_object"))).toBe("json_object");
  });

  it("every spec has help text behind the ? affordance", () => {
    for (const modality of ["chat", "embeddings", "audio.transcription", "audio.speech"] as const)
      for (const spec of specsForNodeParams(modality)) expect(spec.help).toBeTruthy();
  });
});

describe("speak node params vocabulary", () => {
  it("names the audio container `format` (the key the runtime reads)", () => {
    const keys = specsForNodeParams("audio.speech").map((s) => s.key);
    expect(keys).toContain("format");
    expect(keys).not.toContain("response_format");
  });

  it("remaps a bench TTS preset's response_format onto format", () => {
    expect(
      presetParamsForNode("audio.speech", { voice: "af_heart", response_format: "wav" }),
    ).toEqual({ voice: "af_heart", format: "wav" });
    // Other modalities copy through untouched.
    expect(presetParamsForNode("chat", { temperature: 0.2 })).toEqual({ temperature: 0.2 });
  });
});

// ── the params panel (editing + preset load) ─────────────────────────────────

describe("ModelParamsSection", () => {
  it("edits emit literal wire params; clearing a field removes it", async () => {
    // Controlled harness — the real host (the Inspector) re-renders with the updated IR on
    // every change, so the panel must see its own emissions reflected back.
    let latest: Record<string, unknown> = { max_tokens: 256 };
    function Harness() {
      const [params, setParams] = useState<Record<string, unknown>>(latest);
      return (
        <ModelParamsSection
          modality="chat"
          logicalId="local-fast"
          params={params}
          onChange={(p) => {
            latest = p;
            setParams(p);
          }}
        />
      );
    }
    renderWithClient(<Harness />);
    // seeded from the stored params
    expect(screen.getByRole("spinbutton", { name: "Max tokens" })).toHaveValue(256);

    fireEvent.change(screen.getByRole("spinbutton", { name: "Temperature" }), {
      target: { value: "0.7" },
    });
    expect(latest).toEqual({ max_tokens: 256, temperature: 0.7 });

    fireEvent.change(screen.getByRole("spinbutton", { name: "Max tokens" }), {
      target: { value: "" },
    });
    expect(latest).toEqual({ temperature: 0.7 });
    expect("max_tokens" in latest).toBe(false);
  });

  it("shows the reasoning toggle when the model advertises it, and writes the switch", async () => {
    let latest: Record<string, unknown> = {};
    renderWithClient(
      <ModelParamsSection
        modality="chat"
        logicalId="local-fast"
        params={latest}
        onChange={(p) => {
          latest = p;
        }}
      />,
    );
    const toggle = await screen.findByRole("combobox", { name: "Reasoning" });
    fireEvent.change(toggle, { target: { value: "off" } });
    expect(latest).toEqual({ chat_template_kwargs: { enable_thinking: false } });
  });

  it("loads a saved preset as a literal copy of its values", async () => {
    let latest: Record<string, unknown> = { seed: 7 };
    renderWithClient(
      <ModelParamsSection
        modality="chat"
        logicalId="local-fast"
        params={latest}
        onChange={(p) => {
          latest = p;
        }}
      />,
    );
    const picker = await screen.findByRole("combobox", { name: "Preset" });
    fireEvent.change(picker, { target: { value: "p1" } });
    fireEvent.click(screen.getByRole("button", { name: "Load" }));
    // preset values copied over the current dict (preset wins; untouched keys survive)
    expect(latest).toEqual({ seed: 7, temperature: 0.1, max_tokens: 512 });
    // and the form re-seeded from the merged values
    expect(screen.getByRole("spinbutton", { name: "Max tokens" })).toHaveValue(512);
  });

  it("renders a ? help affordance per field", () => {
    renderWithClient(<ModelParamsSection modality="chat" params={{}} onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "What does Temperature do?" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "What does Max tokens do?" })).toBeInTheDocument();
  });
});

// ── the Inspector wiring (llm binding params; speak params form) ─────────────

describe("Inspector model params", () => {
  it("llm: editing a param writes into ir.models[binding].params (hashed content)", async () => {
    const ir = sampleGraph();
    let next: IRDocument | null = null;
    renderWithClient(
      <Inspector
        ir={ir}
        selection={{ kind: "node", id: "n_llm" }}
        onChange={(x) => {
          next = x;
        }}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("Model parameters")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("spinbutton", { name: "Temperature" }), {
      target: { value: "0.3" },
    });
    const models = (next as unknown as IRDocument)?.models as Record<
      string,
      { params?: Record<string, unknown> }
    >;
    expect(models.default.params).toEqual({ temperature: 0.3 });
    // node config untouched — params live on the binding.
    const llm = (next as unknown as IRDocument)?.nodes?.find((n) => n.id === "n_llm");
    expect((llm?.config as Record<string, unknown>).model).toBe("default");
  });

  it("speak: the params form writes config.params with the node vocabulary (format)", async () => {
    const ir = sampleGraph();
    ir.models = {
      ...ir.models,
      tts: { binding: "mlx", model: "local-tts", source: null, params: {} },
    } as IRDocument["models"];
    ir.nodes = [
      ...(ir.nodes ?? []),
      {
        id: "n_speak",
        type: "speak",
        kind: "activity",
        label: null,
        config: { model: "tts", params: {} },
        ports: {
          in: [{ id: "in", type: "any", required: true }],
          out: [{ id: "out", type: "any", required: true }],
        },
      },
    ] as IRDocument["nodes"];
    let next: IRDocument | null = null;
    renderWithClient(
      <Inspector
        ir={ir}
        selection={{ kind: "node", id: "n_speak" }}
        onChange={(x) => {
          next = x;
        }}
        onSelect={() => {}}
      />,
    );
    expect(screen.getByText("Model parameters")).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Format" }), {
      target: { value: "wav" },
    });
    const speak = (next as unknown as IRDocument)?.nodes?.find((n) => n.id === "n_speak");
    expect((speak?.config as Record<string, unknown>).params).toEqual({ format: "wav" });
    await waitFor(() => {}); // let the caps/preset queries settle before unmount
  });
});
