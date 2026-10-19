# AGENTS.md — dev docs

Rules for `docs/dev-docs`, the contributor documentation: the architecture and each component's
design, invariants and reasoning. The repo-wide rules in the root [AGENTS.md](../../AGENTS.md)
apply first. These pages are plain Markdown read on GitHub (no site build); [README.md](README.md)
is the index.

## How these pages relate to the AGENTS.md files

- **AGENTS.md files** (one per module, linked from the root) are the *code maps*: what each file
  does, who calls whom, the local rules, how to make a change. They are read by coding agents
  working in that directory and must stay precise at file level.
- **Dev docs** are the *design narrative*: why the boundaries sit where they do, how a run flows,
  what was deliberately not built. Some pages carry a layout table too; when a module is added,
  moved or renamed, update both.
- Neither repeats the user docs. Behavior a user sees belongs in
  [docs/user-docs](../user-docs/AGENTS.md).

## Map

<!-- agents-map: covers=. -->

| Page | Covers | Code |
|------|--------|------|
| [architecture.md](architecture.md) | The two-plane split, the seams, process topology, monorepo layout, hard-to-reverse decisions | Whole repo |
| [control-plane.md](control-plane.md) | The orchestration service: HTTP surface, schema, stores, MCP, RAG, observability, retention, transfer, config | `apps/control-plane` |
| [graph-execution.md](graph-execution.md) | The walker, the node set, `$in`, edge liveness, tool capabilities, gates | `walker.py` |
| [durable-execution.md](durable-execution.md) | The DBOS workflow, durable-only nodes, schedules, the worker | `durable/`, `apps/worker` |
| [inference-plane.md](inference-plane.md) | Bindings and modalities, the two surfaces, engine lifecycle, catalog, settings | `apps/inference-plane` |
| [ai-gateway.md](ai-gateway.md) | The coding agents' proxy: pass-through, identity, policy, router mode, local models, shadows, prices, records | `apps/ai-gateway`, control plane `ai_gateway/` |
| [mcp-server.md](mcp-server.md) | TheYgent as MCP tools for coding agents | `apps/mcp-server` |
| [interface.md](interface.md) | The SPA: routes, the IR/React Flow adapter, generated types, the HTTP seam, drafts, chat, bench, waterfall | `apps/interface` |
| [ir-and-packages.md](ir-and-packages.md) | The IR envelope and `contentHash`, validation, codegen, the gateway client | `packages/*` |
| [deployment.md](deployment.md) | Bare metal, compose, Kubernetes, CI gates, publishing | `deploy/`, `.github/`, `Makefile` |
| [testing.md](testing.md) | Suites, fixtures, integration tests, lint gates | Every `tests/` |

## Rules

- **Architectural or contract changes update the matching page in the same PR.** So do new
  components, new seams, and changes to a flow these pages narrate.
- **Present tense, no history.** Describe the system as it is; no milestone names, plan
  references or "previously". Same rules as code comments.
- **No competitor product names** (the marketing site's comparison pages are the one exception,
  and they live in `apps/web`).
- **Links are relative and must resolve.** A new page is listed in [README.md](README.md) and in
  the root README's developer-docs table.
