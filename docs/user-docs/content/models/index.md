# Registries

**Registries** is where you manage the models your agents and chats can call. It lists every model registered in your inference plane, lets you probe what each one can do, and gives you a browse-and-install catalog for pulling new models from Hugging Face — all without a model's weights ever passing through the control plane. Open it from the left sidebar, under **Configuration → Registries**.

This page is a tour of the whole surface. For the step-by-step procedures — installing from the catalog, registering a model already on disk, and deleting one — see [Installing local models](installing.md). For pointing an id at a hosted or remote API, see [Remote models](remote.md).

## What Registries talks to

The Registries page reads and writes your **inference plane** directly from the browser (by default `http://localhost:8081`). Everything you see here — the registered models, the Hugging Face catalog, download jobs, and local credentials — lives on your machine. The control plane (agents, runs, sessions) is never involved in registering a model or downloading weights.

Every model is registered under a **logical id** — a short name you choose, like `triage-fast` — and your agents reference that id, never an engine. If this idea is new, read [Models and engines](../concepts/models-and-engines.md) first; the rest of this page assumes it.

## Your registered models

The header shows **Resident Models: N / \<max\>** — how many engines are currently loaded in memory versus the ceiling (two by default). Below it, the **Installed** table lists every registered model:

| Column | What it shows |
|---|---|
| **Logical id** | The name agents and chat use to reach this model. |
| **Engine** | The binding, shown as a friendly label: `MLX`, `llama.cpp`, `vLLM`, or the raw value for a reachable endpoint (`openai-compatible`). |
| **Model** | The weights or upstream model name. A local-path model shows only the file or directory name; hover for the full path. |
| **State** | `resident` (loaded in memory now) or `cold` (registered but not loaded). |
| **Capabilities** | Capability badges, once probed (see below). |
| **Actions** | Bench, Warm, Evict, Delete. |

The engine and state badges are clickable **filters**, and a search box narrows the list by logical id or model name.

### Reading capabilities

A registered model's capabilities aren't known until the engine has loaded it, so the **Capabilities** cell starts empty. Click the search-icon **Probe capabilities** button on a row to find out — this loads the model into its engine (it warms the engine) and reads back what it supports. The badges you may see:

| Badge | Meaning |
|---|---|
| `reasoning` | The model produces a separate "thinking" stream. |
| `tools` | It supports tool/function calling. |
| `structured` | It can return structured JSON output. |
| `vision` | It accepts images as input. |
| `<N>k ctx` | Its maximum context window. |
| `approx` | The report is an approximation read from model metadata rather than the live server. |

If nothing is reported, the cell reads **none reported**. Reachable (`openai-compatible`) endpoints are never probed — they advertise only the modality you declared when you registered them.

### Row actions

Each row carries four actions:

- **Bench** — opens the model bench in a modal, a tester panel per modality with live metrics. See [The Bench](../running/bench.md).
- **Warm** — loads the model into its engine now, so the next call skips the cold-start wait.
- **Evict** — unloads the model from memory (freeing it for other engines). A model with a request in flight is drained rather than killed.
- **Delete** — unregisters the model. This asks for confirmation and warns that any agent referencing the id will fail; it cannot be undone. Covered in [Installing local models](installing.md#deleting-a-model).

Clicking a row (anywhere but its buttons) opens the model's registration — a catalog detail for Hugging Face installs, or an editable settings modal for everything else.

## Adding a model

The **Add model** button opens a dialog with two tabs:

- **Hugging Face** — paste a model id or URL, or open the full browse dialog to search the catalog. This is the path for pulling a new open model to your machine.
- **Hosted / local** — register a model manually: a hosted or remote OpenAI-compatible endpoint, or weights already sitting on your disk.

Both flows end with you choosing a logical id. The procedures are in [Installing local models](installing.md) (catalog and on-disk) and [Remote models](remote.md) (hosted endpoints).

## Browsing the Hugging Face catalog

From **Add model → Browse Hugging Face →** you get a searchable catalog of open models. One rule shapes everything you see here: **discovery only shows models you can actually run.** The listing is filtered to the engines that are ready on your machine, so a model you have no engine for never appears. If no local engine is ready at all, the dialog tells you to install one first.

The controls:

- **Search** — free text, e.g. `qwen`, `llama`, `phi`.
- **Size** — Any / Small (<3B) / Medium (3–15B) / Large (>15B), by parameter count.
- **Sort** — Trending (default) / Most downloaded / Most liked.
- **Engines** — chips that limit the listing to specific ready engines (at least one is always active).
- **Task chips** — single-select, applied on the server: Chat, Vision, Image generation, Embeddings, Speech-to-text, Text-to-speech.
- **Capability chips** — multi-select, applied to the loaded page: Reasoning, Tools.

Scrolling grows the page up to 100 entries.

### Reading a catalog card

Each card summarizes a model before you download anything:

- Title, repository reference, and parameter count (e.g. "7B").
- A **✓ installed** badge if you've already registered it.
- A lock and **gated** marker — "needs a Hugging Face token" — for repositories that require authentication.
- License, a coarse "updated" time, download and star counts, and a **View on Hugging Face ↗** link.
- A **task badge** (vision, embeddings, speech-to-text, text-to-speech, image generation).
- **Capability badges at browse time** — reasoning, tools, vision, and context window, derived from the model's Hugging Face metadata *without downloading it*. These carry an **approx** marker: "static hint from model metadata — the probe confirms it on install." Because the browse hint and the post-install probe use the same signals, they won't contradict each other.

### Variants and fit

Expand a card to see its **variants** — the concrete things you can install. For a llama.cpp model these are the individual GGUF quantizations (labels like `Q4_K_M` with a quality note); for an MLX model it's the whole repo at its precision (e.g. "4-bit"). One variant is marked **recommended** (the largest that fits your machine, or the smallest if none do).

Every variant carries a **fit badge** sized against your machine's RAM, so you can tell before downloading whether it will run:

| Fit badge | Meaning |
|---|---|
| **fits** (green) | Comfortably within memory. |
| **tight** (amber) | Will load but leaves little headroom. |
| **too large** (red) | Likely exceeds your memory — it may fail to load or run slowly. |
| **size unknown** (slate) | The size couldn't be determined. |

Hover a badge for the reasoning, e.g. "~5 GB needed · 16 GB RAM". Picking a too-large variant is allowed but warns you first.

### Download progress

When you install, the download runs **inside your inference plane** — TheYgent never sees the bytes. Progress appears as a card in the notification center (bottom-right): a progress bar, bytes done and total, live speed, an ETA, and a **Cancel** button. It moves through `downloading → registering → done`, survives navigation and page reloads (it re-attaches to active jobs), and finishes with "Installed ✓". On completion the model is auto-registered under the logical id you chose and appears in the Installed table.

The full install walkthrough — including the install dialog and choosing a logical id — is on the next page.

## Moving models to another machine

The **Model registry** section of **Settings → [Import / Export](../import-export/index.md)** carries your registrations between installs. A registry export contains the logical-id → binding map plus, for catalog installs, the Hugging Face repo and variant each one came from — **never the weights themselves, and never credential values** (credential names travel so the target knows what to ask you for).

On import:

- **Catalog-installed models re-download automatically.** The target's inference plane pulls the same repo and variant from Hugging Face — with the exported registration preserved (modality, parameters, lifecycle), only the weights path pointing at the new machine. Machine-specific auxiliary paths in the parameters (such as a vision projector file) are dropped with a `params_path_dropped` warning and re-located next to the new weights automatically. Progress shows in the notification center like any install.
- **Hosted and remote registrations just work** — an `openai-compatible` binding is a URL plus a credential name; re-enter the credential on the target and it's live.
- **Hand-registered local paths can't follow you.** A model registered against a file path on the old machine (with no catalog provenance) is registered anyway but flagged `weights_unavailable` — it fails at first use until you put weights at that path or reinstall it.
- **Already-registered logical ids are skipped**, never overwritten.

## Related pages

- [Installing local models](installing.md) — the step-by-step install, on-disk registration, and deleting models
- [Remote models](remote.md) — registering a hosted or remote OpenAI-compatible endpoint
- [The engines](engines.md) — llama.cpp, MLX, and vLLM, and which one runs on your machine
- [Models and engines](../concepts/models-and-engines.md) — logical ids, bindings, sources, and modalities
- [Import & export](../import-export/index.md) — moving the registry (and everything else) between machines
- [The Bench](../running/bench.md) — testing and comparing models
