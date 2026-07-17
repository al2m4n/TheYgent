# Agents and graphs

In TheYgent, an agent *is* a graph: a set of steps (nodes) wired together (edges), plus everything those steps need to run. This page explains what that document contains and why the way you arrange it on the canvas has no effect on what it does.

## An agent is a graph document

Behind the visual canvas, every agent is a single JSON document. It describes a **graph** — nodes connected by edges — together with the models the agent may call, the tools it may use, and where each node sits on the screen. Running the agent means walking that graph from its input to its output.

You almost never write this document by hand. You build it in the [visual editor](../building/editor.md), dragging nodes and wiring ports; the editor keeps the JSON in sync and offers a raw **code view** as an escape hatch. But it helps to know the shape of what you are building, because the same document is what gets saved as a version, hashed for identity, and executed.

## What the document contains

The document is a small envelope of top-level fields. The keys are camelCase, and a typo in any of them is rejected rather than quietly ignored.

| Field | What it holds |
|---|---|
| `schemaVersion` | The document format version. The editor writes `"1.0"`. |
| `id` | The agent's stable identifier (for example `agent.greeter`). With `version`, it is the registry coordinate. |
| `name` | A human-readable display name. |
| `version` | This document's version string (for example `0.1.0`). |
| `models` | A map of model **key → binding** — the models this agent may call, each given a short local key that nodes reference. |
| `tools` | A map of tool **key → binding** — the built-in, HTTP, or MCP tools this agent may use. |
| `nodes` | The list of nodes: the steps of the agent. |
| `edges` | The list of edges wiring node ports together. |
| `view` | Canvas layout — node positions, viewport zoom, per-node icon overrides. Cosmetic only. |
| `contentHash` | A derived fingerprint of the document (see [Layout is cosmetic](#layout-is-cosmetic)). Filled in by the server when you publish. |
| `metadata` | Optional free-form data you can attach; ignored by execution. |

### Identity: id and version

An agent is identified by its `id`, and each published snapshot by its `version`. Together they form the coordinate the registry uses. When you publish an agent, that exact `(id, version)` pair becomes an immutable version — see [Drafts & publishing](../building/saving-agents.md) and [Versioning](versioning.md).

### The models map

Rather than naming a model inside every node, an agent declares the models it may call once, in the `models` map, and nodes reference them by key. Each entry is a **binding**: which engine family serves the model (`mlx`, `vllm`, `llamacpp`, or `openai-compatible`), the logical model id to ask for, and optional generation parameters.

There are two levels of naming here, and it is worth keeping them straight:

- The **map key** (for example `responder`) is a local alias, meaningful only inside this one agent. Nodes reference this key.
- The binding's **`model`** field (for example `chat-small`) is the *logical model id* registered on the inference plane — never an engine name. Which engine or endpoint actually serves it is a registration detail you can change without touching the agent.

See [Models and engines](models-and-engines.md) for how logical ids, bindings, and sources fit together.

### The tools map

Tools are declared the same way: once in the `tools` map, then referenced by key from `tool` and `mcp_tool` nodes. A binding is one of three kinds — a built-in tool, an HTTP call, or an MCP tool. Crucially, **secrets never live in the document**: an HTTP or MCP tool references a saved connection by id, and the credential is resolved on the server at run time. See the [tools node reference](../building/nodes/tools.md).

### The nodes and edges

The `nodes` and `edges` lists are the graph itself — the steps and the wires between them. This is where the agent's behaviour actually lives. The next page, [Nodes, ports, and edges](nodes-ports-edges.md), covers the node kinds, the ports they connect through, and how values flow.

### The view

The `view` block records where each node sits, the current zoom, and any custom icons you picked. It exists purely so the canvas looks the same when you reopen the agent. Nothing in it affects execution.

## Layout is cosmetic

An agent's identity is a content hash — a `sha256:` fingerprint the server computes over the document. Before hashing, the layout (`view`) is stripped out, defaults are filled in, and keys are sorted. The consequence is simple and worth relying on:

- **Rearranging the canvas never creates a new version.** Dragging a node, zooming, collapsing a node, or changing its icon changes only the `view` block, so the hash is unchanged.
- **Only real content changes the hash** — editing a message, a config value, a model binding, or an edge.
- Formatting and key order of the JSON never matter, and omitting a field that has a default hashes the same as writing that default explicitly.

So two people can lay out the same agent completely differently and still have, byte-for-byte in what counts, the *same* agent. See [Versioning](versioning.md) for how the hash underpins immutable versions.

## A complete example

A minimal agent that sends its input to a model and returns the answer looks like this. In the editor you would build it by dropping an `input`, an `llm`, and an `output` node and wiring them left to right.

??? example "The full document"
    ```json
    {
      "schemaVersion": "1.0",
      "id": "agent.greeter",
      "name": "Greeter",
      "version": "0.1.0",
      "models": {
        "responder": { "binding": "llamacpp", "model": "chat-small" }
      },
      "nodes": [
        {
          "id": "n_in",
          "type": "input",
          "kind": "boundary",
          "config": {},
          "ports": { "in": [], "out": [{ "id": "out" }] }
        },
        {
          "id": "n_llm",
          "type": "llm",
          "kind": "activity",
          "config": {
            "model": "responder",
            "messages": [{ "role": "user", "content": "$in" }]
          },
          "ports": {
            "in": [{ "id": "in" }, { "id": "tools", "role": "tool", "required": false }],
            "out": [{ "id": "out" }]
          }
        },
        {
          "id": "n_out",
          "type": "output",
          "kind": "boundary",
          "config": {},
          "ports": { "in": [{ "id": "in" }], "out": [] }
        }
      ],
      "edges": [
        { "id": "e_in_llm", "source": "n_in", "sourceHandle": "out", "target": "n_llm", "targetHandle": "in" },
        { "id": "e_llm_out", "source": "n_llm", "sourceHandle": "out", "target": "n_out", "targetHandle": "in" }
      ],
      "view": {
        "nodes": {
          "n_in": { "position": { "x": 0, "y": 0 } },
          "n_llm": { "position": { "x": 260, "y": 0 } },
          "n_out": { "position": { "x": 520, "y": 0 } }
        }
      }
    }
    ```

A few things to notice, all covered in more depth elsewhere:

- The `llm` node references the model by its map key (`responder`), not by the logical id (`chat-small`) or an engine name.
- The message content is `$in` — a reference to the value arriving on the node's input. This is the [input reference language](../building/input-references.md).
- Every node carries a `type` and a `kind`; the `input` and `output` nodes are the graph's boundary, and the `llm` node is the work in the middle. See [Nodes, ports, and edges](nodes-ports-edges.md).

## Creating and running agents

A brand-new graph in the editor already contains an `input` node wired to an `output` node, so it validates and runs (it simply echoes its input) before you have changed anything. From there you add nodes, wire them, and configure them in the inspector — your work autosaves as a draft, and the editor's Test panel runs the graph in place at any point. When it is ready, publish it to the registry to mint an immutable version. See:

- [The editor](../building/editor.md) — the canvas, palette, and inspector
- [Drafts & publishing](../building/saving-agents.md) — turning a graph into a named version
- [Runs and sessions](runs-and-sessions.md) — what happens when an agent runs

## Related pages

- [Nodes, ports, and edges](nodes-ports-edges.md) — the graph's building blocks
- [Models and engines](models-and-engines.md) — bindings, logical ids, and sources
- [Versioning](versioning.md) — content hashing and immutable versions
- [Node reference](../building/nodes/index.md) — every node type in detail
