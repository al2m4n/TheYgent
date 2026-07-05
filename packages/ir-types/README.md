# @theygent/ir-types

Generated TypeScript IR types + node-type registry — the FE/BE IR lockstep seam.

**The one rule: nothing here is hand-written.** The IR types are generated from `packages/ir` (the
Pydantic source of truth). A second, drifting TS definition of the IR is exactly the corruption the
drift guard exists to prevent.

## What's in `src/`

| File | Source | What |
|------|--------|------|
| `ir.schema.json` | `IRDocument.model_json_schema()` | the IR document envelope JSON Schema |
| `ir.d.ts` | `json-schema-to-typescript` over `ir.schema.json` | the IR TS interfaces (`IRDocument`, `Node`, `Edge`, `Port`, …) |
| `node-types.json` | `NODE_TYPE_KIND` + the per-type config models | the node-type registry: per executable `type` → `kind`, `configSchema`, `defaultConfig`, default `ports` |
| `index.ts` | hand-written re-export layer (the only authored file) | re-exports the IR types + typed `NODE_TYPES` / `NODE_TYPE_LIST` / `kindForType` |

## Regenerate

```bash
pnpm --filter @theygent/ir-types generate    # runs the Python generator, then json2ts
```

`generate` = `generate:schema` (`uv run python scripts/generate.py`) + `generate:ts` (`json2ts`).

## Drift guard (CI)

The `frontend` CI job regenerates these artifacts and runs `git diff --exit-code -- src`. If
`packages/ir` moved and the committed artifacts didn't, the build fails — FE and BE IR stay in
lockstep. Additionally, the interface app's `tsc` fails if it references a field the generated types
don't define. **After any change to `packages/ir`'s models, run `generate` and commit the result.**
