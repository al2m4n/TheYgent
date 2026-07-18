# AGENTS.md — interface

Rules specific to `apps/interface` (the React SPA). The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first; the component's design is documented in
[docs/dev-docs/interface.md](../../docs/dev-docs/interface.md).

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
  `inferenceUrl()` (localStorage override → `VITE_*` env → localhost default); never fetch
  against the constants directly.
- **The plane split holds in the browser:** raw prompts, audio, and images go directly to
  the inference plane; the control plane receives run orchestration, session text, and
  telemetry. Credentials (e.g. the HF token) PUT straight to the inference plane — never
  proxied.
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
- **`localStorage` is never the IR store.** It holds only the dev token, theme, endpoint
  overrides, and UI prefs; persistence is the drafts + registry APIs.

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
