# AGENTS.md — packages/ir-types

Rules for `packages/ir-types` (`@theygent/ir-types`) — the generated TypeScript mirror of
the Pydantic IR. The repo-wide rules in the root [AGENTS.md](../../AGENTS.md) apply first;
the pipeline is documented in
[docs/dev-docs/ir-and-packages.md](../../docs/dev-docs/ir-and-packages.md).

## Where it sits

- **Generated from:** [packages/ir](../ir/AGENTS.md), by `scripts/generate.py` (run with
  `make gen-ir-types`, i.e. `pnpm --filter @theygent/ir-types generate`).
- **Consumed by:** the [interface](../../apps/interface/AGENTS.md) (workspace dependency: types,
  the palette, config forms, default ports, `kindForType`) and the
  [MCP server](../../apps/mcp-server/AGENTS.md), whose authoring guide reads `src/node-types.json`
  directly.
- **Guarded by:** the CI `frontend` job and the `ir-types-drift` pre-commit hook, which
  regenerate and fail on any diff, including new untracked outputs (`tsc-ir-types` type-checks
  the package).

## Map

<!-- agents-map: covers=src -->
<!-- agents-map: covers=scripts -->

| File | What it is |
|------|------------|
| [`scripts/generate.py`](scripts/generate.py) | The generator (hand-written): imports the Pydantic models and emits the three files below |
| [`src/ir.schema.json`](src/ir.schema.json) | Generated. `IRDocument.model_json_schema()` |
| [`src/ir.d.ts`](src/ir.d.ts) | Generated. TypeScript types compiled from the schema |
| [`src/node-types.json`](src/node-types.json) | Generated. Per node type: kind, config JSON Schema, default config, default ports (including tool-role overrides) |
| [`src/index.ts`](src/index.ts) | Hand-written re-export surface: the types, `NODE_TYPES`, `NODE_TYPE_LIST`, `kindForType()` |
| [`README.md`](README.md), [`package.json`](package.json), [`tsconfig.json`](tsconfig.json) | Package metadata; `generate` and `typecheck` scripts |

## Rules

- **Generated files are never hand-edited.** `src/ir.schema.json`, `src/ir.d.ts`, and
  `src/node-types.json` are produced by `scripts/generate.py` from `packages/ir`. A hand
  edit is either overwritten by the next generate or fails the CI drift guard (which
  regenerates and `git diff --exit-code`s, including a check for untracked outputs).
- **The only hand-written files are `src/index.ts`** (the typed re-export surface over the
  generated artifacts) **and `scripts/generate.py`** (the generator itself).
- **Changes start in `packages/ir`, never here.** To add or change a node type, config
  field, or enum: edit the Pydantic models, then run `make gen-ir-types` and commit the
  regenerated output in the same change. The frontend palette, config forms, and port
  definitions all derive from `node-types.json` — a new node type reaches the canvas with
  zero frontend code.
- Keep the generator deterministic: same input models → byte-identical output, or the
  drift guard turns every unrelated PR red.
