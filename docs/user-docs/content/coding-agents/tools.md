---
description: Every tool TheYgent's MCP server gives a coding agent, prompts that work well, and a project-instructions snippet that teaches the agent when to use TheYgent.
---

# Tools & Recipes

Once [TheYgent is connected](index.md), your coding agent sees the tools below. You rarely name
them. Ask for the outcome and the agent picks the tools itself. Wherever a tool takes an agent or
a retrieval source, it accepts the id or the exact name you gave it.

## Models

| Tool | What it does |
|---|---|
| `list_models` | The models registered in TheYgent: logical id, engine, modality (chat, vision, embeddings…), and which chat model `ask_model` uses by default. |
| `ask_model` | Sends one prompt to a model and returns its answer. `files` attaches local text files: the MCP server reads them and sends them only to the model, so a long log or file never passes through the coding agent's own context or its provider. Only the answer comes back. Up to 256 KiB of files per call. `think: false` turns a reasoning model's thinking off for a faster answer. |

## Agents and runs

| Tool | What it does |
|---|---|
| `list_agents` | Your published agents, newest first. |
| `get_agent` | One agent's versions and full agent document. |
| `run_agent` | Runs a published agent, waits for the result, and returns its output and run id. Pin a `version`, or continue a chat `session_id`. |
| `run_graph` | Runs an agent document **without publishing it**. This is how the coding agent tests an agent it is building. |

`run_agent` and `run_graph` start ordinary runs, not [durable runs](../running/durable.md). An
agent that needs a human approval, a subgraph, a loop or a map runs durably, so start it from
the interface or the API instead.
| `get_run` | A run's status, output or error, by run id, including runs started elsewhere. |

## Building agents

| Tool | What it does |
|---|---|
| `agent_authoring_guide` | How to write an agent: the document format, how values flow, patterns, a full example, and every node type with its ports and defaults. Pass `node_types` to get the full config schema of specific types. |
| `validate_agent` | Checks a document against the same rules publishing applies, without running it, and names each problem. |
| `create_agent` | Publishes a new agent. It appears on your **Agents** page and can be opened in the editor. |
| `publish_agent_version` | Publishes a new version of an existing agent. Earlier versions stay runnable. |
| `list_samples` / `install_samples` | The ready-made [sample agents](../samples/index.md), and installing them with your models in their slots. |

Agents the coding agent builds are ordinary TheYgent agents. They follow
[versioning](../concepts/versioning.md), show up in the editor, can get
[triggers](../running/triggers.md), and their runs are traced on the **Runs** page.

## Retrieval

| Tool | What it does |
|---|---|
| `list_rag_sources` | Your [retrieval sources](../rag/index.md) and their status. |
| `create_rag_source` | Creates an `upload` source, or a `crawl` source for a website. Needs an embeddings model from `list_models`. |
| `add_rag_files` | Indexes local files, whole directories, or glob patterns such as `docs/**/*.md`. Relative paths resolve against the project the coding tool runs in. Hidden, dependency, and build directories are skipped. Each file is named by its path in the project, so two `index.ts` files stay two documents, and adding a file again replaces its earlier copy. One call indexes up to 500 files. |
| `add_rag_text` | Adds a document from text, such as notes, a generated summary, or an API response. |
| `ingest_rag_source` | Starts or re-runs a crawl source's crawl. |
| `get_rag_source` | Ingest progress and each document's status. |
| `search_rag` | Hybrid semantic and keyword search over a source. Returns the best passages with their documents and scores. |

Source code and config files index as plain text, next to the document formats listed on the
[RAG page](../rag/index.md#upload-sources).

## Recipes

Prompts that work well with Claude Code, Codex or OpenCode once TheYgent is connected:

**Offload a summary to a local model**

> Have TheYgent's local model read `logs/build.log` and summarize the failures in five bullets.
> Pass the log as a file, don't read it yourself. Use think=false.

**Draft with a private model**

> Write the release notes for the commits since the last tag. Draft them with TheYgent's local
> model, then tidy the draft yourself.

**Index the docs, then answer from them**

> Create a TheYgent retrieval source called `project-docs` with the `local-embed` model and add
> `docs/` and `README.md` to it. When it's ready, search it for how authentication is configured.

**Build a reusable agent**

> Build me a TheYgent agent called `commit-titler` that turns a change description into a
> conventional-commit title using my local model. Test it on two examples, then publish it.

**Use an agent you already have**

> Run my TheYgent agent `pii-firewall` on `fixtures/customers.csv` before you paste any of it here.

## Teach your coding agent when to use TheYgent

The MCP server tells the coding agent what it offers. A line in your project instructions turns
that into a habit. Add something like this to `CLAUDE.md` (Claude Code) or `AGENTS.md` (Codex,
OpenCode):

```markdown
## TheYgent

TheYgent's MCP tools are available. For summaries, first drafts and extraction over large or
private files, call `ask_model` with the paths in `files` (think=false for quick tasks) instead of
reading the files yourself. Search the `project-docs` retrieval source before guessing how something works here.
Check `list_agents` for an existing agent before writing a one-off script for a repeatable task.
```

## When something goes wrong

Every tool error says what to do next:

| Message says | Fix |
|---|---|
| `THEYGENT_API_KEY is not set` | Add your key to the server's environment, then restart the coding tool. |
| `rejected the API key (401)` | The key was revoked or mistyped. Create a new one under **API keys**. |
| `refused this action (403)` | Your role can't do this. Publishing agents needs **editor** or above. |
| `is not reachable at …` | TheYgent isn't running, or isn't at that address. Start it with `make up`, or set the plane URL variables. |
| `No chat model is registered` | Install a model from **Registries → Add model**. |
| `did not answer in time` | A run took longer than `THEYGENT_MCP_RUN_TIMEOUT_S`. Raise it, and the coding tool's own tool timeout. |

Errors from TheYgent itself pass through with their code, for example `agent_exists` when
publishing an id that is taken (use `publish_agent_version`), or `unsupported_document` for a
file type retrieval can't read.
