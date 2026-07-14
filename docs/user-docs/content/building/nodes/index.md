# Node reference

An agent is a graph of **nodes** wired together by edges. Each node does one job — take an input, call a model, run a tool, choose a branch — and passes its result to the next. This page is the map: every node type, what it does, and where to read the details.

If you are new to graphs, start with [Nodes, ports and edges](../../concepts/nodes-ports-edges.md) for the underlying model, then come back here to look up a specific node. The [editor tour](../editor.md) shows where these nodes live on screen.

## The palette, grouped by kind

Every node has a **kind** — one of three fixed categories that decide how it behaves and where it sits in the editor's left-hand palette:

| Kind | Palette group | What these nodes do |
|---|---|---|
| `boundary` | **I/O & Human** | Enter, leave, or pause the graph. |
| `activity` | **Compute & Tools** | Do real work with side effects — call a model, a tool, an engine. |
| `orchestration` | **Control flow** | Steer the run deterministically — branch, reshape, gate. |

The palette derives its list directly from the set of runnable node types, so the tables below are exhaustive. Search and category chips at the top of the palette filter it; groups are collapsible.

## I/O & Human (boundary)

| Type | Purpose | Reference |
|---|---|---|
| `input` | The agent's entry point — carries the run input into the graph. | [Input & output](input-output.md) |
| `output` | The agent's exit — the value that reaches it becomes the run result. | [Input & output](input-output.md) |
| `human` | Pauses the run for a person to supply input or approve. Durable-only. | [Human approval](human.md) |
| `subgraph` | Runs another saved agent as a single step. Durable-only. | [Subgraph, loop & map](orchestration.md) |

## Compute & Tools (activity)

| Type | Purpose | Reference |
|---|---|---|
| `llm` | Calls a language model with templated messages; optional tool-calling and streaming. | [LLM](llm.md) |
| `tool` | Runs a built-in tool or an HTTP API — as a pipeline step or a model capability. | [Tools](tools.md) |
| `mcp_tool` | Calls a tool exposed by a connected MCP server. | [MCP tools](mcp.md) |
| `rag` | Retrieves the most relevant passages from a RAG source — as a step or a model capability. | [RAG](rag.md) |
| `transcribe` | Speech-to-text: turns audio into text. | [Audio & images](media.md) |
| `speak` | Text-to-speech: turns text into audio. | [Audio & images](media.md) |
| `imagine` | Generates an image from a text prompt. | [Audio & images](media.md) |
| `ratelimit` | Allows or denies a request by a request-count limit per key per window. | [Rate limit & quota](ratelimit-quota.md) |
| `quota` | Allows or denies a request by a token budget per key per window. | [Rate limit & quota](ratelimit-quota.md) |

!!! note "The Tool node has three kinds"
    `mcp_tool` is not dragged from the palette — it is the **MCP** kind of the one Tool node, chosen in the inspector after you drop a Tool. The other two kinds are a built-in tool and an HTTP call. See [Tools](tools.md) and [MCP tools](mcp.md).

## Control flow (orchestration)

| Type | Purpose | Reference |
|---|---|---|
| `router` | Sends the run down exactly one named branch, chosen at runtime. | [Router](router.md) |
| `transform` | Reshapes a value with a deterministic template — no model, no I/O. | [Transform](transform.md) |
| `guardrail` | Checks a value and routes it to a `pass` or `block` port. | [Guardrail](guardrail.md) |
| `loop` | Runs a saved agent repeatedly, feeding each output into the next. Durable-only. | [Subgraph, loop & map](orchestration.md) |
| `map` | Runs a saved agent once per list element, optionally in parallel. Durable-only. | [Subgraph, loop & map](orchestration.md) |

!!! info "The `guardrail` exception"
    A guardrail's kind depends on how you configure it: an inline rule check is `orchestration`, while a model-judge check is an `activity` (it makes a real model call). The editor sets the right kind for you — you never touch it. In the palette it lives under **Control flow**. See [Guardrail](guardrail.md).

## Durable-only nodes

Four node types run **only** on the durable runtime, because they wait, recurse, or fan out over time: `human`, `subgraph`, `loop`, and `map`. A graph that contains any of them cannot run on the plain interactive path — save it, then use **Run durably** (or deploy it behind a trigger). The editor marks these nodes with a "Durable-only" banner. See [Durable runs](../../running/durable.md).

## Reading a node on the canvas

Each node card shows an icon, a label (falling back to the node id), and its `type` in small caps. A colored dot and border encode the kind: **green** for boundary, **blue** for activity, **amber** for orchestration.

Nodes connect through **handles** on their edges:

- **Data handles** are round, on the left (in) and right (out) sides. A required in-port handle is blue; an optional one is gray. An error out-port (`err`) is red.
- **Control handles** are amber squares on the top and bottom — pure sequencing ("run after this"), carrying no value.
- **Tool handles** are violet squares — they wire a tool node's capability into a model. See [LLM](llm.md).

You can only connect like to like (data to data, control to control, tool to tool). More on this in [The editor](../editor.md).

## What isn't here

A handful of type ids exist in the underlying schema but do not run yet and never appear in the palette: `agent`, `retriever`, `memory`, `code`, `condition`, and `iterator`. If you hand-write raw IR that uses one, the control plane refuses to run it. Build with the runnable types above.

## Related pages

- [Nodes, ports and edges](../../concepts/nodes-ports-edges.md) — the model behind every node
- [The editor](../editor.md) — the palette, canvas, and inspector in practice
- [Referencing inputs](../input-references.md) — the `$in` templating grammar every node shares
- [Saving agents](../saving-agents.md) — turn a graph into an immutable, runnable version
