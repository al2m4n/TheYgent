# AGENTS.md — packages/ir-types

Rules for `packages/ir-types` (`@theygent/ir-types`) — the generated TypeScript mirror of
the Pydantic IR. The repo-wide rules in the root [AGENTS.md](../../AGENTS.md) apply first;
the pipeline is documented in
[docs/dev-docs/ir-and-packages.md](../../docs/dev-docs/ir-and-packages.md).

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
