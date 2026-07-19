# Environment variables

Every setting TheYgent reads from the environment. You configure these in the `.env` file at the repository root, which `make up` and the services load automatically. Only `DATABASE_URL` is required — everything else has a working default for a local install.

!!! tip "Where to set them"
    Copy `.env.example` to `.env` and edit it. The `Makefile` includes `.env` and exports every variable to the services it starts, so a value set there reaches all three planes. After editing `.env`, run `make restart` to pick up the change. See [Installation](../getting-started/installation.md).

## Required

| Variable | Default | Effect |
|---|---|---|
| `DATABASE_URL` | *(required)* | The Postgres connection string for the control plane and worker. Must be an async DSN: `postgresql+asyncpg://user:pass@host:5432/dbname`. The example value is `postgresql+asyncpg://theygent:theygent@localhost:5432/theygent`. Missing or unreachable → the control plane fails readiness and migrations refuse to run. |

## Connecting the planes

| Variable | Default | Effect |
|---|---|---|
| `THEYGENT_INFERENCE_PLANE_URL` | `http://127.0.0.1:8081/v1` | Where the control plane reaches the inference data plane. **Must include the `/v1` suffix.** Point this at a remote inference host to run models on another machine. |
| `THEYGENT_CONTROL_PLANE_HOST` | `127.0.0.1` | Address the control plane listens on. |
| `THEYGENT_CONTROL_PLANE_PORT` | `8080` | Port the control plane listens on. |
| `THEYGENT_INFERENCE_PLANE_HOST` | `127.0.0.1` | Address the inference plane listens on. |
| `THEYGENT_INFERENCE_PLANE_PORT` | `8081` | Port the inference plane listens on. |
| `THEYGENT_CORS_ORIGINS` | `localhost` / `127.0.0.1` on `:5173` and `:5174` when unset | Comma-separated list of browser origins allowed to call both planes. The shipped `.env.example` sets it to `http://localhost:5174`. An empty string disables CORS. Add your interface origin here if you serve it from a non-default port. |

## Control-plane behavior

| Variable | Default | Effect |
|---|---|---|
| `THEYGENT_DURABLE` | off | Set to `1` (also `true`/`yes`/`on`) to run the durable runtime in-process, enabling crash-resumable runs and the `human`/`subgraph`/`loop`/`map` node types. Interactive runs are unchanged either way. See [Durable runs](../running/durable.md). |
| `THEYGENT_INVOKE_TOKEN` | unset | A shared per-deploy bearer token for `POST /agents/{id}/invoke`. Personal [API keys](../running/users.md#personal-api-keys) (`tyk_…`) also open that endpoint, so this variable is optional; **with it unset and no API key presented, the endpoint is closed and every call returns 401.** |
| `THEYGENT_OIDC_REDIRECT_URL` | `http://localhost:8080/auth/oidc/callback` | Pins the `auth.oidc_redirect_url` platform setting — the redirect URL [sign-in providers](../running/users.md#single-sign-on) send the browser back to, and the exact URL you register with each provider. Change it when the control plane sits behind a reverse proxy or a non-default bind. |
| `THEYGENT_SECRET_KEY` | unset → ephemeral dev key | Fernet key(s) encrypting the connection secret store. **Unset means an in-memory key: stored secrets do not survive a restart** (you'll see a loud warning). Generate a real key and set it to persist secrets; supply a comma-separated list to rotate (the first encrypts, all decrypt). |
| `THEYGENT_ARTIFACT_DIR` | `<system tmp>/theygent-artifacts` | Where generated audio/image artifacts are stored on local disk. |
| `DBOS_SYSTEM_DATABASE_URL` | your `DATABASE_URL` | Optional override to put durable-runtime checkpoints on a different Postgres. State is confined to a separate `dbos` schema. |

## Observability

See [Observability](../running/observability.md) for how these interact.

| Variable | Default | Effect |
|---|---|---|
| `THEYGENT_IO_CAPTURE` | `full` | The ceiling for per-node input/output capture: `off`, `metadata` (byte sizes only), or `full` (payloads + sizes). This is a hard cap — no agent policy can capture above it. |
| `THEYGENT_TOPOLOGY` | `local` | `local` defaults capture to `full`; `hosted` defaults it to `metadata` (raw payloads never land by default in a hosted database). An agent can still opt back up to the ceiling. |
| `THEYGENT_IO_CAPTURE_MAX_BYTES` | `262144` (256 KiB) | Per-payload size cap. Larger values are stored truncated with the true byte count preserved, never silently dropped. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset → export off | When set, span *scalars* (never payloads) are exported to this OTLP/HTTP endpoint in addition to the local waterfall. |
| `THEYGENT_OTEL_REDACT_ATTRS` | unset | Comma-separated span-attribute names whose values are replaced with `[redacted]` on the exported copy (the local row keeps them). |

## Inference plane

| Variable | Default | Effect |
|---|---|---|
| `THEYGENT_MAX_RESIDENT` | `2` | How many model engines may be loaded in memory at once. When full, the least-important, least-recently-used engine is evicted to make room. Raise it if you have RAM to spare and switch models often. |
| `THEYGENT_INFERENCE_PLANE_STATE_DIR` | `~/.theygent/inference` | Where the local model registry (`registry.json`) and credentials (`credentials.json`) live. |
| `THEYGENT_INFERENCE_PLANE_MODEL_DIR` | `<state dir>/models` | Where catalog-installed model weights download. Point it at an external volume to keep large weights off your system disk. |

### Engine binary overrides

Each engine binary is resolved from `PATH` by default; set the matching variable to point at a specific build. See [Engines](../models/engines.md).

| Variable | Engine binary |
|---|---|
| `THEYGENT_LLAMACPP_BIN` | `llama-server` (chat, embeddings, vision) |
| `THEYGENT_WHISPERCPP_BIN` | `whisper-server` (speech-to-text) |
| `THEYGENT_MLX_BIN` | `mlx_lm.server` (chat, Apple Silicon) |
| `THEYGENT_MLX_VLM_BIN` | `mlx_vlm.server` (vision, Apple Silicon) |
| `THEYGENT_MLX_AUDIO_BIN` | `mlx_audio.server` (text-to-speech, Apple Silicon) |
| `THEYGENT_SDCPP_BIN` | `sd-cli` (image generation) |
| `THEYGENT_MFLUX_BIN` | `mflux-generate` (image generation, Apple Silicon) |
| `THEYGENT_VLLM_BIN` | `vllm` (chat, CUDA hosts) |

## Worker (server topology only)

The standalone [worker](../running/durable.md) reads `DATABASE_URL` (required) and `THEYGENT_INFERENCE_PLANE_URL`. For the inference URL it also accepts a legacy fallback name:

| Variable | Default | Effect |
|---|---|---|
| `THEYGENT_INFERENCE_PLANE_BASE_URL` | falls back to `THEYGENT_INFERENCE_PLANE_URL`, then `http://127.0.0.1:8081/v1` | Legacy alias the worker checks for the inference URL. Prefer `THEYGENT_INFERENCE_PLANE_URL`. |

## Interface (build-time)

The web app reads these at build time (Vite `VITE_*` variables). Change them only if you moved a plane off its default port.

| Variable | Default | Effect |
|---|---|---|
| `VITE_CONTROL_PLANE_URL` | `http://localhost:8080` | The control-plane base URL the browser calls. |
| `VITE_INFERENCE_URL` | `http://localhost:8081` | The inference-plane base URL the browser calls. |

## Advanced tuning

| Variable | Default | Effect |
|---|---|---|
| `THEYGENT_MCP_CALL_TIMEOUT_S` | `120` | Timeout, in seconds, for a single MCP tool call before it errors. Raise it for slow tools. |
| `THEYGENT_MCP_REGISTRIES` | *(unset)* | Extra MCP hubs for the catalog browser: a JSON array of `{"id", "label", "url"}` objects. Any registry speaking the standard subregistry API works; a self-hosted one doubles as an allowlist for air-gapped setups. A malformed value is a loud error, never a silently dropped registry. See [MCP servers](../mcp/index.md). |
| `THEYGENT_OAUTH_REDIRECT_URL` | `http://localhost:8080/mcp/oauth/callback` | Where an MCP OAuth sign-in redirects the browser back to. Change it only if the control plane is reached at a different address than the default. |

!!! note
    A number of other `THEYGENT_*` names exist only to drive tests and integration harnesses — they are not end-user configuration and are omitted here. If a name is not on this page, treat it as internal.

## Related pages

- [Installation](../getting-started/installation.md) — setting up `.env` for the first time.
- [Running TheYgent](../getting-started/running.md) — starting the services that read these.
- [Troubleshooting](troubleshooting.md) — symptoms that trace back to a misconfigured variable.
