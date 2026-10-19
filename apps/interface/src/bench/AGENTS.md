# AGENTS.md — interface · `src/bench/`

The bench: test any model or agent. The model bench (opened from a Registries row) probes a
model's capabilities and renders a panel per declared modality, measuring latency and throughput.
The agent bench (opened from an Agents card) runs a pinned version and overlays its waterfall on
the canvas. Rules in [apps/interface/AGENTS.md](../../AGENTS.md) apply first; the design is in
"The bench" in [interface.md](../../../../docs/dev-docs/interface.md).

## Where it sits

- **Model traffic** goes straight to the [inference plane](../../../inference-plane/AGENTS.md)'s
  `/v1/*` through [`dataplane.ts`](dataplane.ts), the one sanctioned HTTP sibling of `lib/api.ts`.
- **Results, suites and presets** are stored on the control plane's `/bench/*`; agent runs use
  `POST /agents/{id}/runs` or `/durable-runs` and `POST /runs/{id}/resume`.

## Map

<!-- agents-map: covers=. depth=2 -->

| File | What it does |
|------|--------------|
| [`ModelBench.tsx`](ModelBench.tsx) | The per-model modal: probe capabilities, render the registered panels, record and compare results |
| [`AgentBench.tsx`](AgentBench.tsx) | The per-agent modal: pin a version, run (or run durably), resume a waiting human node, overlay the waterfall, apply a preset |
| [`registry.tsx`](registry.tsx) | The capability → panel registry (a new modality is a data addition, not an if/else) |
| [`panels/`](panels) | `ChatPanel` (chat and vision through the unified chat), `EmbeddingsPanel`, `SttPanel`, `TtsPanel` |
| [`params.ts`](params.ts), [`ParamForm.tsx`](ParamForm.tsx) | Per-modality param specs as data, narrowed by capability; the generic form that renders them |
| [`metrics.ts`](metrics.ts) | Pure math over timed samples: TTFT (on the think-split answer), tokens/s, RTF, TTFB |
| [`preset.ts`](preset.ts) | Applies a preset to an agent's binding as literal values, never a reference |
| [`shared.tsx`](shared.tsx) | Metrics readout, param hook, save result, save as preset |
| [`toolgraph.ts`](toolgraph.ts) | The throwaway one-node graph used to test a single tool through `/graphs/runs` |
| [`DetectionOverlay.tsx`](DetectionOverlay.tsx) | Boxes and masks drawn over an image (grounding VLM output or a CV tool's result) |
| [`dataplane.ts`](dataplane.ts) | Raw `/v1/*` calls: `streamChat`, `embed`, `transcribe`, `speak`, `generateImage` (also used by `chat/useInferenceChat.ts`) |

## Tests

`tests/bench.test.ts`, `bench-panels.test.tsx`, `bench-ui.test.tsx`, `agent-bench.test.tsx`.
