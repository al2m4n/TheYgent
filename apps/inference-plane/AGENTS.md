# AGENTS.md — inference plane

Rules specific to `apps/inference-plane`. The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first; the component's design is documented in
[docs/dev-docs/inference-plane.md](../../docs/dev-docs/inference-plane.md).

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
  the local model dir) — that is the sovereignty promise made concrete. Engine *binaries*
  are resolved (`binary.py`: env var → PATH → `python -m`) and supervised, never built or
  bundled; a missing binary is a `/readyz` not-ready and a clean 503, never a 500 at first
  inference.

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
