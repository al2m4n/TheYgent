# AGENTS.md — packages/ir

Rules for `packages/ir` (`theygent_ir`) — the single source of truth for the Agent Graph
IR and the inference-plane registration payload. The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first; the design is documented in
[docs/dev-docs/ir-and-packages.md](../../docs/dev-docs/ir-and-packages.md).

This is the most dangerous place in the repo to change: every saved agent's identity, both
runtimes' dispatch, and the frontend's generated types hang off these models.

## Hard rules

- **`theygent_ir` imports nothing but Pydantic.** No SQLAlchemy, no HTTP, no engine code —
  both planes and the frontend codegen depend on it staying pure.
- **`content_hash` is one function with two callers** (the run path and the agent
  registry) — they can never disagree. It hashes the hydrated, default-filled, validated
  model over canonical key-sorted JSON; the `view` block is stripped first and is never
  hashed.
- **Adding a defaulted field to any hashed model shifts every existing `contentHash`.**
  The pinned-hash guard test exists to force that to be a named, deliberate decision —
  when it fires, stop and confirm the hash migration story before updating the pin.
- **The binding enum is frozen at four values** (`mlx | vllm | llamacpp |
  openai-compatible`); `source` (`hf | local-path | url`) is a weights axis, never an
  engine. `modality` exists only on the registration payload (`registration.py`), never on
  the graph's `ModelBinding` — it must never touch a `contentHash`.
- **Dispatch is by `kind`** (`boundary | activity | orchestration`); `NODE_TYPE_KIND` maps
  every node type to its kind, and a mismatch is a validation error. A new node type here
  requires matching executors in both control-plane runtimes and a regenerated
  `packages/ir-types`.
- **`validate_graph` runs before any run or publish** — validation failures are 400s and
  nothing persists. Add new structural rules here, not in per-consumer code.
- **After any model change:** `make gen-ir-types` (regenerates the JSON Schema, the TS
  types, and the node registry). CI fails on drift, including untracked generated files.

## Tests

```bash
uv run --package theygent-ir pytest
```

Graph validation, hash stability (the pinned trivial-graph hash), kind/type integrity, and
registration-payload rules all live in `tests/`. A change that alters any pinned value
must explain why in the PR, not just update the pin.
