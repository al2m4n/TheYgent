# AGENTS.md — interface

Rules specific to `apps/interface` (the React SPA). The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first; the component's design is documented in
[docs/dev-docs/interface.md](../../docs/dev-docs/interface.md).

## What it is

TheYgent's one frontend (`@theygent/interface`): a Vite + React 19 SPA on :5174 (TanStack
Router and Query, Tailwind v4, shadcn/ui, React Flow, CodeMirror). It renders the visual agent
builder (an agent's IR as a canvas) and every operator surface: dashboard, agents, chat, runs,
sessions, model registries, MCP, RAG, the AI Gateway page, settings. It is served by Vite in dev
and by nginx in the container image.

## Where it sits

- **Talks to the [control plane](../control-plane/AGENTS.md)** (`controlPlaneUrl()`, snake_case):
  runs, sessions, agents, drafts, triggers, connections, RAG, MCP, bench store, settings, auth,
  artifacts, export/import, the AI gateway's reports and config.
- **Talks to the [inference plane](../inference-plane/AGENTS.md) directly** (`inferenceUrl()`,
  camelCase `/admin/*`, OpenAI-shaped `/v1/*`): the model registry and catalog, credentials, its
  settings, and raw model traffic for chat with a model and the bench.
- **Talks to the [AI gateway](../ai-gateway/AGENTS.md) directly** (`gatewayUrl()`, `/admin/*`):
  gateway status and the organization's Anthropic key.
- **Types come from [packages/ir-types](../../packages/ir-types/AGENTS.md)** — generated from
  [packages/ir](../../packages/ir/AGENTS.md).

## Map

<!-- agents-map: covers=src -->
<!-- agents-map: covers=src/adapter -->
<!-- agents-map: covers=src/auth -->
<!-- agents-map: covers=src/hooks -->

| Path | What it holds | Context |
|------|---------------|---------|
| [`src/main.tsx`](src/main.tsx) | Providers: theme → TanStack Query → auth → `AuthGate` → router | |
| [`src/router.tsx`](src/router.tsx) | The route table, role floors per route, the in-shell NotFound | [src/routes/AGENTS.md](src/routes/AGENTS.md) |
| [`src/routes/`](src/routes) | One component per page | [src/routes/AGENTS.md](src/routes/AGENTS.md) |
| [`src/adapter/`](src/adapter) | The React Flow ⇄ IR seam: `index.ts` (`irToReactFlow`, `reactFlowToIr`, every IR→IR mutation the canvas uses), `layout.ts` (auto-layout for a view-less IR), `types.ts` (RF-facing types). The only code that knows React Flow's node/edge shape | The one rule below |
| [`src/components/`](src/components) | Editor pieces (canvas, palette, inspector, test panel), shared widgets, the AI Gateway and Settings tabs, the run waterfall, shadcn primitives | [src/components/AGENTS.md](src/components/AGENTS.md) |
| [`src/lib/`](src/lib) | The HTTP seam (`api.ts`), SSE, auth, modality, validation mirrors, transfer, notifications, theme, formatting | [src/lib/AGENTS.md](src/lib/AGENTS.md) |
| [`src/chat/`](src/chat) | The unified chat: one `ChatView` over two transports | [src/chat/AGENTS.md](src/chat/AGENTS.md) |
| [`src/bench/`](src/bench) | Model and agent benches: capability-routed panels, metrics, presets | [src/bench/AGENTS.md](src/bench/AGENTS.md) |
| [`src/auth/`](src/auth) | Pre-app screens: `AuthGate`, `SetupWizard` (first run), `LoginScreen` (local + SSO), `ProfileModal` (password, API keys, theme), `AuthShell` (their full-screen chrome) | |
| [`src/hooks/`](src/hooks) | `useDraftAutosave` (the drafts state machine), `use-mobile` (shadcn) | |
| [`src/queries.ts`](src/queries.ts) | TanStack Query hooks and the `keys` map: paginated lists, run polling, mutations with invalidation | |
| [`src/index.css`](src/index.css) | Theme tokens (`--c-*`), the light-theme slate inversion, React Flow theming | |
| [`tests/`](tests) | Vitest + Testing Library suites (`*.test.ts(x)`), `fixtures.ts` (stored-version-shaped IRs), `setup.ts`, `smoke/hand_drive.py` (`make smoke-interface` against a live stack) | |
| [`Dockerfile`](Dockerfile), [`nginx.conf`](nginx.conf) | The static image; `VITE_*` URLs are baked in at build time | [deploy/AGENTS.md](../../deploy/AGENTS.md) |
| [`components.json`](components.json), [`biome.json`](biome.json), [`vite.config.ts`](vite.config.ts) | shadcn CLI config, lint/format config, Vite config (`@/` → `src/`) | |

## Making changes

- **New page:** a component in `src/routes/`, a route (with its role floor) in `router.tsx`, a nav
  entry in `routes/Root.tsx`, the `Page` container, and a user docs page.
- **New API call:** a method on the `api` object in `lib/api.ts` (or `gatewayAdmin`), a query or
  mutation in `queries.ts` when it is server state, types next to it. Never `fetch` elsewhere.
- **New node type:** nothing here unless a config field needs a custom inspector form
  (`components/Inspector.tsx`) or the type is durable-only (`lib/durable.ts`).
- **New setting:** it appears from the control plane's catalog; add a tab field only when it needs
  a custom control (`components/settings/`).

## The one rule

**The IR is the app state; React Flow is a renderer behind one adapter.** Only
`src/adapter/*` (and the canvas components it backs — `GraphCanvas`, `NodeView`) know
React Flow's node/edge shape. App state, API calls, and persistence speak IR types from
`@theygent/ir-types` — never React Flow's types, never a hand-written TS IR. RF node
`data` carries only `label`/`nodeType`/`ports`; never `kind`, `models`, or `contentHash`.

## Hard rules

- **Types are generated.** Consume `@theygent/ir-types`; after any `packages/ir` change
  run `make gen-ir-types`. CI regenerates and diffs, so hand-edits to generated files are
  always overwritten or fail the build.
- **No hashing in the frontend.** `contentHash` and canonicalization live server-side in
  `packages/ir`. `lib/canonical.ts` is a structural-equality helper for the "modified"
  badge and tests — it must mirror the server's view/hash exclusions, never produce a hash.
- **`view` never affects logic or the hash.** Dragging, tidying, icon picks mutate `view`
  only; a pure layout change must leave the hashed content identical (asserted by the
  adapter "view isolation" test). A view-less IR still renders via auto-layout.
- **`src/lib/api.ts` is the only HTTP seam** (`bench/dataplane.ts` is the one sanctioned
  sibling for raw `/v1/*` calls). Base URLs resolve **per call** via `controlPlaneUrl()` /
  `inferenceUrl()` / `gatewayUrl()` (localStorage override → `VITE_*` env → localhost
  default); never fetch against the constants directly.
- **The plane split holds in the browser:** raw prompts, audio, and images go directly to
  the inference plane; the control plane receives run orchestration, session text, and
  telemetry. Credentials PUT straight to the service that owns them — the HF token to the
  inference plane, the organization's Anthropic key to the AI gateway — never proxied, never
  displayed.
- **Drafts are the mutable autosave tier, not versions.** They may be structurally
  invalid, are never validated as a graph, never hashed, never published. Publish goes
  through the agent-registry contract (create → 409 → add-version); version bumping is the
  author's action, and `version_conflict` is surfaced verbatim.
- **The node palette is derived, never hardcoded** — per-type kind/config/ports come from
  the generated `NODE_TYPES` registry; a type added in `packages/ir` appears on the canvas
  after a regenerate with zero FE change.
- **Every streaming surface aborts its fetch on Stop/close/unmount** — the server cancels
  on disconnect, and an orphaned reader would keep a local engine generating. Record a
  deliberate stop before aborting so it reads as "stopped", not a failure.
- **`localStorage` is never the IR store.** It holds only the session token, theme, endpoint
  overrides, and UI prefs; persistence is the drafts + registry APIs.
- **Hand-kept mirrors stay in sync with the backend:** `lib/durable.ts` (durable-only types),
  `lib/validate.ts` (a fast mirror of `validate_graph`), `lib/kind.ts` (guardrail kind),
  `lib/gatewayReasons.ts` (exhaustive over the gateway's reason codes). The backend stays
  authoritative.

## UI system

All new UI composes the shadcn/ui components in `src/components/ui/` (add missing ones via
the shadcn CLI; several generated files carry deliberate local edits marked with comments —
answer "No" to overwrite prompts and re-apply the edit if you must regenerate). Style with
the semantic theme tokens (`bg-card`, `text-muted-foreground`, `border-border`, …) — never
raw palette color classes. The `src/components/ui.tsx` wrappers (Modal, Page,
ConfirmDialog, …) encode app behavior invariants — use them where they exist. Searchable
pickers use `SearchableSelect` (Popover + Command); relative times use `TimeAgo`.

## Tests

```bash
pnpm --filter @theygent/interface test    # vitest + RTL
pnpm --filter @theygent/interface check   # biome
pnpm --filter @theygent/interface build   # tsc --noEmit + vite build
```

The headline test is the adapter round-trip: IR → React Flow → IR is identity on
view-stripped content — prove the seam is lossless before trusting anything built on it.
Radix-backed components need real interaction patterns in RTL (`userEvent.click` for
menus, keyboard events for sliders); don't swap a plain button for a Radix trigger just to
satisfy a `role` query — style the button instead.
