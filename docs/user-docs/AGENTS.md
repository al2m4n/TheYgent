# AGENTS.md — user docs

Rules for `docs/user-docs`, the end-user documentation site (MkDocs Material, published
versioned per release to https://docs.theygent.ai/). The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first.

- **Strict mode is non-negotiable:** `make docs-build` runs `mkdocs build --strict` —
  orphan pages and broken links fail. Every new page needs a `nav:` entry in `mkdocs.yml`.
- **Audience is users, not contributors.** Pages describe what the product does — never
  module paths or internal architecture beyond the user-visible plane split. Contributor
  material belongs in [docs/dev-docs](../dev-docs/README.md).
- **These pages are published:** no competing product names, no internal plan references.
  Brand casing is "TheYgent" in prose and UI copy — the GitHub repo is `al2m4n/TheYgent`
  and the webhook header is `X-TheYgent-Signature`; lowercase `theygent` only in code
  identifiers, env vars, and domains (`theygent.ai`).
- **Honesty rules mirror the product:** never overclaim privacy ("everything runs where
  you point it, and you own every hop" — not "no data ever leaves your machine"); label
  unverified paths (e.g. vLLM as experimental); document limitations plainly.
- **Examples are executable documentation:** curl examples use real endpoint paths, the
  default ports (:8080 control, :8081 inference, :5174 interface), and real error codes.
- **When product behavior changes, the matching page changes in the same PR** — the page
  map under `content/` is organized by feature area (getting-started, concepts, models,
  chat, rag, mcp, running, reference).
- This project is deliberately **not** part of the root uv workspace — its own lockfile
  isolates the docs toolchain. `make docs-serve` for live preview. Releases publish with
  `mike`; published versions are immutable and `latest` tracks the newest release.

## Map: which page documents which code

<!-- agents-map: covers=content -->

| Section (`content/…`) | Documents | Code it tracks |
|-----------------------|-----------|----------------|
| `index.md`, `assets/` | The home page; logos and favicon | Brand, the product overview |
| `getting-started/` | Install, run, first chat, first agent | `Makefile`, `docker-compose.yml`, `deploy/`, `apps/interface` first-run screens |
| `concepts/` | Agents and graphs, nodes/ports/edges, models and engines, runs and sessions, versioning, privacy | `packages/ir`, the plane split, the agent registry |
| `building/` (+ `nodes/`, one page per node family) | The editor, `$in` references, every node type | `apps/interface` editor, `packages/ir/graph.py`, `walker.py` |
| `models/` | Engines, installing from the catalog, remote (openai-compatible) models | `apps/inference-plane` |
| `chat/` | Chat, vision and images, voice | `apps/interface/src/chat`, inference `/v1/*` |
| `rag/` | Retrieval sources | `apps/control-plane/.../rag` |
| `mcp/` | MCP servers, the hub, Apify crawlers, examples | `apps/control-plane/.../mcp`, `.../crawlers` |
| `running/` | Triggers, durable runs, human in the loop, bench, observability, users | Triggers, `durable/`, `observability/`, `auth.py`, `src/bench` |
| `ai-gateway/` | The AI gateway: setup, running, local models, reports | `apps/ai-gateway`, the control plane's `ai_gateway/` |
| `coding-agents/` | The MCP server for coding tools and its tool list | `apps/mcp-server` |
| `samples/` | The shipped sample agents | `apps/control-plane/.../samples` |
| `import-export/` | Moving an install between machines | `transfer.py` (both planes), `src/lib/transfer.ts` |
| `reference/` | HTTP API, environment variables, settings, glossary, troubleshooting | Every route, every env read, the settings catalogs |

Other files: [`mkdocs.yml`](mkdocs.yml) (nav, theme, hooks), [`hooks/llms_full.py`](hooks/llms_full.py)
(writes `llms-full.txt`), [`root/`](root) (domain-root files copied into every publish),
[`README.md`](README.md) (publishing details).
