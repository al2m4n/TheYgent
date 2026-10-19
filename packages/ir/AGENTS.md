# AGENTS.md — packages/ir

Rules for `packages/ir` (`theygent_ir`) — the single source of truth for the Agent Graph
IR, the inference-plane registration payload, and the AI gateway ↔ control plane wire
types. The repo-wide rules in the root [AGENTS.md](../../AGENTS.md) apply first; the
design is documented in [docs/dev-docs/ir-and-packages.md](../../docs/dev-docs/ir-and-packages.md).

This is the most dangerous place in the repo to change: every saved agent's identity, both
runtimes' dispatch, and the frontend's generated types hang off these models.

## Where it sits

Every Python app imports it; the frontend gets it through generated types.

| Consumer | Uses |
|----------|------|
| [control plane](../../apps/control-plane/AGENTS.md) | `parse_document`, `validate_graph`, `topological_order`, `content_hash`, every config model (walker and durable compiler), `theygent_ir.gateway` (AI gateway records and config) |
| [inference plane](../../apps/inference-plane/AGENTS.md) | `theygent_ir.registration` (`ManagedBinding`, `ReachableBinding`, `Capabilities`, `Modality`) |
| [AI gateway](../../apps/ai-gateway/AGENTS.md) | `theygent_ir.gateway` (records, config, key set, heartbeat, totals, reason codes) |
| [MCP server](../../apps/mcp-server/AGENTS.md) | Local validation, `EXECUTABLE_TYPES`, `NODE_TYPE_KIND` |
| [packages/ir-types](../ir-types/AGENTS.md) | `scripts/generate.py` imports the models and emits the JSON Schema, TS types and node registry the [interface](../../apps/interface/AGENTS.md) consumes |

## Map

<!-- agents-map: covers=src/theygent_ir -->
<!-- agents-map: covers=tests -->

| File | What it does |
|------|--------------|
| [`src/theygent_ir/graph.py`](src/theygent_ir/graph.py) | The IR: `IRDocument`, `Node`, `Edge`, `Port`/`Ports`, `ModelBinding`, tool bindings (`BuiltinTool`, `HttpTool`, `McpTool`), one config model per node type (`_CONFIG_MODELS`), `NODE_TYPE_KIND` (type → `boundary`/`activity`/`orchestration`), `EXECUTABLE_TYPES`, `_CAPABILITY_TYPES` (what may feed an llm's `tools` port), `_PINNED_BODY_TYPES` (subgraph/loop/map pin exactly one version or hash), `parse_document`, `validate_graph`, `topological_order` |
| [`src/theygent_ir/contenthash.py`](src/theygent_ir/contenthash.py) | `content_hash`: `sha256:` over the validated, default-filled model dumped as canonical key-sorted JSON, with `view` and `contentHash` excluded |
| [`src/theygent_ir/registration.py`](src/theygent_ir/registration.py) | The inference plane's registration payload (camelCase on the wire): `ManagedBinding` (with `modality`), `ReachableBinding` (with `requireFinishReason`), `Lifecycle`, `Capabilities`, `MODALITIES`, `BINDING_NAMES`, `parse_registration` |
| [`src/theygent_ir/gateway.py`](src/theygent_ir/gateway.py) | AI gateway ↔ control plane types (snake_case): `GatewayRequestRecord`, `GatewayEventBatch`/`Ack`, `GatewayTotals`, `GatewayConfig`/`PolicyConfig`, local routing, `GatewayKeySet`, heartbeat, fingerprints, `TARGET_CAPABILITIES`; every free-form string is a constrained `Token` |
| [`src/theygent_ir/__init__.py`](src/theygent_ir/__init__.py) | Re-exports the graph and registration API (`theygent_ir.gateway` is imported from the submodule) |
| [`tests/test_graph.py`](tests/test_graph.py), [`tests/test_registration.py`](tests/test_registration.py), [`tests/test_gateway.py`](tests/test_gateway.py) | Validation, the pinned hash, kind/type integrity, registration rules, the no-free-text scan of the gateway types |
| [`tests/test_require_finish_reason.py`](tests/test_require_finish_reason.py) | `requireFinishReason`: off by default, camelCase round trip, reachable-only (rejected on `ManagedBinding`), boolean only |

## Adding a node type (the full path)

1. Here: a config model, its `_CONFIG_MODELS` and `NODE_TYPE_KIND` entries, `EXECUTABLE_TYPES`
   once it runs, any structural rule in `validate_graph`, tests.
2. `make gen-ir-types`, and commit the regenerated `packages/ir-types/src/*`.
3. The control plane: an executor and handler in `walker.py`, a step and branch in
   `durable/compiler.py`, tests in both runtimes.
4. The interface: the palette and forms appear on their own; add an inspector case only when a
   field needs more than the generic schema form.
5. Docs: a page under `docs/user-docs/content/building/nodes/` and its `nav:` entry, and the node
   set in `docs/dev-docs/graph-execution.md`.

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
- **The AI gateway wire types (`gateway.py`) carry no free text.** Every string field is a
  constrained `Token` or a closed vocabulary — the plane boundary (prompts and credentials stay in
  the gateway) is enforced by the types, and a schema-scanning test guards it. They are
  snake_case (the control-plane API's casing) and never hashed.
- **After any model change:** `make gen-ir-types` (regenerates the JSON Schema, the TS
  types, and the node registry). CI fails on drift, including untracked generated files.

## Tests

```bash
uv run --package theygent-ir pytest
```

Graph validation, hash stability (the pinned trivial-graph hash), kind/type integrity, and
registration-payload rules all live in `tests/`. A change that alters any pinned value
must explain why in the PR, not just update the pin.
