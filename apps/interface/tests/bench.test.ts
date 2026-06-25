// M18 bench — pure-logic + §10-guard tests (m18.md §4 frontend).
//
// Covered: benchmark math from a synthetic timed stream; capability-routed panel selection;
// schema-driven + capability-narrowed param forms; apply-preset writes LITERAL values + the preset
// NAME never lands in the IR (§1.7, the key guard); detection-overlay parsing; and the §10 guard —
// the model bench posts data-plane payloads to the INFERENCE base URL, NEVER a control-plane route.

import type { IRDocument } from "@theygent/ir-types";
import { afterEach, describe, expect, it, vi } from "vitest";
import { detectionsFromOutput, parseDetections } from "../src/bench/DetectionOverlay";
import { embed, speak, streamChat, transcribe } from "../src/bench/dataplane";
import { computeChatMetrics } from "../src/bench/metrics";
import { coerceParam, paramsForModality } from "../src/bench/params";
import { applyPresetToBinding } from "../src/bench/preset";
import { panelsFor } from "../src/bench/registry";
import { buildToolGraph } from "../src/bench/toolgraph";
import { CONTROL_PLANE_URL, INFERENCE_URL } from "../src/lib/api";

// ── benchmark math (deterministic fixture) ───────────────────────────────────

describe("computeChatMetrics", () => {
  it("computes TTFT, total, throughput, and tokens from a timed stream", () => {
    const m = computeChatMetrics([
      { atMs: 100, content: "hel" },
      { atMs: 150, content: "lo" },
      { atMs: 200, usage: { prompt_tokens: 5, completion_tokens: 8, cost: 0.001 } },
    ]);
    expect(m.ttftMs).toBe(100); // first content-bearing chunk
    expect(m.totalMs).toBe(200);
    expect(m.promptTokens).toBe(5);
    expect(m.completionTokens).toBe(8);
    expect(m.cost).toBeCloseTo(0.001);
    // throughput over the generation window (TTFT→end): 8 tok / 0.1 s = 80 tok/s
    expect(m.tokensPerSec).toBeCloseTo(80);
  });

  it("falls back to a chunk count when usage is absent", () => {
    const m = computeChatMetrics([
      { atMs: 50, content: "a" },
      { atMs: 100, content: "b" },
    ]);
    expect(m.completionTokens).toBe(2);
  });
});

// ── capability-routed panel selection (the §1.2 seam) ────────────────────────

describe("panelsFor (data-driven, no hardcoded modality switch)", () => {
  it("routes a chat+vision model to the chat panel only (vision rides chat)", () => {
    expect(panelsFor(["chat", "vision"])).toEqual(["chat"]);
  });
  it("routes an audio.transcription model to the STT panel", () => {
    expect(panelsFor(["audio.transcription"])).toEqual(["audio.transcription"]);
  });
  it("routes a multi-modality model to every matching panel, in registry order", () => {
    expect(panelsFor(["audio.speech", "embeddings", "chat"])).toEqual([
      "chat",
      "embeddings",
      "audio.speech",
    ]);
  });
  it("returns no panels for an unknown modality", () => {
    expect(panelsFor(["telepathy"])).toEqual([]);
  });
});

// ── schema-driven, capability-narrowed param form ───────────────────────────

describe("paramsForModality", () => {
  it("hides capability-gated params a model does not advertise", () => {
    const without = paramsForModality("chat", { toolCalling: false, structuredOutput: false });
    const keys = without.map((p) => p.key);
    expect(keys).toContain("temperature"); // unconditional generation param
    expect(keys).not.toContain("response_format"); // gated on structuredOutput
    expect(keys).not.toContain("tool_choice"); // gated on toolCalling
  });
  it("shows them when the capability is advertised", () => {
    const withCaps = paramsForModality("chat", { toolCalling: true, structuredOutput: true });
    const keys = withCaps.map((p) => p.key);
    expect(keys).toContain("response_format");
    expect(keys).toContain("tool_choice");
  });
  it("emits a valid per-modality param object via coerceParam", () => {
    const specs = paramsForModality("chat", {});
    const temp = specs.find((s) => s.key === "temperature");
    expect(temp && coerceParam(temp, "0.2")).toBe(0.2);
    const stop = specs.find((s) => s.key === "stop");
    expect(stop && coerceParam(stop, "END, STOP")).toEqual(["END", "STOP"]);
  });
});

// ── apply-preset → literal values, NO preset name in the IR (§1.7) ───────────

const AGENT_IR = {
  schemaVersion: "1",
  id: "a1",
  name: "x",
  version: "1.0.0",
  models: { fast: { binding: "mlx", model: "m", params: { temperature: 0.9 } } },
  nodes: [],
  edges: [],
} as unknown as IRDocument;

describe("applyPresetToBinding (the §1.7 guard)", () => {
  it("copies LITERAL values into models[binding].params and never writes the preset name", () => {
    const next = applyPresetToBinding(AGENT_IR, "fast", { temperature: 0.0, top_p: 1 });
    const models = next.models as Record<string, { params: Record<string, unknown> }>;
    expect(models.fast.params.temperature).toBe(0.0); // preset value wins
    expect(models.fast.params.top_p).toBe(1);
    // The whole serialized IR must contain NO preset name/reference — only literal values.
    const json = JSON.stringify(next);
    expect(json).not.toContain("paramsPreset");
    expect(json).not.toContain("preset");
  });
  it("does not mutate the input IR (returns a new document)", () => {
    const before = JSON.stringify(AGENT_IR);
    applyPresetToBinding(AGENT_IR, "fast", { temperature: 0.0 });
    expect(JSON.stringify(AGENT_IR)).toBe(before);
  });
  it("throws loudly on an unknown binding (never silently creates one)", () => {
    expect(() => applyPresetToBinding(AGENT_IR, "nope", { temperature: 0 })).toThrow(
      /no model binding/,
    );
  });
});

// ── detection overlay parsing ────────────────────────────────────────────────

describe("parseDetections", () => {
  it("parses boxes from a detections payload (CV tool or VLM grounding)", () => {
    const dets = parseDetections({
      detections: [
        { bbox: [10, 20, 30, 40], label: "cat", score: 0.9 },
        { box: [1, 2, 3, 4] },
        { label: "no box" },
      ],
    });
    expect(dets).toHaveLength(2);
    expect(dets[0]).toEqual({ box: [10, 20, 30, 40], label: "cat", score: 0.9 });
  });

  it("parses a JSON-serialized run OUTPUT string (the tool tester / VLM grounding path)", () => {
    // The run output crosses the wire JSON-serialized (control-plane `_coerce_output`).
    const out = JSON.stringify({ detections: [{ box: [0.1, 0.2, 0.3, 0.4], label: "dog" }] });
    expect(detectionsFromOutput(out)).toEqual([{ box: [0.1, 0.2, 0.3, 0.4], label: "dog" }]);
  });

  it("returns [] for a plain-text answer or empty output (never throws)", () => {
    expect(detectionsFromOutput("just a sentence, no boxes")).toEqual([]);
    expect(detectionsFromOutput("")).toEqual([]);
    expect(detectionsFromOutput(null)).toEqual([]);
  });
});

// ── tool tester: the throwaway input→mcp_tool→output graph (§2.6) ─────────────

describe("buildToolGraph (the §2.6 throwaway one-node graph)", () => {
  it("composes a valid input → mcp_tool → output IR wired with data edges", () => {
    const ir = buildToolGraph({ server: "yolo", tool: "detect", argNames: ["image"] });

    // Envelope present (the §8.2 required fields) so `/graphs/runs` accepts it inline.
    expect(ir.id).toBeTruthy();
    expect(ir.name).toBeTruthy();
    expect(ir.version).toBeTruthy();
    expect(ir.schemaVersion).toBeTruthy();

    // Exactly the three nodes, in order, with the right (type, kind) — boundary/activity/boundary.
    const nodes = ir.nodes ?? [];
    expect(nodes.map((n) => [n.type, n.kind])).toEqual([
      ["input", "boundary"],
      ["mcp_tool", "activity"],
      ["output", "boundary"],
    ]);

    // The mcp_tool node carries {server, tool, args} with args templated from $in.in.<name> (§8.5).
    const toolNode = nodes.find((n) => n.type === "mcp_tool");
    expect(toolNode?.config).toEqual({
      server: "yolo",
      tool: "detect",
      args: { image: "$in.in.image" },
    });

    // Edges wire input.out → mcp_tool.in → output.in, all `data` (the value path the walker runs).
    const inNode = nodes.find((n) => n.type === "input");
    const outNode = nodes.find((n) => n.type === "output");
    expect(ir.edges).toEqual([
      expect.objectContaining({
        source: inNode?.id,
        sourceHandle: "out",
        target: toolNode?.id,
        targetHandle: "in",
        channel: "data",
      }),
      expect.objectContaining({
        source: toolNode?.id,
        sourceHandle: "out",
        target: outNode?.id,
        targetHandle: "in",
        channel: "data",
      }),
    ]);

    // Every required in-port is fed by an inbound data edge (so the graph passes validate_graph).
    const fedPorts = new Set((ir.edges ?? []).map((e) => `${e.target}:${e.targetHandle}`));
    for (const node of nodes) {
      for (const port of node.ports?.in ?? []) {
        if (port.required !== false) expect(fedPorts.has(`${node.id}:${port.id}`)).toBe(true);
      }
    }
  });

  it("templates one $in.in.<name> arg per declared input field", () => {
    const ir = buildToolGraph({ server: "sam", tool: "segment", argNames: ["image", "prompt"] });
    const toolNode = (ir.nodes ?? []).find((n) => n.type === "mcp_tool");
    expect(toolNode?.config).toMatchObject({
      args: { image: "$in.in.image", prompt: "$in.in.prompt" },
    });
  });
});

// ── §10 guard — data-plane payloads go to the inference URL, never control-plane ─

describe("§10 guard: model bench data-plane → inference base URL only", () => {
  afterEach(() => vi.unstubAllGlobals());

  function fakeFetch(calls: string[]) {
    const enc = new TextEncoder();
    return vi.fn(async (url: string | URL, init?: RequestInit) => {
      calls.push(String(url));
      const method = init?.method ?? "GET";
      void method;
      const body = new ReadableStream<Uint8Array>({
        start(c) {
          c.enqueue(enc.encode('data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'));
          c.enqueue(enc.encode("data: [DONE]\n\n"));
          c.close();
        },
      });
      return {
        ok: true,
        status: 200,
        body,
        headers: { get: () => "audio/mpeg" },
        json: async () => ({ data: [{ embedding: [0.1], index: 0 }], text: "t" }),
      } as unknown as Response;
    });
  }

  it("routes chat / embeddings / STT / TTS to INFERENCE_URL and not CONTROL_PLANE_URL", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", fakeFetch(calls));

    for await (const _ of streamChat("m", [{ role: "user", content: "hi" }], {})) {
      /* drain */
    }
    await embed("m", "hi", {});
    await transcribe("m", new Blob(["x"]), {});
    await speak("m", "hi", {});

    expect(calls.length).toBeGreaterThanOrEqual(4);
    for (const url of calls) {
      expect(url.startsWith(INFERENCE_URL)).toBe(true);
      expect(url.startsWith(CONTROL_PLANE_URL)).toBe(false);
    }
  });
});
