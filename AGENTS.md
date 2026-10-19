# AGENTS.md

Rules for anyone — human or coding agent — making changes to this repository, and the map to
the per-module context files below. Read the architecture first:
[docs/dev-docs/architecture.md](docs/dev-docs/architecture.md).

## What this is

TheYgent is a no-code, local-first platform for building and running AI agents: a visual
builder that compiles graphs into a typed IR, and a runtime that executes them against
local engines (llama.cpp, MLX, vLLM) or any OpenAI-compatible API — the user's choice per
agent and per call. The core promise is anti-lock-in: TheYgent is never an involuntary
middleman between a user and their models.

## How the context files work

Every module boundary has an `AGENTS.md`: what the module does, where it sits (who calls it,
what it calls), a file-by-file map, how to make common changes, local rules, and its tests.
**Before reading or changing code in a directory, read its `AGENTS.md` and those of its
parents**, then use the map to open only the files that matter (always read code you are about
to change; the map tells you where it is, not what every line does). Each `AGENTS.md` has a
sibling `CLAUDE.md` containing only `@AGENTS.md`, so tools that read either name load the same
text. `AGENTS.md` is the only file with content; never write context into a `CLAUDE.md`.

## Repository map

<!-- agents-map: covers=apps -->
<!-- agents-map: covers=packages -->

| Path | What it is | Context |
|------|------------|---------|
| `apps/control-plane` | FastAPI orchestration service (:8080): graph runtimes, registry, sessions, triggers, MCP, RAG, observability, settings, identity | [AGENTS.md](apps/control-plane/AGENTS.md) |
| ↳ `src/theygent_control_plane/durable` | The DBOS durable runtime | [AGENTS.md](apps/control-plane/src/theygent_control_plane/durable/AGENTS.md) |
| ↳ `src/theygent_control_plane/mcp` | MCP transports, connections, generated servers, OAuth, the hub | [AGENTS.md](apps/control-plane/src/theygent_control_plane/mcp/AGENTS.md) |
| ↳ `src/theygent_control_plane/rag` | Retrieval sources, ingest, hybrid search | [AGENTS.md](apps/control-plane/src/theygent_control_plane/rag/AGENTS.md) |
| ↳ `src/theygent_control_plane/crawlers` | The `crawler` node's provider seam | [AGENTS.md](apps/control-plane/src/theygent_control_plane/crawlers/AGENTS.md) |
| ↳ `src/theygent_control_plane/observability` | Spans, node I/O, the waterfall's data, OTLP | [AGENTS.md](apps/control-plane/src/theygent_control_plane/observability/AGENTS.md) |
| ↳ `src/theygent_control_plane/ai_gateway` | The AI gateway's records, configs, keys, reports | [AGENTS.md](apps/control-plane/src/theygent_control_plane/ai_gateway/AGENTS.md) |
| ↳ `src/theygent_control_plane/samples` | Shipped sample agents | [AGENTS.md](apps/control-plane/src/theygent_control_plane/samples/AGENTS.md) |
| ↳ `alembic` | The migration chain | [AGENTS.md](apps/control-plane/alembic/AGENTS.md) |
| ↳ `tests` | Suites, fakes, where a test goes | [AGENTS.md](apps/control-plane/tests/AGENTS.md) |
| `apps/inference-plane` | Engines and the OpenAI-compatible data plane (:8081), model registry, catalog | [AGENTS.md](apps/inference-plane/AGENTS.md) |
| `apps/worker` | Separate deployable hosting the durable runtime | [AGENTS.md](apps/worker/AGENTS.md) |
| `apps/ai-gateway` | Anthropic-Messages proxy for coding agents (:8082): observer and router | [AGENTS.md](apps/ai-gateway/AGENTS.md) |
| `apps/mcp-server` | TheYgent as MCP tools for coding agents | [AGENTS.md](apps/mcp-server/AGENTS.md) |
| `apps/interface` | The React SPA (:5174): builder and every operator surface | [AGENTS.md](apps/interface/AGENTS.md) |
| ↳ `src/lib` | The HTTP seam, SSE, auth, modality, client-side mirrors, transfer | [AGENTS.md](apps/interface/src/lib/AGENTS.md) |
| ↳ `src/components` | Editor pieces, shared widgets, gateway and settings tabs, waterfall, shadcn | [AGENTS.md](apps/interface/src/components/AGENTS.md) |
| ↳ `src/routes` | One component per page | [AGENTS.md](apps/interface/src/routes/AGENTS.md) |
| ↳ `src/chat` | Unified chat and its two transports | [AGENTS.md](apps/interface/src/chat/AGENTS.md) |
| ↳ `src/bench` | Model and agent benches | [AGENTS.md](apps/interface/src/bench/AGENTS.md) |
| `apps/web` | Static marketing site (theygent.ai) | [AGENTS.md](apps/web/AGENTS.md) |
| `packages/ir` | Pydantic IR, registration payload, AI gateway wire types | [AGENTS.md](packages/ir/AGENTS.md) |
| `packages/ir-types` | Generated TypeScript mirror of the IR | [AGENTS.md](packages/ir-types/AGENTS.md) |
| `packages/gateway-client` | The one Python client for the inference data plane | [AGENTS.md](packages/gateway-client/AGENTS.md) |
| `deploy` (+ root `docker-compose*.yml`) | Kubernetes, the sample OTel stack, deploy contract tests | [AGENTS.md](deploy/AGENTS.md) |
| `.github` | CI, publishing workflows, PR template, CI scripts | [AGENTS.md](.github/AGENTS.md) |
| `docs/dev-docs` | Contributor design docs | [AGENTS.md](docs/dev-docs/AGENTS.md) |
| `docs/user-docs` | User docs site (MkDocs, published per release) | [AGENTS.md](docs/user-docs/AGENTS.md) |

Root files: [`Makefile`](Makefile) (every dev command; `make help`), [`.env.example`](.env.example)
(every env var with its default), [`pyproject.toml`](pyproject.toml) (uv workspace),
[`pnpm-workspace.yaml`](pnpm-workspace.yaml), [`.pre-commit-config.yaml`](.pre-commit-config.yaml)
(the `make lint` gates).

## Definition of done

A change is not done until all of these hold:

1. **All tests pass.** `make test` (Python suites + interface vitest + deploy contract
   guards) and `make lint` (ruff · ty · biome · tsc · ir-types drift · agent docs) are both green.
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
5. **Context files move with code.** Every `AGENTS.md` covering a directory the change
   touches is true after the change. See [Keeping the context files current](#keeping-the-context-files-current).

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
  (including identifiers and string literals) stays free of competitor product names. The
  one deliberate exception is the marketing site's comparison pages (`apps/web/compare/`,
  plus the `llms.txt` and sitemap entries that index them) — see
  [apps/web/AGENTS.md](apps/web/AGENTS.md).
- **Loud failures.** Unknown `$in` references, invalid IR, unknown settings, unfillable
  ports: fail fast with a named, stable error code. No silent fallback or pass-through.
- **Naming across the wire:** the control-plane API speaks `snake_case`; the inference
  plane's OpenAI-compatible and admin surfaces speak `camelCase`. Don't "fix" either.
- **Brand casing:** the brand is "TheYgent" — in prose, UI copy, the GitHub repository
  (`al2m4n/TheYgent`), and wire-visible names (the `X-TheYgent-Signature` header).
  Lowercase `theygent` only where platform convention forces it: code identifiers,
  package and module names, env vars (`THEYGENT_*`), domains (`theygent.ai`), and
  Docker/Kubernetes names.
- **Empty env means unset.** Every env read treats `""` like absence (compose/k8s pass
  `${VAR:-}` through); keep new env reads empty-safe.
- Match the surrounding file's style; Ruff formats Python, Biome formats TS.

## Keeping the context files current

Agents read these files instead of the code, so a stale map is worse than none: it sends the
next agent to the wrong file with confidence. Updating them is part of the change, never a
follow-up.

**Update the nearest `AGENTS.md` in the same PR when a change:**

- adds, removes, renames or moves a file or directory its map lists;
- changes what a module does, or who calls it and what it calls;
- adds or changes an endpoint, node type, setting, env var, migration, workflow, CLI command or
  test fixture the file mentions;
- makes a rule, flow or "Making changes" recipe in it untrue.

Also update a parent's map when a child's one-line summary stops being true, and the
[repository map](#repository-map) when a module is added or removed.

**A new module boundary** (a new app, package, or a subpackage big enough that an agent would
otherwise read several files to understand it) gets its own `AGENTS.md` plus a one-line
`CLAUDE.md` with `@AGENTS.md`, linked from its parent's map and from the repository map. Small
directories are covered by their parent's map instead.

**What CI enforces** ([`check_agent_docs.py`](.github/scripts/check_agent_docs.py), first step
of the `fast` job and the `agent-docs` pre-commit hook): every `AGENTS.md` has its `CLAUDE.md`
stub and is linked from the repository map; every relative link and `#anchor` resolves; and
every directory a map declares is fully mapped. A map declares the directories it covers with
an invisible marker under its map heading, path relative to the `AGENTS.md`:

```markdown
<!-- agents-map: covers=src/theygent_control_plane -->
<!-- agents-map: covers=. depth=2 skip=ui -->
```

Each file and subdirectory there (to `depth`, default 1) must be named in the `AGENTS.md`, so a
new, renamed or deleted file fails CI until its map line is written. CI cannot tell whether a
description is still *true*; that part is the checklist below and review.

**Before opening a PR, a coding agent:**

1. lists the directories the diff touches (`git diff --name-only main...`);
2. re-reads the `AGENTS.md` that covers each of them, and its parents;
3. fixes every statement the diff made false, and adds what the diff introduced;
4. fills the PR template's context-files line: which files were updated, or why none needed it.

**How to write them:** present tense, facts over prose, relative links that resolve, file-level
maps (`| file | what it does |`). Describe what the code does and how the parts relate; leave
design rationale to `docs/dev-docs` and link to it. The same rules as code comments apply: no
development history, plan references or competitor names.

## Commands

| Task | Command |
|------|---------|
| Bring the stack up (bare metal) | `make up` — inference :8081, control :8080, interface :5174, AI gateway :8082 |
| All tests | `make test` (= `test-py` + `test-web` + `test-deploy`) |
| All lint/type gates | `make lint` |
| Check the context files only | `python3 .github/scripts/check_agent_docs.py` |
| One Python package's tests | `uv run --package <name> pytest` (fast suite; integration is opt-in via `-m integration`) |
| Interface tests | `pnpm --filter @theygent/interface test` |
| Regenerate IR types after a `packages/ir` change | `make gen-ir-types` (CI fails on drift) |
| Migrations | `make migrate` (Alembic owns the schema — never `create_all`) |
| User-docs preview / strict build | `make docs-serve` / `make docs-build` |

Integration tests that need real engines or a real browser are marked and skip cleanly
when prerequisites are absent — the default suites must stay runnable on any machine.
