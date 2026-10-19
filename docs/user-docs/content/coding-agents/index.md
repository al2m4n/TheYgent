---
description: Connect Claude Code, Codex, OpenCode or any MCP client to TheYgent, so your coding agent can offload work to your local models, run and build TheYgent agents, and search your documents and code.
---

# Use TheYgent from Coding Agents

TheYgent ships an **MCP server** that hands your coding agent the platform as tools. Add it to
Claude Code, Codex, OpenCode or any other MCP client, and the coding agent can:

- **Offload work to your local models.** Summarize a long file, draft a commit message or
  release notes, classify or extract data, or get a second opinion. The work runs on the model
  you pick. With a local MLX or llama.cpp model, the content stays on your machine and costs
  nothing per token.
- **Run your TheYgent agents.** Anything you have built and published, such as a triage
  router, a PII firewall or a review loop, becomes one tool call away.
- **Build new agents for you.** Describe a repeatable job and the coding agent writes the agent,
  checks it, test-runs it, and publishes it. It then appears on your **Agents** page like
  any agent you built in the editor.
- **Search your documents and code.** The coding agent can index a project's docs, specs or
  source into a [retrieval source](../rag/index.md), then search it with the same hybrid
  retrieval your agents use.

!!! note "Two directions of MCP"
    The [MCP](../mcp/index.md) section is about TheYgent *using* MCP servers inside agents. This
    page is the other direction: **TheYgent as an MCP server** that other tools use.

## Before you start

- **TheYgent is running.** Use `make up` or Docker Compose (see [Running TheYgent](../getting-started/running.md)).
  The MCP server talks to the control plane on `:8080` and the inference plane on `:8081`.
- **At least one chat model is registered** (see [Installing local models](../models/installing.md)). Add an
  embeddings model too if you want the coding agent to index and search documents.
- **`uv` is on your `PATH`.** The coding tool starts the MCP server from your TheYgent checkout
  with `uv run`.
- **A personal API key.** In TheYgent, open the profile menu at the bottom of the sidebar and
  choose **API keys → Create**. Copy the `tyk_…` key; it is shown only once. The key acts with your
  role, and creating agents needs **editor** or above (see [Users, roles & sign-in](../running/users.md#personal-api-keys)).

## Print your setup commands

From your TheYgent checkout:

```bash
make mcp-setup
```

It prints a ready-to-paste setup for each tool below, with your checkout's path already filled in.
Replace `<your TheYgent API key>` with your key.

## Claude Code

```bash
claude mcp add theygent --scope user \
  -e 'THEYGENT_API_KEY=tyk_…your key' \
  -- uv run --project /path/to/TheYgent --package theygent-mcp theygent-mcp
```

`--scope user` makes TheYgent available in every project. Leave it out to add it only to the
current project. Check the connection with `claude mcp list`, or run `/mcp` inside a session.
Then ask in plain words, for example *"use TheYgent's local model to summarize this log"*. Claude
picks the tools itself.

Agent runs on a local model can take a few minutes. If a long run is cut off, raise Claude Code's
MCP tool timeout, for example `export MCP_TOOL_TIMEOUT=600000` (milliseconds) before starting `claude`.

## Codex

```bash
codex mcp add theygent --env 'THEYGENT_API_KEY=tyk_…your key' \
  -- uv run --project /path/to/TheYgent --package theygent-mcp theygent-mcp
```

Codex stops a tool call after 60 seconds by default, which is too short for agent runs on a
local model. Give it room in `~/.codex/config.toml`:

```toml
[mcp_servers.theygent]
command = "uv"
args = ["run", "--project", "/path/to/TheYgent", "--package", "theygent-mcp", "theygent-mcp"]
tool_timeout_sec = 600
env = { THEYGENT_API_KEY = "tyk_…your key" }
```

## OpenCode

Add TheYgent to `opencode.json`, either in the project or in `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "theygent": {
      "type": "local",
      "command": ["uv", "run", "--project", "/path/to/TheYgent", "--package", "theygent-mcp", "theygent-mcp"],
      "enabled": true,
      "environment": { "THEYGENT_API_KEY": "tyk_…your key" }
    }
  }
}
```

Check it with `opencode mcp list`. It should show `theygent` as connected.

## Other MCP clients

Any client that can launch a stdio MCP server works with the same command and environment.
For a client that only speaks HTTP, serve streamable HTTP instead:

```bash
THEYGENT_API_KEY='tyk_…your key' \
  uv run --package theygent-mcp theygent-mcp --http --port 8083
```

Then point the client at `http://127.0.0.1:8083/mcp`. The HTTP mode binds to `127.0.0.1` by
default. Anyone who can reach that port acts with your API key, so don't expose it beyond your
machine.

## Settings

The MCP server reads its settings from the environment the coding tool starts it with:

| Variable | Default | What it does |
|---|---|---|
| `THEYGENT_API_KEY` | — | Your personal API key. Required by every agent, run, sample and retrieval tool. The model tools work without it. |
| `THEYGENT_CONTROL_PLANE_URL` | `http://127.0.0.1:8080` | Where the control plane is. |
| `THEYGENT_INFERENCE_PLANE_URL` | `http://127.0.0.1:8081/v1` | The inference plane's OpenAI-compatible root, ending in `/v1`. |
| `THEYGENT_MCP_DEFAULT_MODEL` | first chat model | The model `ask_model` uses when the coding agent doesn't name one. |
| `THEYGENT_MCP_RUN_TIMEOUT_S` | `600` | How long agent runs and model calls may take, in seconds. |

Pass extra variables the same way as the key: `-e` for Claude Code, `--env` for Codex, or
`environment` for OpenCode.

## What stays where

Model calls (`ask_model`) go straight from the MCP server to the inference plane on your machine.
Prompts never pass through the control plane. Files given to `ask_model` as `files` are read by
the MCP server and sent only to the model you chose. The coding agent and its provider see the
answer, not the file. Agent runs and retrieval searches go through the
control plane, as they do from the interface, and show up on the **Runs** page with their full trace.
The coding tool sees only what the tools return. See [Privacy & data flow](../concepts/privacy.md).

## Next

- [Tools & recipes](tools.md) lists every tool, gives prompts that work well, and has a snippet that
  teaches your coding agent when to reach for TheYgent.
