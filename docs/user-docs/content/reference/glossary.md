# Glossary

The terms used across TheYgent and this documentation, in alphabetical order. Where a concept has its own page, the definition links to it.

### Activity

One of the three node [kinds](#kind). Activity nodes do non-deterministic work with side effects — calling a model (`llm`), running a tool (`tool`, `mcp_tool`), transcribing audio, generating an image. See [Nodes, ports & edges](../concepts/nodes-ports-edges.md).

### Agent

A published, runnable AI workflow: a [graph](#graph) of [nodes](#node) plus its model and tool [bindings](#binding), published under an id. You build agents in the [editor](../building/editor.md) (where work-in-progress lives as a [draft](#draft)) and run them from the Bench, from chat, or behind a [trigger](#trigger). See [Agents & graphs](../concepts/agents-and-graphs.md).

### Artifact

A blob of generated media — audio or an image — stored on local disk and passed between steps **by reference** (an `art_…` id) rather than inline. A voice agent's spoken reply and an image node's output are artifacts. Bytes stay in your trust domain and are never journaled into a run's history. See [Voice](../chat/voice.md).

### Binding

How a logical model maps to something that can actually answer a request. The binding value is one of four: `mlx`, `vllm`, `llamacpp` (local engines TheYgent manages), or `openai-compatible` (any reachable OpenAI-style server or hosted API). See [Models & engines](../concepts/models-and-engines.md).

### Boundary

One of the three node [kinds](#kind): the entry, exit, and wait points of a graph — `input`, `output`, `human`, `subgraph`. See [Nodes, ports & edges](../concepts/nodes-ports-edges.md).

### Capability

A property of a model, probed from the engine: whether it supports tool-calling, structured output, vision, or reasoning, and its maximum context length. Capabilities drive which controls appear on the Bench and which badges show in the [Registries](../models/index.md) page.

### Channel

What an [edge](#edge) carries between [ports](#port). `data` passes a value; `control` is pure sequencing (run-after, no value); `tool` is a capability wire connecting a tool node to an `llm` so the model may call it. See [Nodes, ports & edges](../concepts/nodes-ports-edges.md).

### Connection

A named, reusable credential for a tool or MCP server — kind `http_auth` or `mcp_server`. The non-secret config is stored plainly; the secret is encrypted, write-only, and never appears in the graph. Remote-server auth types: `bearer`, `api_key`, `basic`, `headers` (a whole header map), `oauth2_client_credentials`, or `oauth` (interactive sign-in, tokens stored encrypted behind the same secret reference). Everything the MCP page creates — hub installs, remote, OpenAPI/GraphQL, stdio-with-secrets — is a connection. See [Tools](../building/nodes/tools.md) and [MCP servers](../mcp/index.md).

### Content hash

The `sha256:` fingerprint that gives an agent version its identity, computed over the graph with layout stripped and defaults filled. Rearranging the canvas, zooming, or changing a node's icon never changes it; changing a message, config value, or edge does. See [Agent versioning](../concepts/versioning.md).

### Control plane

The hosted orchestration service (default `http://localhost:8080`) that owns agents, runs, sessions, triggers, connections, artifacts, and observability, backed by your Postgres. It reaches the [inference plane](#inference-plane) over HTTP only and never runs models itself. See [Architecture](../concepts/index.md).

### Draft

The editor's autosaved work-in-progress: a mutable snapshot of the graph you are building, saved server-side as you edit. A draft may be structurally invalid, has no [content hash](#content-hash), and can't be run by reference or pinned — publishing graduates its content into an immutable [version](#version) and removes the draft. Drafts live in the **Drafts** strip on the Agents page. See [Drafts & publishing](../building/saving-agents.md).

### Durable run

A run executed on the durable runtime, which checkpoints each completed step so the run survives a crash and resumes where it left off. Durable mode also unlocks the `human`, `subgraph`, `loop`, and `map` node types. See [Durable runs](../running/durable.md).

### Edge

A wire connecting one node's out-[port](#port) to another's in-port, carrying a [channel](#channel). At most one `data` edge may feed a given in-port. See [Nodes, ports & edges](../concepts/nodes-ports-edges.md).

### Embedding model

A model that turns text into a vector so that similar meanings land near each other — the model behind retrieval. Each [RAG source](#rag-source) pins one embedding model (a [logical id](#logical-model-id)) at creation, because vectors from different models are not comparable. See [RAG sources](../rag/index.md).

### Engine

The underlying server program that runs a model — `llama.cpp`, an MLX server, `whisper.cpp`, vLLM, or an image-generation CLI wrapper. TheYgent lazily spawns and supervises managed engines; you never name an engine when calling a model. See [Engines](../models/engines.md).

### Generated server

An MCP server TheYgent mints in-process from an API you already have: an OpenAPI spec (every operation becomes a callable tool) or a GraphQL endpoint (two tools — schema introspection and a validated query runner). No subprocess; only the upstream API calls leave the process, carrying auth resolved server-side. See [MCP servers](../mcp/index.md).

### Graph

The document that defines an [agent](#agent): a `camelCase` JSON envelope holding the model and tool [bindings](#binding), a list of typed [nodes](#node), and the [edges](#edge) wiring them, plus a never-hashed `view` block for canvas layout. Also called the IR (intermediate representation). See [Agents & graphs](../concepts/agents-and-graphs.md).

### Guardrail

A node that checks its input against a rule (regex, length, allow/deny list, JSON-shape, PII) or an LLM judge, then routes to a `pass` or `block` port. Wire `block` to an output to refuse a request before doing expensive work. See [Guardrail](../building/nodes/guardrail.md).

### Hub (MCP registry)

A catalog of published MCP servers you can browse and install from: the Official MCP Registry, the GitHub MCP Registry, or a self-hosted registry added via `THEYGENT_MCP_REGISTRIES` (which doubles as an allowlist for air-gapped setups). Installing an entry creates an `mcp_server` [connection](#connection) stamped with its origin (registry + version). See [MCP servers](../mcp/index.md).

### Hybrid search

How a [RAG source](#rag-source) is searched: semantic (vector) similarity fused with keyword full-text ranking, so paraphrases and exact identifiers both surface. Runs entirely inside your Postgres. See [RAG sources](../rag/index.md).

### Inference plane

The user-controlled service (default `http://localhost:8081`) that registers models under logical ids, spawns engines, and answers OpenAI-compatible requests. It runs wherever you point it and keeps its registry and weights on your machine. See [Architecture](../concepts/index.md).

### I/O capture

The policy governing whether a run's per-node input/output payloads are recorded for the observability waterfall: `off`, `metadata` (byte sizes only), or `full`. Capped by an environment ceiling. See [Observability](../running/observability.md).

### Kind

The category of a [node](#node): `activity`, `orchestration`, or `boundary`. Every node type has exactly one correct kind (the `guardrail` is the one exception, whose kind follows its check type). See [Nodes, ports & edges](../concepts/nodes-ports-edges.md).

### Logical model id

The name you give a model when you register it (for example `triage-fast`) and the only thing you ever put in a `model` field. It hides the engine and weights behind it, so swapping a local model for a hosted one is one registration change with no graph edits. See [Models & engines](../concepts/models-and-engines.md).

### MCP (Model Context Protocol)

An open protocol for exposing external tools to models. TheYgent connects to MCP servers — spawned locally over `stdio`, reached remotely over HTTP or SSE, or [generated in-process](#generated-server) from an OpenAPI spec or GraphQL endpoint — and lets your agents call the tools they expose through the `mcp_tool` node. Servers are added by hand or installed from a [hub](#hub-mcp-registry). See [MCP servers](../mcp/index.md) and [MCP tools](../building/nodes/mcp.md).

### Modality

What kind of input/output a model handles: `chat`, `vision`, `embeddings`, `audio.transcription`, `audio.speech`, or `images.generation`. A model is registered for one modality; it is a third axis orthogonal to [binding](#binding) and [source](#source). See [Models & engines](../concepts/models-and-engines.md).

### Node

One step in a [graph](#graph): a typed box with a config, in-[ports](#port), and out-ports. Seventeen types are executable, across the three [kinds](#kind). See the [node reference](../building/nodes/index.md).

### Orchestration

One of the three node [kinds](#kind): deterministic control flow — `router`, `transform`, `loop`, `map`. See [Nodes, ports & edges](../concepts/nodes-ports-edges.md).

### Port

A named connection point on a [node](#node), on the in or out side, carrying a [channel](#channel) (`data`, `control`, or `tool`). A required in-port must be fed by exactly one data edge for the graph to validate. See [Nodes, ports & edges](../concepts/nodes-ports-edges.md).

### Publish

The deliberate act that turns the graph on the canvas into an immutable, content-addressed [version](#version) in the registry — visible to everyone who can reach the control plane, runnable by reference, and pinnable by triggers and composing agents. Contrast with the automatic [draft](#draft) tier. See [Drafts & publishing](../building/saving-agents.md).

### RAG source

A named document collection agents retrieve from — filled by uploading files or crawling a site, chunked and embedded against its pinned [embedding model](#embedding-model), stored in your Postgres, and searched by the [rag node](../building/nodes/rag.md). Referenced by a stable id, so re-ingesting never changes an agent's version. See [RAG sources](../rag/index.md).

### Run

One execution of an agent or graph. It moves through statuses `created → streaming → waiting → completed | failed`, persists in Postgres, and survives restarts. Its output is the value that reached the `output` node. See [Runs & sessions](../concepts/runs-and-sessions.md).

### Session

Opt-in conversational memory: a stored thread of turns that a run can read back and append to, so a model remembers earlier messages. Passing a `session_id` on a run turns it on; without one, the run is a one-shot. See [Runs & sessions](../concepts/runs-and-sessions.md).

### Source

Where a managed model's weights come from: `hf` (a Hugging Face repo), `local-path` (a file or directory on disk), or `url`. Separate from the [binding](#binding) — Hugging Face is a source of weights, not an engine. See [Models & engines](../concepts/models-and-engines.md).

### Span

One node in a run's trace: a timed bar in the [observability](../running/observability.md) waterfall, with a status (`ok`, `err`, `skipped`, `running`) and, on model-generation phases, token counts. Spans carry timing and metadata; payloads are stored separately and gated by the [I/O capture](#io-capture) policy.

### Trigger

A deployed entry point that fires a saved, pinned agent unattended: a `schedule` (cron) or a `webhook` (an HTTP POST with an HMAC signature). A saved agent can also be reached through the always-on token-invoke endpoint, which is gated by `THEYGENT_INVOKE_TOKEN` rather than created as a trigger. See [Triggers & webhooks](../running/triggers.md).

### Version

An immutable, content-addressed snapshot of an agent, identified by its `version` string and [content hash](#content-hash). Publishing new content mints a new version; the old ones remain runnable. See [Agent versioning](../concepts/versioning.md).

### Webhook

A [trigger](#trigger) kind that runs an agent when an external system POSTs to `/hooks/{id}`. The request body becomes the run input; a per-webhook secret authenticates the call via an HMAC signature. See [Triggers & webhooks](../running/triggers.md).

### Worker

The standalone process (`theygent-worker`) that runs the durable queue in a server or air-gapped deployment. On your own machine you don't need it — setting `THEYGENT_DURABLE=1` runs the same runtime inside the control plane. See [Durable runs](../running/durable.md).

## Related pages

- [Architecture](../concepts/index.md) — how the two planes fit together.
- [API reference](api.md) — the endpoints these terms appear in.
- [Troubleshooting](troubleshooting.md) — when one of these goes wrong.
