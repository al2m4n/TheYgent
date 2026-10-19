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
