# Developer documentation

Documentation for people working **on** TheYgent — the architecture, each component's
design and invariants, and how to build, test, and deploy the stack. If you want to *use*
TheYgent, read the [user documentation](https://docs.theygent.ai/) instead.

Start with [architecture.md](./architecture.md) — the system map and the reasoning behind
the load-bearing boundaries. Then go component by component:

| Page | Covers |
|------|--------|
| [architecture.md](./architecture.md) | The two-plane split, the three seams, process topology, monorepo layout, design principles |
| [control-plane.md](./control-plane.md) | The FastAPI orchestration service: runs, agents, sessions, triggers, MCP, RAG, observability, secrets, settings |
| [graph-execution.md](./graph-execution.md) | How an IR graph runs: the walker, the node set, `$in` substitution, conditional dataflow |
| [durable-execution.md](./durable-execution.md) | Crash-safe runs: the journaled workflow, durable-only nodes, the worker process |
| [inference-plane.md](./inference-plane.md) | Engines and the gateway: bindings, modalities, lifecycle, catalog, credentials |
| [interface.md](./interface.md) | The React SPA: the canvas/IR adapter, generated types, chat, bench, the run waterfall |
| [ir-and-packages.md](./ir-and-packages.md) | The IR document envelope, `contentHash`, validation, TS codegen, the gateway client |
| [deployment.md](./deployment.md) | Bare metal, Docker Compose, Kubernetes; CI gates; the sample OTel stack |
| [testing.md](./testing.md) | Test suites, fixtures, integration tests, lint gates |

Contributor rules — what must hold before a change lands — live in
[AGENTS.md](../../AGENTS.md) at the repo root and per app.
