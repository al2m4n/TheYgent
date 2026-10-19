# AGENTS.md — MCP server

Rules specific to `apps/mcp-server`. The repo-wide rules in the root
[AGENTS.md](../../AGENTS.md) apply first; the component's design is documented in
[docs/dev-docs/mcp-server.md](../../docs/dev-docs/mcp-server.md).

## What it is

An MCP server that gives coding agents (Claude Code, Codex, OpenCode, any MCP client) TheYgent's
platform as tools: build, test and run agents, ask local models, and search retrieval sources.
It runs on the developer's machine next to the coding tool — over stdio by default, or
streamable HTTP — and is a **client** of both planes, exactly like the interface:

- **Control plane** (`THEYGENT_CONTROL_PLANE_URL`, default `http://127.0.0.1:8080`) for agents,
  runs, retrieval sources and samples, authenticated with a TheYgent API key
  (`THEYGENT_API_KEY`, `Authorization: Bearer`).
- **Inference plane** (`THEYGENT_INFERENCE_PLANE_URL`, default `http://127.0.0.1:8081/v1`)
  directly for model calls — prompts never route through the control plane.

It holds no state. Other knobs: `THEYGENT_MCP_DEFAULT_MODEL` (the `ask_model` default),
`THEYGENT_MCP_RUN_TIMEOUT_S`.

## Where it sits

- **Launched by:** the coding tool, as `uv run --project <checkout> --package theygent-mcp
  theygent-mcp` (`make mcp-setup` prints the per-client setup); `--http` serves streamable HTTP
  on `127.0.0.1:8083`.
- **Calls:** the [control plane](../control-plane/AGENTS.md) (`/agents*`, `/graphs/runs`,
  `/runs/{id}`, `/samples*`, `/rag/sources*`) and the
  [inference plane](../inference-plane/AGENTS.md) (`/v1/chat/completions`, `/admin/models`).
- **Shares:** `theygent_ir` from [packages/ir](../../packages/ir/AGENTS.md) (local
  `parse_document` + `validate_graph`, `EXECUTABLE_TYPES`, `NODE_TYPE_KIND`) and the generated
  `packages/ir-types/src/node-types.json` (each type's ports and config schema for the
  authoring guide). A node type added to the IR shows up in the guide with no change here.
- **Documented for users in** `docs/user-docs/content/coding-agents/` (the tool list lives in
  `tools.md`). A tool added, renamed or removed changes that page in the same PR.

## Map

<!-- agents-map: covers=src/theygent_mcp -->

All paths under [`src/theygent_mcp/`](src/theygent_mcp):

| Module | What it does |
|--------|--------------|
| [`server.py`](src/theygent_mcp/server.py) | `build_server`: the FastMCP server and its twenty tools — models (`list_models`, `ask_model` with inlined local files), agents (`list_agents`, `get_agent`, `run_agent`, `run_graph`, `get_run`, `agent_authoring_guide`, `validate_agent`, `create_agent`, `publish_agent_version`), samples (`list_samples`, `install_samples`), retrieval (`list_rag_sources`, `search_rag`, `create_rag_source`, `add_rag_files`, `add_rag_text`, `ingest_rag_source`, `get_rag_source`) |
| [`planes.py`](src/theygent_mcp/planes.py) | HTTP access to both planes; failures become `ToolError`s that say what to do next, passing a plane's error code through |
| [`guide.py`](src/theygent_mcp/guide.py) | The agent-authoring guide text, the node-type registry read from the IR and `node-types.json`, local validation |
| [`config.py`](src/theygent_mcp/config.py) | `Config` from the environment (empty means unset) |
| [`__main__.py`](src/theygent_mcp/__main__.py) | `theygent-mcp` (serve over stdio or `--http`) and `theygent-mcp setup [claude\|codex\|opencode\|all]` |
| [`tests/test_server.py`](tests/test_server.py) | An in-memory MCP client against fake planes (httpx `MockTransport`) |

## Rules

- **Only public HTTP APIs.** The server calls the planes' documented endpoints; it never imports
  `theygent_control_plane` or `theygent_inference_plane`, and never touches their databases or
  state directories. `theygent_ir` is the one shared package (validation and the node registry).
- **Tool errors are actionable.** Every failure becomes a `ToolError` whose message says what to
  do next (start the stack, set `THEYGENT_API_KEY`, fix the named IR field). A plane's error
  envelope `{"error": {"message", "code"}}` is passed through with its code.
- **Tools stay few and composable.** A new tool needs a real coding-agent use case, a docstring
  written for a model (when to use it, what it returns), and a fast-suite test.
- **Empty env means unset**, as everywhere in the repo.

## Tests

```bash
uv run --package theygent-mcp pytest     # in-memory MCP client against fake planes (httpx MockTransport)
```
