# AGENTS.md

Rules for anyone — human or coding agent — making changes to this repository. Per-app
files add local rules: [apps/control-plane](apps/control-plane/AGENTS.md),
[apps/inference-plane](apps/inference-plane/AGENTS.md),
[apps/interface](apps/interface/AGENTS.md), [apps/worker](apps/worker/AGENTS.md),
[apps/web](apps/web/AGENTS.md), [packages/ir](packages/ir/AGENTS.md),
[packages/ir-types](packages/ir-types/AGENTS.md),
[packages/gateway-client](packages/gateway-client/AGENTS.md),
[docs/user-docs](docs/user-docs/AGENTS.md). Read the architecture first:
[docs/dev-docs/architecture.md](docs/dev-docs/architecture.md).

## What this is

TheYgent is a no-code, local-first platform for building and running AI agents: a visual
builder that compiles graphs into a typed IR, and a runtime that executes them against
local engines (llama.cpp, MLX, vLLM) or any OpenAI-compatible API — the user's choice per
agent and per call. The core promise is anti-lock-in: TheYgent is never an involuntary
middleman between a user and their models.

## Definition of done

A change is not done until all of these hold:

1. **All tests pass.** `make test` (Python suites + interface vitest + deploy contract
   guards) and `make lint` (ruff · ty · biome · tsc · ir-types drift) are both green.
   Never mark work complete with a failing suite; never skip or weaken a test to get green.
2. **Docs move with behavior.** Any user-visible change updates the matching page under
   [docs/user-docs/content](docs/user-docs/content) (and its `nav:` entry — the strict
   build fails on orphans). Any architectural or contract change updates
   [docs/dev-docs](docs/dev-docs). New env vars land in the environment reference.
3. **New behavior lands with a test.** Bug fixes include a regression test; new node
   types, endpoints, or settings include coverage in the owning package's suite.
4. **Contract extensions are deliberate.** The frozen surfaces below stay frozen until a
   change is consciously made, named, and tested — even when additive. Never extend a
   contract as a side effect of something else.

## Architecture guardrails

These decisions are load-bearing and expensive to reverse. Do not change them casually;
if a task seems to require it, stop and flag it instead.

- **The two-plane split.** The control plane (orchestration) never imports an engine —
  no `mlx`, `vllm`, or llama.cpp bindings anywhere in `apps/control-plane`, `apps/worker`,
  or `packages/*`. It reaches inference only over the OpenAI-compatible HTTP seam via
  `packages/gateway-client`. Symmetrically, the inference plane keeps its state (registry,
  credentials, settings, weights) in its own local files and never touches the control
  plane's Postgres.
- **Logical model ids only on the data plane.** The `model` field on `/v1/*` and in every
  graph node is a logical id (`triage-fast`), never an engine name. Engine names exist
  only on the inference plane's `/admin/*` surface, and the control plane rejects them
  with `400 engine_name_not_allowed`.
- **The binding enum is frozen at four values:** `mlx | vllm | llamacpp |
  openai-compatible`. Everything else in the world is reached as `openai-compatible` plus
  a URL. `source` (`hf | local-path | url`) and `modality` are separate axes — never fold
  them into the enum.
- **The IR is the single source of truth** and lives in `packages/ir` (Pydantic). The
  TypeScript types in `packages/ir-types` are generated — never hand-edited. `contentHash`
  is computed server-side only, over canonical, view-stripped, key-sorted JSON; the `view`
  block (layout) is never hashed. Anything that shifts existing hashes is a major,
  deliberate decision (a pinned-hash guard test enforces this).
- **FastAPI is only the API surface.** No durable orchestration in request handlers;
  long-running work belongs to the graph runtimes. The `dbos` import lives only under
  `apps/control-plane/.../durable/` and in the worker.
- **The plane boundary for data:** raw prompts/payloads/weights flow between the user and
  the inference plane; the control plane receives orchestration, session text, and
  telemetry. Never route a user's model traffic or credentials through the control plane
  when it can go direct.
- **Secrets are never inline.** Auth material lives in the encrypted secret store behind
  `secret_ref`s; graph IR and connection configs carry references, never values. Rotating
  a secret keeps its ref so agent hashes stay stable.

## Code conventions

- **Comments state present-tense constraints and reasons** ("the server exposes no
  `/props`, so max context comes from the model config") — never development history,
  internal plan or milestone references, or comparisons to other products. Committed code
  (including identifiers and string literals) stays free of competitor product names.
- **Loud failures.** Unknown `$in` references, invalid IR, unknown settings, unfillable
  ports: fail fast with a named, stable error code. No silent fallback or pass-through.
- **Naming across the wire:** the control-plane API speaks `snake_case`; the inference
  plane's OpenAI-compatible and admin surfaces speak `camelCase`. Don't "fix" either.
- **Brand casing:** "TheYgent" in prose and UI copy; lowercase `theygent` in code, URLs,
  env vars, and identifiers.
- **Empty env means unset.** Every env read treats `""` like absence (compose/k8s pass
  `${VAR:-}` through); keep new env reads empty-safe.
- Match the surrounding file's style; Ruff formats Python, Biome formats TS.

## Commands

| Task | Command |
|------|---------|
| Bring the stack up (bare metal) | `make up` — inference :8081, control :8080, interface :5174 |
| All tests | `make test` (= `test-py` + `test-web` + `test-deploy`) |
| All lint/type gates | `make lint` |
| One Python package's tests | `uv run --package <name> pytest` (fast suite; integration is opt-in via `-m integration`) |
| Interface tests | `pnpm --filter @theygent/interface test` |
| Regenerate IR types after a `packages/ir` change | `make gen-ir-types` (CI fails on drift) |
| Migrations | `make migrate` (Alembic owns the schema — never `create_all`) |
| User-docs preview / strict build | `make docs-serve` / `make docs-build` |

Integration tests that need real engines or a real browser are marked and skip cleanly
when prerequisites are absent — the default suites must stay runnable on any machine.
