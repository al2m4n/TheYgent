# The Bench

The Bench is where you test and measure things before you wire them into an agent. Point it at a model to see how fast it answers, how it handles a prompt, and what it costs; point it at a saved agent to run it with a real input and watch every step. It is the same tooling for a quick "does this work" check and a careful side-by-side comparison.

There are two benches — one for models, one for agents — plus a small tester for MCP tools. All three share the same measurement and trace machinery.

## What you can bench

- **Models** — any registered model, across its modalities: chat and vision, embeddings, speech-to-text, and text-to-speech. Model testing goes **straight to your inference plane's data endpoints** — the raw prompt, image, or audio never passes through the control plane. Only the measured metrics are recorded afterwards.
- **Agents** — any [saved agent](../building/saving-agents.md), run through the normal run path so you exercise the whole graph exactly as production would.
- **MCP tools** — a single registered tool, run standalone to check its shape and output.

## Benching a model

Open the [Registries](../models/index.md) page and press the **Bench** button on any model row (titled "Test & benchmark this model"). A modal opens titled `Bench · <logical id>`.

At the top, the model's capabilities are probed and shown as badges — its modalities, its maximum context, and an "approximate" marker when the probe is a static hint rather than a confirmed value. A **Warm** button preloads the engine so your first timing isn't skewed by the model loading; a warm that fails surfaces as an error rather than a mysterious slow run.

The panel you get below is chosen by the model's declared modalities:

| Modality | Panel | What you do |
|---|---|---|
| `chat` | Chat | A real multi-turn conversation with an optional system prompt. Vision rides here — image attach turns on when the model advertises vision. |
| `embeddings` | Embeddings | Embed one input, or two to see their cosine similarity. |
| `audio.transcription` | Speech-to-text | Record from the mic or attach an audio file; the transcript comes back as the reply. |
| `audio.speech` | Text-to-speech | Type text; the reply is a playable audio bubble. |

Image-generation models have no bench panel of their own — you exercise them from [New Chat](../chat/vision-and-images.md) instead.

### Tuning parameters

Each panel has a parameter form that is **schema-driven and capability-narrowed**: a parameter that needs a capability the model doesn't advertise is hidden, not greyed out, and every field has a `?` with help text. The chat form covers `temperature`, `top_p`, `max_tokens` (placeholder `2048`), `stop`, `presence_penalty`, `frequency_penalty`, `seed`, a **Reasoning** toggle, `reasoning_effort` (low/medium/high), `tool_choice`, and `response_format`. Embeddings, speech-to-text, and text-to-speech each have their own narrower set.

These are the same specs the [editor](../building/editor.md) uses to tune a node — so what you dial in on the Bench is exactly what you can set on a node or binding later.

### Reading the metrics

Every exchange is measured and the numbers show as badges. Which ones you see depend on the modality:

| Metric | Label | Meaning |
|---|---|---|
| `ttftMs` | TTFT | Time to the first visible answer token (thinking does not count — inline reasoning can't fake an early TTFT). |
| `totalMs` | Total | End-to-end time. |
| `tokensPerSec` | Throughput | Tokens per second over the post-TTFT generation window. |
| `promptTokens` / `completionTokens` | — | Token counts (completion falls back to a chunk count, clearly approximate, if the gateway reports no usage). |
| `cost` | Cost | USD, shown only when the gateway prices the call — absent for local engines. |
| `rtf` | Real-time factor | Speech-to-text: audio seconds ÷ processing seconds. |
| `ttfbMs` / `charsPerSec` | TTFB / Throughput | Text-to-speech: time to the first audio byte, and characters per second. |
| `latencyMs` | — | Image generation. |

### Saving, comparing, and presets

Three actions turn a one-off test into something reusable:

- **Save result** records this exchange's metrics to the control plane's bench store (`POST /bench/runs`). The server computes a `params_digest` (so two runs that differ by one parameter are distinct results) and an `output_digest` (a content fingerprint used for comparison **without** storing the raw output). The raw output is kept only if you opt in, and then only as a **local reference** on your own machine — never uploaded as a blob.
- **Compare** appears once you've saved two or more results in the modal. Pick an A and a B and you get per-metric deltas (B − A) and whether the outputs matched (digest equality — no payloads compared). Deltas are colored by direction: for throughput and real-time factor, higher is better; for latency and the rest, lower is better; zero is neutral. The compare list holds only results you recorded in the current modal session.
- **Save as preset** stores the current parameters as a named, modality-scoped snippet (`POST /bench/presets`). A preset is literal parameter values only — the name is never stored inside a graph, so it can't drift an agent's content hash. You can apply a preset to an agent later (below).

All of a model bench's raw traffic — `/v1/chat/completions`, `/v1/embeddings`, `/v1/audio/transcriptions`, `/v1/audio/speech`, `/v1/images/generations` — goes directly to your inference plane. The control plane only ever sees the metrics and digests you save.

## Benching an agent

Open the Agents page (the home grid of your [saved agents](../building/saving-agents.md)) and press **Run** on an agent card — disabled until the agent has at least one saved version. The agent bench modal opens. (The **Bench** button appears on *model* rows in [Registries](../models/index.md); an agent card uses **Run**.)

Pick a **Version** (it defaults to and follows the latest until you pin one), then enter an **Input** in **Text** or **JSON** mode — JSON is parsed loudly, so a malformed object tells you rather than failing silently. A multi-input agent takes an object you address with `$in.in.<field>` (see [referencing inputs](../building/input-references.md)).

### Run vs Run durably

The **Run** control is a split button:

- **Run** launches a normal streaming run (`POST /agents/{id}/runs`). Reasoning arrives on its own channel and the answer streams token by token, exactly like chat.
- **Run durably** launches a checkpointed run on the [durable runtime](durable.md) (`POST /agents/{id}/durable-runs`). It returns a run id and the modal polls for the result — there is no token streaming, because a durable run journals results rather than streaming them. Use this to get crash-resumability.

Agents that contain `human`, `subgraph`, `loop`, or `map` nodes are **durable-only** — they can run only on the durable runtime, so the modal shows a single **Run durably** button for them.

!!! warning "Durable mode must be enabled"
    If the control plane isn't running in durable mode, a durable run returns a `durable_required` error and the modal shows: "The control-plane isn't running in durable mode — start it with `THEYGENT_DURABLE=1` to run durably." See [durable runs](durable.md).

### Reading an agent bench result

Once the stream ends, the canonical output is re-read from the persisted run row (a graph that ends at a tool or router emits no tokens — the stored output is what counts). The answer renders like a chat reply: a collapsible thinking block above a markdown bubble. A **Stop** button aborts a streaming run mid-flight, and leaving the modal aborts it too.

Below the answer is the live [run waterfall](observability.md) alongside a read-only mini canvas of the pinned graph — hovering a node row in the waterfall flashes the matching node on the canvas, so you can see where the time went at a glance. If a durable run pauses at a human gate it shows a resume panel right there; the run buttons stay usable and the paused run remains visible under [Runs](index.md).

For agents that aren't durable-only, a **Chat** section lets you hold a real conversation with the agent — turns share session memory and record as a session under Recents, just like [chat](../chat/index.md).

### Applying a preset to an agent

If you saved parameter presets from a model bench, the agent bench offers **Apply a preset**: pick a saved preset and one of the agent's model bindings, and TheYgent writes the preset's literal parameters into that binding and saves a **new agent version** through the registry. The preset is modality-matched, so a chat preset can't land on a text-to-speech binding, and the preset name never enters the graph — only its values do, so the new version's [content hash](../concepts/versioning.md) reflects real content.

## Testing an MCP tool

The [MCP](../building/nodes/mcp.md) page has a **tool tester** for exercising one registered tool in isolation. Pick a connected server, name a tool, and send it either an uploaded image (under a configurable argument name) or free-form JSON arguments. It composes a throwaway `input → mcp_tool → output` graph and runs it through the normal run path — no special execution route — so what you see is exactly what an agent would get. Structured detections (for example bounding boxes from a vision tool) draw over the image; raw output shows below.

## Where results are stored

Saved model-bench results, presets, and any test suites live in the control plane's Postgres database (the bench tables), alongside your runs and agents. What is stored is deliberately lightweight:

- **Metrics and digests**, not raw outputs. The `output_digest` lets comparisons check whether two outputs matched without keeping the text.
- **Raw output only on opt-in**, and then only as a **local reference** in your own trust domain — TheYgent never uploads the generated text, image, or audio.
- Bench records carry the coordinates you'd expect for later analysis: `target_kind` (`model` or `agent`), `modality`, `logical_id`, `binding`, the `params` and their digest, the `metrics`, the `output_digest`, and a `run_id`.

Saved results are readable through the API (`GET /bench/runs`, filterable by `logical_id`, `agent_id`, `suite_id`, or `case_id`) and comparable in-modal. Suites of golden cases and assertions exist in the API for future use, but there is no page for them yet — recording results, in-modal compare, and presets are what the interface surfaces today.

## Works well with

- [Installing local models](../models/installing.md) — get a model into your registry so it has a Bench button.
- [Remote models](../models/remote.md) — register a hosted, OpenAI-compatible model to bench (and to see a cost figure).
- [Observability](observability.md) — the run waterfall the agent bench embeds.
- [Durable runs](durable.md) — what "Run durably" does and why some agents require it.
- [Saving agents](../building/saving-agents.md) — creating the versions the agent bench runs and the presets it applies.
