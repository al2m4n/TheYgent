# AGENTS.md — inference plane

`apps/inference-plane` (`theygent_inference_plane`) runs models. It is one FastAPI service on
:8081, in the user's trust domain, with two surfaces: an OpenAI-compatible data plane (`/v1/*`)
addressed by logical model id, and a TheYgent-native management plane (`/admin/*`) for the model
registry, engine lifecycle, the Hugging Face catalog and downloads, credentials, settings and
diagnostics. It spawns and supervises local engine servers (llama.cpp, MLX, vLLM) per
`(engine, modality)` and proxies `openai-compatible` bindings straight to their URL. The
repo-wide rules in the root [AGENTS.md](../../AGENTS.md) apply first; the design is in
[inference-plane.md](../../docs/dev-docs/inference-plane.md), which also carries the long-form
file layout.

## Where it sits

- **Called by:** the [control plane](../control-plane/AGENTS.md) and [worker](../worker/AGENTS.md)
  through [packages/gateway-client](../../packages/gateway-client/AGENTS.md) (`/v1/*` only);
  the [interface](../interface/AGENTS.md) directly (`/v1/*` for chat, bench and media; `/admin/*` for
  Registries, Settings → Inference and import/export); the [AI gateway](../ai-gateway/AGENTS.md)'s
  local route (`/v1/chat/completions`); the [MCP server](../mcp-server/AGENTS.md)'s `ask_model`.
- **Calls:** the engine processes it spawns on `127.0.0.1` ports; hosted OpenAI-compatible APIs
  the user registered; the Hugging Face Hub (catalog and downloads).
- **State (local files, never Postgres):** under `THEYGENT_INFERENCE_PLANE_STATE_DIR`
  (default `~/.theygent/inference`): `registry.json`, `installs.json`, `credentials.json`,
  `settings.json`, `logs/`; weights under `THEYGENT_INFERENCE_PLANE_MODEL_DIR` (default
  `…/models`) and the HF cache.
- **Wire types:** `ManagedBinding`, `ReachableBinding`, `Capabilities`, `Modality` come from
  `theygent_ir.registration` in [packages/ir](../../packages/ir/AGENTS.md). Never redefine them here.

## Map

<!-- agents-map: covers=src/theygent_inference_plane -->

All paths under [`src/theygent_inference_plane/`](src/theygent_inference_plane):

| Module | What it does |
|--------|--------------|
| [`app.py`](src/theygent_inference_plane/app.py) | `create_app` with injectable seams (launcher, clock, probe, policy, catalog, downloader); the `(engine, modality)` launcher table; `/admin/*` and `/v1/*` routes; OpenAI-style error mapping; guarded SSE; the idle reaper |
| [`registry.py`](src/theygent_inference_plane/registry.py) | Logical id → binding, persisted atomically to `registry.json`; `UnknownLogicalId` is why an engine name is a 404 on `/v1/*` |
| [`manager.py`](src/theygent_inference_plane/manager.py) | `EngineManager`: lazy spawn, leases and in-flight counts, draining, idle teardown, eviction at admission; returns an `Upstream` (`api_base`, `needs_tool_parse`, …) |
| [`launcher.py`](src/theygent_inference_plane/launcher.py) | `EngineLauncher` protocol, the subprocess lifecycle, per-engine launchers (`LlamaCppLauncher`, `WhisperCppLauncher`, `MlxLauncher`, `MlxVlmLauncher`, `MlxAudioLauncher`, `ImageServerLauncher`), capability probes, `ManagedLauncherSet` (exact `(engine, modality)` dispatch), engine logs |
| [`vllm_engine.py`](src/theygent_inference_plane/vllm_engine.py) | `VllmLauncher` — CUDA-only and unverified here; every CUDA assumption stays in this file |
| [`eviction.py`](src/theygent_inference_plane/eviction.py), [`clock.py`](src/theygent_inference_plane/clock.py) | The eviction policy (count ceiling, priority then LRU) and the injectable clock |
| [`gateway.py`](src/theygent_inference_plane/gateway.py) | The L0 dispatch on LiteLLM: `complete`, `stream`, `embed`, transcription; speech and images as direct POSTs; param merging; dropped-request detection (`EngineDroppedRequest`) |
| [`tool_parse.py`](src/theygent_inference_plane/tool_parse.py), [`harmony.py`](src/theygent_inference_plane/harmony.py) | MLX chat output normalized to OpenAI `tool_calls` and `reasoning_content` (Llama text tool calls, gpt-oss harmony); applied only when `Upstream.needs_tool_parse` |
| [`capabilities.py`](src/theygent_inference_plane/capabilities.py) | Pure capability detection from a chat template and config (reasoning, tools, vision, context) |
| [`catalog.py`](src/theygent_inference_plane/catalog.py) | `CatalogProvider` and the Hugging Face provider: listing, variants, RAM fit, MoE hints, browse-time capabilities |
| [`downloader.py`](src/theygent_inference_plane/downloader.py), [`installs.py`](src/theygent_inference_plane/installs.py) | In-plane downloads with progress and cancel, register-on-complete; `installs.json` provenance (repo + variant) |
| [`weights.py`](src/theygent_inference_plane/weights.py) | GGUF header reader that refuses an incomplete diffusion checkpoint before `sd-cli` fails deep inside |
| [`credentials.py`](src/theygent_inference_plane/credentials.py) | Write-only credential store (`0600`) and `secret://NAME` resolution (store, then env); the reserved `HF_TOKEN` |
| [`settings.py`](src/theygent_inference_plane/settings.py) | `settings.json` and `maxResident` resolution (env > stored > default) |
| [`transfer.py`](src/theygent_inference_plane/transfer.py) | `GET /admin/export`, `POST /admin/import`: bindings and provenance only, never weights or credential values |
| [`binary.py`](src/theygent_inference_plane/binary.py) | Engine binary resolution: `THEYGENT_*_BIN` → `PATH` → `python -m` → not found (a `/readyz` reason) |
| [`image_server.py`](src/theygent_inference_plane/image_server.py) | Stdlib-only OpenAI images server wrapping `sd-cli` / `mflux-generate`; spawned as a module |
| [`mlx_expert_stream.py`](src/theygent_inference_plane/mlx_expert_stream.py) | `mlx_lm.server` with routed MoE experts streamed from disk through a per-layer GPU slot bank; runs in mlx-lm's interpreter and imports nothing from this package |
| [`__main__.py`](src/theygent_inference_plane/__main__.py), [`asgi.py`](src/theygent_inference_plane/asgi.py) | Dev entrypoint (env, state dir, uvicorn) and the side-effect-free ASGI module |
| [`spikes/`](spikes) | Measurement spikes (`llamacpp_moe`, `mlx_expert_streaming`) with results; not product code |

## The `(engine, modality)` table

Built in `app.py`. One launcher per key; an unlisted pair is a 503 `engine_unavailable`.

| Engine | Modality | Server spawned |
|--------|----------|----------------|
| `llamacpp` | `chat`, `embeddings`, `vision` | `llama-server` (one launcher; vision adds `--mmproj`) |
| `llamacpp` | `audio.transcription` | `whisper-server` |
| `llamacpp` | `images.generation` | `image_server` wrapping `sd-cli` |
| `mlx` | `chat` | `mlx_lm.server` (or `mlx_expert_stream` when `streamExperts` is on) |
| `mlx` | `vision` | `mlx_vlm.server` |
| `mlx` | `audio.speech` | `mlx_audio.server` |
| `mlx` | `images.generation` | `image_server` wrapping `mflux-generate` |
| `vllm` | `chat` | `vllm serve` |

## Key flows

1. **`POST /v1/chat/completions`:** `registry.require(model)` (unknown → 404 `model_not_found`) →
   managed: `manager.warm()` (spawn or reuse, evict if needed, 503 `no_capacity` /
   `engine_unavailable`) then a lease held for the whole stream; reachable: resolve
   `credentialRef` locally → `gateway.stream`/`complete` → SSE. Upstream errors map to OpenAI error
   shapes before the response commits; a client disconnect cancels the upstream call and releases
   the lease.
2. **Install from the catalog:** `GET /admin/catalog/models` → `POST /admin/catalog/install` →
   the downloader fetches into the model dir → registers `source=local-path` → records provenance
   in `installs.json` → the model appears in `GET /admin/models`.

## Making changes

- **New engine or modality:** a launcher in `launcher.py` (or its own module), one row in the
  `app.py` table, a fast-suite test with the fake launcher, a gated real-engine test, the row
  above, and the user docs engines page. Never touch `EngineManager` callers.
- **New `/admin/*` field:** camelCase on the wire, the interface's `lib/api.ts` types, and the
  user docs that show it.
- **New env var:** empty means unset; add it to `.env.example`, the deploy manifests that need it,
  and `docs/user-docs/content/reference/environment.md`.

## The contract

- **Two HTTP surfaces, never conflated.** `/v1/*` is the OpenAI-compatible data plane —
  conform to OpenAI's shapes. `/admin/*` is the theygent-native management plane. Engine
  names exist only on `/admin/*`; on `/v1/*` an engine name is simply not a registered
  logical id and returns `model_not_found`. There is a negative test for this; keep it
  green.
- **`binding` · `source` · `modality` are three orthogonal axes.** `mlx | vllm | llamacpp`
  are lifecycle-managed engines; `openai-compatible` is a reachable passthrough that
  bypasses the `EngineManager` entirely. `source` says where weights come from; `modality`
  (chat, vision, embeddings, speech-to-text, text-to-speech, image generation) is the
  spawn discriminator on the registration payload — it is never a binding value and never
  appears on `/v1/*`.
- **Fail-closed modality dispatch.** `ManagedLauncherSet` looks up the exact
  `(engine, modality)` key with no chat fallback — an unregistered pair is rejected 503
  `engine_unavailable`, never silently served by the chat launcher.

## The seams (do not collapse)

- **`EngineLauncher`** (`launcher.py`): the manager depends on `launch(binding)`, never on
  `subprocess.Popen`. Adding an engine or modality means a new launcher behind
  `ManagedLauncherSet` with **zero `EngineManager` caller changes** — if a change seems to
  require touching manager callers, the seam is wrong; stop and fix the seam.
- **`EvictionPolicy`** (`eviction.py`): eviction is a policy the manager calls, never
  inline manager logic. Arbitration is count-based (`maxResident`, priority-then-LRU);
  real byte accounting would drop into the same `select_victims` signature. Keep the
  injected clock and resource-probe seams — they make lifecycle tests deterministic.
- **Never evict an engine with an in-flight request.** Only idle, non-draining engines are
  offered to the policy; evicting a busy engine marks it draining and it tears down on
  idle. With no capacity and nothing evictable, the load is refused (503), never resolved
  by killing a live stream.

## Boundaries

- **State is local.** `registry.json`, `credentials.json`, and `settings.json` live in the
  plane's own state dir (`~/.theygent/inference` by default), written atomically —
  never the control plane's Postgres. `registry.py`/`catalog.py`/`downloader.py` import no
  SQLAlchemy/asyncpg/control-plane code (a fast-suite guard asserts this).
- **Credentials never leave the user's trust domain.** `credentialRef` resolves locally
  (store, then env); listings return names + `hasValue`, never values. The browser writes
  the HF token directly to this plane — never proxy that write through the control plane.
- **Model weights are downloaded here** (`/admin/catalog/*` → the in-plane downloader into
  the local model dir) — that is the sovereignty promise made concrete. Third-party engine
  *binaries* are resolved (`binary.py`: env var → PATH → `python -m`) and supervised, never
  built or bundled; a missing binary is a `/readyz` not-ready and a clean 503, never a 500 at
  first inference. TheYgent's own engine programs ship in this package instead:
  `image_server` (wraps diffusion CLIs) and `mlx_expert_stream` (`mlx_lm.server` with
  experts streamed from disk). `mlx_expert_stream` runs in mlx-lm's interpreter, so it
  imports only the stdlib, mlx, mlx-lm and mlx-lm's dependencies (numpy, huggingface_hub) —
  never this package.

## Per-engine reality

Each engine is an external OpenAI-compatible server this service spawns; their surfaces
differ (capability probes, health endpoints, context discovery). Never assume one engine's
shape for another, and keep conservative capability reports marked `approximate`.
**vLLM's real path is CUDA-only and unverified here** — its integration test is
CUDA-gated; do not treat "type-checks" as "works", and keep every CUDA/VRAM assumption
confined to `vllm_engine.py`.

## Tests

```bash
uv run --package theygent-inference-plane pytest                  # fast suite (fake launchers, no binaries)
uv run --package theygent-inference-plane pytest -m integration   # opt-in; env-gated per engine, skips clean
```

The fast suite must run on any machine with no engine installed. Real-engine integration
tests gate on env vars (model paths / binaries) and skip cleanly when absent; a new
launcher or modality needs both a fast-suite test and a gated real-path test.

Fixtures: `tests/conftest.py` and `tests/_fake_upstream.py` (`FakeUpstreamLauncher`: a real
OpenAI-compatible server standing in for every engine), `tests/_dropping_upstream.py` (an engine
that drops requests like `mlx_lm.server`), `tests/_payloads.py` (camelCase registration builders).
Tests are named by area: `test_fast_suite.py` (both surfaces end to end), `test_engines.py`,
`test_eviction.py`, `test_modality_dispatch.py`, `test_catalog*.py`, `test_transfer.py`,
`test_tool_parse.py`, `test_harmony.py`, `test_mlx_*`, `test_image_*`, and `test_integration_*.py`
for the real engines.
