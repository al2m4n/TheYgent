# Privacy and data flow

TheYgent's promise is **everything runs where you point it, and you own every hop** — not "no data ever leaves your machine". This page is the checkable version of that promise: where each kind of data lives, every connection the software makes on its own and when, and how to run with no network at all.

## Where your data lives

| What | Where it stays |
|---|---|
| Prompts and responses | Browser → control plane → inference plane, over local HTTP. All of them listen on `127.0.0.1` by default. A hosted model is reached only if you [register one](../models/remote.md), and then directly from the inference plane. |
| Model weights | The inference plane's model directory, `~/.theygent/inference/models/` by default (`THEYGENT_INFERENCE_PLANE_MODEL_DIR`). Weights you register by path stay where they are. |
| The model registry and hosted-API keys | Local files under `~/.theygent/inference/` (`THEYGENT_INFERENCE_PLANE_STATE_DIR`). Keys are referenced as `secret://NAME` and never sent to the control plane. |
| Agents, runs, sessions, traces | Your Postgres, at `DATABASE_URL`. What each agent's runs capture — and for how long — is set by its [I/O policy](../running/observability.md). |
| Retrieval documents | pgvector in the same Postgres, embedded by the model you bind to the source. Bind a local embedding model and the documents never leave the machine. See [RAG sources](../rag/index.md). |
| Tool and MCP secrets | The control plane's secret store, encrypted with `THEYGENT_SECRET_KEY`. Agents carry references, never values. |
| Audio and images | Local artifacts under `THEYGENT_ARTIFACT_DIR` on the control-plane host — never written into run history or exported with traces. |

## Every connection TheYgent makes

This is the complete list for a running install. Apart from the first row, nothing connects until you open a feature that needs the network, or configure one.

| Connection | When | What it carries |
|---|---|---|
| A public model price list from GitHub | Each time the inference plane starts. The model client library it uses to call engines (LiteLLM) loads it. | None of your data. With no network, the library falls back to the copy bundled with it, and startup continues. |
| Hugging Face | When you browse the model catalog, install a model, or start a model you registered by hand with `source: hf`. | Your catalog searches and the files you download. A [gated repository](../models/installing.md#install-from-the-catalog) also sends your Hugging Face token. Catalog installs register as local files, so they load from disk afterwards. |
| MCP registries | When you open **Browse hubs** on the MCP page. | Your searches. Replace the public hubs with a registry you host by setting `THEYGENT_MCP_REGISTRIES` — see [MCP servers](../mcp/index.md#installing-from-a-hub). |
| Things you configure | When an agent or a setting uses them. | Exactly what you wired: a [hosted API](../models/remote.md) you registered, a remote MCP server, an HTTP tool, a site you crawl for retrieval, a sign-in provider, an [OpenTelemetry collector](../running/observability.md), or the Claude Code traffic you route through the [AI Gateway](../ai-gateway/index.md). |

Installing TheYgent — cloning the repository, installing dependencies and engines, pulling container images — needs the network once.

### What TheYgent doesn't have

There is no usage analytics, crash reporting, update check, or license activation, and no vendor account: the only account is the local admin you create in the first-run wizard. Trace export is off until you set `OTEL_EXPORTER_OTLP_ENDPOINT` to a collector you run.

## Running offline or air-gapped

Once it is installed, TheYgent runs with no network access: the canvas, the engines, Postgres, durable runs, and retrieval with a local embedding model. To prepare a host that will never be online:

1. **Install while connected** — or on a connected machine of the same platform that you then move across. Follow [Installation](../getting-started/installation.md) through `make engines`, and run `make up` once so dependencies install and the schema migrates.
2. **Bring the weights.** Install models from the catalog while connected, or copy GGUF files and MLX model folders onto the host and [register them by path](../models/installing.md#register-a-model-already-on-disk). Avoid hand-registered `source: hf` models on an offline host: their engine looks for the weights on Hugging Face when it starts.
3. **Keep retrieval local.** Bind your [retrieval sources](../rag/index.md) to a local embedding model, and ingest by uploading files or crawling sites on your own network.
4. **Mirror what people browse.** If the MCP hub browser should work, set `THEYGENT_MCP_REGISTRIES` to a registry you host; it doubles as an allowlist of servers people can install.
5. **Run durable work in-house.** On a server, run the standalone durable worker next to the control plane — see [Durable runs](../running/durable.md).

The startup price-list request fails fast and falls back to the bundled copy; nothing else expects to be online.

## Who can reach your install

- **The control plane requires sign-in.** A fresh install's API is closed until the first-run wizard creates an admin; after that, every call carries a bearer token. See [Users, roles and sign-in](../running/users.md).
- **The inference plane has no sign-in of its own.** It listens on `127.0.0.1` by default (`THEYGENT_INFERENCE_PLANE_HOST`), and anything that can reach port 8081 can use your models. Keep it on loopback or a private network you trust.
- **The AI Gateway** in personal mode refuses any listen address but loopback; in team mode, every developer has a gateway key. See [Running the gateway](../ai-gateway/running.md).
